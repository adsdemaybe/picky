"""``--policy perceptron``: Gemini plans, Perceptron Mk1.5 grasps (VLA) and verifies; the SO-101 rover executes.

Mirrors Picky's live planner -> VLA -> verifier loop on the Genie Sim supermarket:

  Gemini (demo/planner.py)   route + a per-item step plan: approach, look, grasp, place_in_basket, verify
  approach                   A* + differential drive to the parking pose (so101_demo.Mission.drive_to)
  look                       the arm unfolds to a look pose 20 cm in front of the slot (store map), Mk1.5 is asked
                             whether the product is in the wrist image
  grasp = Perceptron VLA     per step: mast + wrist JPEGs + arm state + the last steps -> the policy sidecar
                             (demo/vla_sidecar.py, httpx) -> Mk1.5 answers with ONE tool call (reach_point / move_ee /
                             set_gripper / lift / done) -> vla_policy.map_action validates + clamps it -> IK -> the
                             arm's joint drives play it. Physics is paused while Mk1.5 thinks (no app.update() runs:
                             sim time and the 30 fps recording only advance while the arm moves).
                             close = the jaws close until the moving jaw touches the product; it is attached only when
                             it sits between the jaw pads (jaw-contact check). Up to --vla-attempts attempts of
                             --vla-max-calls calls; then the scripted IK grasp runs and the event log says so
                             (``vla_fallback``, tier per attempt). Never reported as a VLA pick.
  place_in_basket            scripted IK (tool down, swing, lower, open: gravity drops it in)
  verify = Perceptron        "is <product> held between the jaws?" (wrist frame, after the grasp) and "is <product> in
                             the basket?" (basket frame, after the drop); a "no" re-checks once with a fresh frame. The
                             sim's own state (jaw check / basket containment) is logged next to every verdict.

Events (events.jsonl, for demo/edit_so101.py): vla_ready, leg (+ steps), look, vla_step (tool, args, outcome, latency,
pointed pixel), vla_attempt, vla_fallback, verify, vla_summary. The JPEGs Mk1.5 saw are kept in RUN/vla/.
"""

from __future__ import annotations

import base64
import io
import json
import math
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from demo.so101_demo import GRIP_IN, SIDE_N, base_T, ik_natural, r2
from demo.vla_policy import (
    BASKET_PROMPT,
    GRIPPER_ROI_WRIST,
    LOOK_PROMPT,
    MODEL,
    Action,
    Cam,
    Session,
    Slot,
    grasp_targets,
    held_box,
    hfov_deg,
    map_action,
    project,
    short_call,
)

STEPS = ("approach", "look", "grasp", "place_in_basket", "verify")
REQUIRED = ("approach", "grasp", "place_in_basket")
SYNONYMS = {
    "drive": "approach",
    "navigate": "approach",
    "park": "approach",
    "find": "look",
    "locate": "look",
    "scan": "look",
    "pick": "grasp",
    "grab": "grasp",
    "place": "place_in_basket",
    "basket": "place_in_basket",
    "drop": "place_in_basket",
    "check": "verify",
    "confirm": "verify",
}
MAST = ("mast", (640, 480), 14.0)  # render name, resolution, focal (mm, 20.955 mm aperture: 73.6 deg hfov)
INSTRUCTION = "pick up the {name} and put it in the basket"


def norm_steps(raw) -> tuple[list[str], str]:
    """Gemini's per-stop step list -> the executor's canonical steps (order kept, unknown dropped, the required ones
    added). Returns (steps, 'gemini' | 'gemini+repaired' | 'default')."""
    if not isinstance(raw, list) or not raw:
        return list(STEPS), "default"
    out: list[str] = []
    for s in raw:
        name = s.get("step") if isinstance(s, dict) else s
        name = str(name or "").strip().lower().replace(" ", "_").replace("-", "_")
        name = SYNONYMS.get(name, name)
        if name in STEPS and name not in out:
            out.append(name)
    repaired = False
    for req in REQUIRED:
        if req not in out:
            out.append(req)
            repaired = True
    order = {s: k for k, s in enumerate(STEPS)}
    canon = sorted(out, key=lambda s: order[s])
    repaired = repaired or canon != out
    return canon, "gemini+repaired" if repaired else "gemini"


def jpeg(arr: np.ndarray, quality: int = 85) -> bytes:
    from PIL import Image  # noqa: PLC0415

    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr[..., :3])).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class Sidecar:
    """urllib client of demo/vla_sidecar.py on localhost (never talks to Perceptron itself)."""

    def __init__(self, url: str, timeout_s: float = 90.0) -> None:
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s

    def _req(self, path: str, obj: dict | None = None) -> dict:
        data = None if obj is None else json.dumps(obj).encode()
        req = urllib.request.Request(
            self.url + path, data=data, headers={"Content-Type": "application/json"} if data else {}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return {"error": json.loads(e.read()).get("error", f"HTTP {e.code}")}
            except Exception:  # noqa: BLE001
                return {"error": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001 - sidecar down / timeout
            return {"error": f"{type(e).__name__}: {e}"}

    def health(self) -> dict:
        return self._req("/health")

    def act(self, text: str, images: list[bytes], tag: str) -> dict:
        return self._req("/act", {"text": text, "images": [base64.b64encode(b).decode() for b in images], "tag": tag})

    def detect(self, classes: list[str], image: bytes, tag: str) -> dict:
        return self._req("/detect", {"classes": classes, "image": base64.b64encode(image).decode(), "tag": tag})

    def ask(self, prompt: str, images: list[bytes], tag: str) -> dict:
        return self._req(
            "/ask", {"prompt": prompt, "images": [base64.b64encode(b).decode() for b in images], "tag": tag}
        )


class VlaRig:
    """The Perceptron loop on one Mission (so101_demo.Mission) in Isaac Sim."""

    def __init__(
        self, ms, stage, robot_path: str, client: Sidecar, out: Path, wrist_res=(960, 540), max_calls=8, max_attempts=2
    ):
        from pxr import UsdGeom  # noqa: PLC0415

        self.ms, self.stage, self.client = ms, stage, client
        self.kin = ms.kin
        self.out = out
        (out / "vla").mkdir(parents=True, exist_ok=True)
        self.max_calls, self.max_attempts = int(max_calls), int(max_attempts)
        cw = stage.GetPrimAtPath(f"{robot_path}/so101_gripper/cam_wrist")
        self.wrist_local = np.array(UsdGeom.Xformable(cw).GetLocalTransformation()).T
        c = UsdGeom.Camera(cw)
        self.wrist_hfov = hfov_deg(float(c.GetFocalLengthAttr().Get()), float(c.GetHorizontalApertureAttr().Get()))
        self.wrist_res = tuple(int(v) for v in wrist_res)
        self.mast_path = "/World/picky_vla_mast"
        self.mast_hfov = hfov_deg(MAST[2], 20.955)
        self.mast_pose = None  # (eye, right, up, fwd) of the last render
        self.aim = None  # world point the mast camera looks at (the slot during a pick)
        self.k = 0
        self.held_item = None
        self.at_look = None  # item id while the arm still stands in that item's look pose
        self.stats = {
            "vla_grasps": 0,
            "scripted_fallbacks": 0,
            "vla_attempts": 0,
            "perceptron_calls": 0,
            "perceptron_latency_ms": [],
        }
        self.items_report: dict[str, dict] = {}
        h = client.health()
        self.available = bool(h.get("ok")) and bool(h.get("key"))
        self.why_unavailable = "" if self.available else (h.get("error") or "sidecar has no PERCEPTRON_API_KEY")
        ms.ev("vla_ready", ok=self.available, why=self.why_unavailable, model=h.get("model", MODEL), sidecar=client.url)

    # ---- cameras -------------------------------------------------------------------------------------------------
    @staticmethod
    def define_mast(stage, path: str, res=MAST[1], focal=MAST[2]):
        from pxr import Gf, UsdGeom  # noqa: PLC0415

        cam = UsdGeom.Camera.Define(stage, path)
        cam.CreateFocalLengthAttr(float(focal))
        cam.CreateHorizontalApertureAttr(20.955)
        cam.CreateVerticalApertureAttr(20.955 * res[1] / res[0])
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.03, 100.0))
        UsdGeom.Xformable(cam).AddTransformOp()
        return path

    def mast_hook(self, x, y, yaw):
        """Follow-camera hook (every captured frame): the mast camera rides 0.3 m behind the arm's pan axis, 1.6 m up,
        looking at the slot during a pick (else at shelf height 0.6 m to the robot's right)."""
        from demo import autopilot  # noqa: PLC0415

        Tb = base_T(x, y, yaw)
        pan_x = self.ms.pan_x
        eye = (Tb @ np.array([pan_x - 0.32, 0.16, 1.62, 1.0]))[:3]
        tgt = self.aim if self.aim is not None else (Tb @ np.array([pan_x + 0.05, -0.60, 0.85, 1.0]))[:3]
        autopilot.look_at(self.stage, self.mast_path, eye, tgt, focal=MAST[2])
        z = (eye - tgt) / np.linalg.norm(eye - tgt)
        xr = np.cross([0.0, 0.0, 1.0], z)
        xr /= np.linalg.norm(xr) + 1e-9
        yu = np.cross(z, xr)
        self.mast_pose = (eye, xr, yu, -z)

    def cams(self) -> dict[str, Cam]:
        ms = self.ms
        out = {}
        if self.mast_pose is not None:
            e, r, u, f = self.mast_pose
            out["mast"] = Cam(tuple(e), tuple(r), tuple(u), tuple(f), self.mast_hfov, MAST[1][0], MAST[1][1])
        Tw = ms.Tb() @ self.kin.links(ms.arm_vec())["so101_gripper"] @ self.wrist_local
        out["wrist"] = Cam.from_matrix(Tw, self.wrist_hfov, *self.wrist_res)
        return out

    def frame(self, name: str) -> np.ndarray | None:
        rec = self.ms.rec
        if rec is None or name not in rec.ann:
            return None
        d = rec.ann[name].get_data()
        if d is None or getattr(d, "size", 0) == 0:
            return None
        return np.asarray(d)[..., :3].copy()

    def save(self, name: str, data: bytes) -> str:
        p = self.out / "vla" / name
        p.write_bytes(data)
        return p.name

    # ---- geometry from the store map (the scene's planogram) -------------------------------------------------------
    def slot(self, it) -> Slot:
        n = np.array([*SIDE_N[it["side"]], 0.0])
        c = np.asarray(it["center"], float)
        size = np.asarray(it["size"], float)
        depth = float(abs(size[:2] @ n[:2]))
        face = c + n * depth / 2
        return Slot(
            face=tuple(float(v) for v in face),
            normal=tuple(float(v) for v in n),
            board_z=float(c[2] - size[2] / 2),
            height=float(size[2]),
            width=float(it["width"]),
        )

    def geometry(self, it) -> dict:
        ms, kin = self.ms, self.kin
        n = np.array([*SIDE_N[it["side"]], 0.0])
        ph = math.radians(it.get("pitch", 0) or 0)
        roll = it.get("roll")
        a_w = -n * math.cos(ph) + np.array([0, 0, -math.sin(ph)])
        Tbi = np.linalg.inv(ms.Tb())
        a_b = Tbi[:3, :3] @ a_w
        R = kin.target_rotation(a_b, roll)
        size = np.asarray(it["size"], float)
        dl = float(abs(size[:2] @ n[:2]))
        shift_in = min(dl / 2, GRIP_IN)
        # VLA grasps centre the product between WIDE-open jaws (pointing is +-1 cm; the scripted grasp knows the
        # product's true pose and parks the fixed jaw against it instead)
        g_open = ms.g_open_for(float(it["width"]), clear=0.024)
        x_mid = 0.5 * (0.002 + float(np.interp(g_open, ms.jaw_g, ms.jaw_x)))
        return {
            "n": n,
            "a_w": a_w,
            "a_b": a_b,
            "R": R,
            "roll": roll,
            "shift_in": shift_in,
            "far_d": max(0.12, shift_in + 0.09),
            "g_open": g_open,
            "x_mid": x_mid,
        }

    def jaw_center_w(self, geo) -> np.ndarray:
        ms = self.ms
        T = ms.world_tcp()
        return T[:3, 3] + T[:3, 0] * geo["x_mid"]

    def tcp_for_jaw(self, p_w, geo) -> np.ndarray:
        """base-frame TCP that puts the jaw centre (between the open jaws) at world point p_w"""
        return self.ms.to_base(p_w) - geo["R"][:, 0] * geo["x_mid"]

    # ---- arm motions -------------------------------------------------------------------------------------------------
    def _line_to(self, p1_b, R, n=8, T=None):
        ms = self.ms
        q0 = ms.arm_vec()
        p0 = self.kin.fk(q0)[:3, 3]
        qs, err = ms.line(q0, p0, p1_b, R, n=n)
        if err > 0.012:
            return None, err
        dist = float(np.linalg.norm(np.asarray(p1_b) - p0))
        ms.plan_path(qs, T=T or max(0.6, dist / 0.08))
        ms.wait_arm()
        return qs[-1], err

    def look_pose(self, it, geo) -> bool:
        """unfold to a pose 20 cm in front of the slot's face (store map), jaws open, wrist camera on the slot"""
        ms, kin = self.ms, self.kin
        slot = self.slot(it)
        face = np.asarray(slot.face)
        z = slot.board_z + min(0.5 * slot.height, 0.12)
        best = None
        for d in (0.20, 0.17, 0.24, 0.14):
            p = face + geo["n"] * d
            p[2] = z
            tcp = self.tcp_for_jaw(p, geo)
            q, e = ik_natural(kin, tcp, geo["R"], roll=geo["roll"])
            if e[0] < 0.006 and e[1] < math.radians(5):
                best = q
                break
        if best is None:
            return False
        ms.phase, ms.sub = "look", "arm to the look pose (store map slot)"
        ms.plan_move(
            {
                "shoulder_pan": float(best[0]),
                "shoulder_lift": -0.3,
                "elbow_flex": 0.9,
                "wrist_flex": 0.4,
                "wrist_roll": float(best[4]),
            },
            vscale=0.85,
        )
        ms.plan_move({"gripper": geo["g_open"]}, T=0.8)
        ms.wait_arm()
        ms.plan_move(ms.arm_dict(best), vscale=0.8)
        ms.wait_arm()
        ms.wait(0.2)
        return True

    def retract(self, geo, back=0.10, up=0.01):
        ms = self.ms
        T = self.kin.fk(ms.arm_vec())
        p = T[:3, 3] - geo["a_b"] * back
        p = p + np.array([0, 0, up])
        q, _ = self._line_to(p, T[:3, :3], n=6, T=0.8)
        if q is None:  # no straight line out: fold (the jaws are open, nothing held)
            ms.plan_move(ms.travel_pose(), vscale=0.6)
            ms.wait_arm()

    def execute(self, act: Action, it, geo) -> str:
        ms, kin = self.ms, self.kin
        held = ms.held is not None
        if act.kind in ("noop", "done"):
            return act.note
        if act.kind == "gripper":
            if not act.close:
                ms.phase = "reach"
                ms.plan_move({"gripper": geo["g_open"]}, T=0.6)
                ms.wait_arm()
                return "opened the gripper"
            ms.phase = "grasp"
            g_c = ms.contact_angle(it["path"], float(ms.qm["gripper"]))
            if g_c is None:
                ms.plan_move({"gripper": 0.0}, T=0.8)
                ms.wait_arm()
                ms.wait(0.2)
                ms.ev("grasp_check", item=it["id"], tier="perceptron_vla", ok=False, why="the jaws closed on nothing")
                return "closed the gripper"
            ms.plan_move({"gripper": g_c}, T=0.9)
            ms.wait_arm()
            ms.wait(0.25)
            chk = ms.jaw_check(it["path"])
            ok = chk["moving_jaw_touch"] and chk["pads_around_it"]
            ms.ev("grasp_check", item=it["id"], tier="perceptron_vla", g_contact=round(g_c, 3), **{**chk, "ok": ok})
            if not ok:
                return "closed the gripper"
            ms.attach(it["path"])
            self.held_item = it["id"]
            ms.ev("attached", item=it["id"], name=it["name"], tier="perceptron_vla")
            return "closed the gripper: the product is held (moving jaw on it, between the pads)"
        if act.kind in ("nudge", "lift"):
            ms.phase = "lift" if act.kind == "lift" else ("lift" if held else "reach")
            T = kin.fk(ms.arm_vec())
            Tbi = np.linalg.inv(ms.Tb())
            p1 = T[:3, 3] + Tbi[:3, :3] @ act.delta
            q, err = self._line_to(p1, T[:3, :3], n=6)
            if q is None:
                return f"ignored (no straight IK path, error {err * 100:.1f} cm)"
            d = act.delta * 100
            if act.kind == "lift":
                return f"lifted {d[2]:.0f} cm" + (" with the product" if held else "")
            return f"moved {act.note.split(' ', 1)[1]} (ik error {err * 100:.1f} cm)"
        if act.kind == "reach":
            if held:
                return "ignored: already holding the product (lift or done)"
            ms.phase = "reach"
            tcp, far = grasp_targets(
                act.point, geo["n"], ms.Tb(), geo["R"], geo["a_b"], geo["x_mid"], geo["shift_in"], geo["far_d"]
            )
            qg, e = ik_natural(kin, tcp, geo["R"], roll=geo["roll"], extra_seeds=[ms.arm_vec()])
            if e[0] > 0.008 or e[1] > math.radians(5):
                return (
                    f"ignored: target out of reach (ik error {e[0] * 100:.1f} cm, {math.degrees(e[1]):.0f} deg; "
                    "a level side grasp needs <= 0.8 cm, 5 deg)"
                )
            qf, ef = kin.solve(far, R=geo["R"], w_rot=0.1, seeds=[qg])
            line_in, err_in = ms.line(qf, far, tcp, geo["R"], n=12)
            if ef[0] > 0.008 or err_in > 0.008:
                return "ignored: no straight approach to that point (IK)"
            # out of the shelf first if the jaws are in, open, to the pre-grasp, straight in
            T = kin.fk(ms.arm_vec())
            depth_now = float((T[:3, 3] - self.ms.to_base(np.asarray(self.slot(it).face))) @ (-geo["a_b"]))
            if depth_now < geo["far_d"] - 0.02:
                self.retract(geo, back=max(0.0, geo["far_d"] - depth_now))
            if ms.qm["gripper"] < geo["g_open"] - 0.05:
                ms.plan_move({"gripper": geo["g_open"]}, T=0.6)
            ms.plan_move(ms.arm_dict(qf), vscale=0.8)
            ms.wait_arm()
            ms.wait(0.1)
            ms.plan_path(line_in, T=1.6)
            ms.wait_arm()
            ms.wait(0.15)
            note = f"moved the open jaws to the point (ik error {e[0] * 100:.1f} cm)"
            return note + (f"; clamped {', '.join(act.clipped)}" if act.clipped else "")
        return f"ignored ({act.kind})"

    # ---- one VLA attempt -----------------------------------------------------------------------------------------
    def attempt(self, it, k: int, instruction: str) -> dict:
        ms = self.ms
        geo = self.geometry(it)
        slot = self.slot(it)
        sess = Session(instruction)
        self.aim = np.asarray(it["center"], float)
        if self.at_look != it["id"] and not self.look_pose(it, geo):
            return {"held": False, "calls": 0, "tools": [], "why": "no look pose (IK)"}
        self.at_look = None
        calls, post, tools, errors = 0, 0, [], 0
        ms.ev(
            "vla_grasp_setup", item=it["id"], attempt=k, wrist_flip=bool(it.get("flip")), g_open=round(geo["g_open"], 2)
        )
        while calls < self.max_calls:
            ms.wait(0.12)  # settle: the annotators hold this pose
            mast, wrist = self.frame("mast"), self.frame("wrist")
            if mast is None or wrist is None:
                return {"held": False, "calls": calls, "tools": tools, "why": "no mast / wrist frame"}
            cams = self.cams()
            jaw = self.jaw_center_w(geo)
            g_open_now = float(ms.qm["gripper"]) > 0.25
            held = ms.held is not None
            text = sess.user_text(sess.state_text(g_open_now, held, jaw, slot))
            mj, wj = jpeg(mast), jpeg(wrist)
            self.k += 1
            stem = f"{self.k:04d}_{it['id']}_a{k}_c{calls + 1}"
            self.save(stem + "_mast.jpg", mj)
            self.save(stem + "_wrist.jpg", wj)
            ms.sub = "Perceptron VLA thinking"
            resp = self.client.act(text, [mj, wj], tag=stem)
            calls += 1
            self.stats["perceptron_calls"] += int(resp.get("attempts", 1) or 1) if not resp.get("error") else 0
            if resp.get("error"):
                errors += 1
                ms.ev("vla_error", item=it["id"], attempt=k, n=calls, error=str(resp["error"])[:200])
                if errors >= 2:
                    return {"held": False, "calls": calls, "tools": tools, "why": "Perceptron unavailable"}
                continue
            self.stats["perceptron_latency_ms"].append(resp.get("latency_ms"))
            name, args = resp.get("tool"), resp.get("args") or {}
            if not name:
                sess.record("no_tool", {}, "answered in text; call a tool")
                ms.ev(
                    "vla_step",
                    item=it["id"],
                    attempt=k,
                    n=calls,
                    tool=None,
                    text=resp.get("text", "")[:160],
                    outcome="no tool call",
                    latency_ms=resp.get("latency_ms"),
                    model=resp.get("model"),
                    img=stem,
                )
                continue
            act = map_action(name, args, cams, slot, jaw)
            act = sess.guard(act, g_open_now, held)
            px = None
            if name == "reach_point":
                cam = "wrist" if str(args.get("camera", "")).lower().startswith("w") else "mast"
                px = [cam, args.get("x"), args.get("y")]
            ms.sub = f"Perceptron VLA: {short_call(name, args)}"
            outcome = self.execute(act, it, geo)
            held = ms.held is not None
            sess.record(name, args, outcome)
            if act.kind == "gripper" and act.close:
                sess.resolve_close(held)
            proj = None
            if act.point is not None:
                pr = project(cams["wrist"], act.point)
                proj = [round(pr[0]), round(pr[1])] if pr else None
            ms.ev(
                "vla_step",
                item=it["id"],
                attempt=k,
                n=calls,
                tool=name,
                args={a: (round(v, 1) if isinstance(v, float) else v) for a, v in args.items()},
                action=short_call(name, args),
                act_kind=act.kind,
                outcome=outcome,
                clipped=act.clipped,
                point=r2(act.point) if act.point is not None else None,
                point_px=px,
                target_in_wrist=proj,
                latency_ms=resp.get("latency_ms"),
                model=resp.get("model"),
                held=held,
                img=stem,
            )
            if not outcome.startswith(("ignored", "refused")):
                tools.append(short_call(name, args))
            if held:
                post += 1
                if act.kind in ("lift", "done") or post >= 3:
                    break
            elif act.kind == "done":
                break
        return {"held": ms.held is not None, "calls": calls, "tools": tools, "why": "" if ms.held else "no grasp"}

    # ---- grasp step (VLA attempts, then the scripted fallback) ------------------------------------------------------
    def instruction(self, it, hint: str = "") -> str:
        looks = it.get("looks") or hint
        nm = it["name"].lower() + (f" ({looks})" if looks else "")
        return INSTRUCTION.format(name=nm)

    def look(self, it):
        """the 'look' step: is the product in the wrist image? (after look_pose)"""
        ms = self.ms
        self.aim = np.asarray(it["center"], float)
        geo = self.geometry(it)
        if not self.look_pose(it, geo):
            ms.ev("look", item=it["id"], ok=False, why="no look pose (IK)")
            return None
        self.at_look = it["id"]
        ms.wait(0.12)
        w = self.frame("wrist")
        if w is None or not self.available:
            return None
        wj = jpeg(w)
        self.k += 1
        stem = f"{self.k:04d}_{it['id']}_look"
        self.save(stem + "_wrist.jpg", wj)
        self.debug_overlay(stem, it, {"wrist": w, "mast": self.frame("mast")})
        ms.phase, ms.sub = "look", "Perceptron: is it in the wrist image?"
        r = self.client.ask(LOOK_PROMPT.format(product=f"the {it['name'].lower()}"), [wj], tag=stem)
        if not r.get("error"):
            self.stats["perceptron_calls"] += 1
            self.stats["perceptron_latency_ms"].append(r.get("latency_ms"))
        ms.ev("look", item=it["id"], name=it["name"], answer=r.get("text", r.get("error", ""))[:160],
              verdict=r.get("verdict"), latency_ms=r.get("latency_ms"), model=r.get("model"), img=stem)  # fmt: skip
        return r.get("verdict")

    def debug_overlay(self, stem, it, frames: dict):
        """camera-model check: the map's product centre projected into each frame (red cross) -> RUN/vla/*_dbg.jpg"""
        from PIL import Image, ImageDraw  # noqa: PLC0415

        cams = self.cams()
        for name, arr in frames.items():
            if arr is None or name not in cams:
                continue
            im = Image.fromarray(np.ascontiguousarray(arr))
            pr = project(cams[name], np.asarray(it["center"], float))
            if pr is not None:
                x, y = pr[0] / 1000 * im.size[0], pr[1] / 1000 * im.size[1]
                d = ImageDraw.Draw(im)
                d.line([x - 14, y, x + 14, y], fill=(255, 0, 0), width=3)
                d.line([x, y - 14, x, y + 14], fill=(255, 0, 0), width=3)
            im.save(self.out / "vla" / f"{stem}_{name}_dbg.jpg", quality=85)

    def grasp(self, it, hint: str = "") -> tuple[bool, str]:
        ms = self.ms
        rep = self.items_report.setdefault(
            it["id"], {"name": it["name"], "attempts": [], "tier": None, "verify": [], "in_basket": None}
        )
        reason = self.why_unavailable
        if self.available:
            instruction = self.instruction(it, hint)
            for k in range(1, self.max_attempts + 1):
                self.stats["vla_attempts"] += 1
                res = self.attempt(it, k, instruction)
                rep["attempts"].append({"tier": "perceptron_vla", **res})
                ms.ev("vla_attempt", item=it["id"], name=it["name"], attempt=k, tier="perceptron_vla", **res)
                if res["held"]:
                    self.stats["vla_grasps"] += 1
                    ms.stats["grasp_ok"] += 1
                    rep["tier"] = "perceptron_vla"
                    self.finish(it)
                    self.verify_held(it, rep)
                    self.aim = None
                    return True, ""
                ms.stats["grasp_fail"] += 1
                self.reset(it)
                reason = res["why"] or "no grasp"
                if res["why"] == "Perceptron unavailable":
                    break
        # scripted fallback: logged as such, never counted as a VLA pick
        self.stats["scripted_fallbacks"] += 1
        ms.ev("vla_fallback", item=it["id"], name=it["name"], reason=reason, attempts=len(rep["attempts"]))
        ms.sub = "scripted IK grasp (fallback)"
        ms.plan_move({**ms.travel_pose(), "gripper": 0.0}, vscale=0.85)
        ms.wait_arm()
        ok, why = ms.pick_shelf(it)
        if not ok and why == "slipped out of the jaws":
            ms.ev("retry", item=it["id"])
            ok, why = ms.pick_shelf(it)
        rep["attempts"].append({"tier": "scripted_fallback", "held": ok, "why": why})
        if ok:
            rep["tier"] = "scripted_fallback"
            self.verify_held(it, rep)
        self.aim = None
        return ok, why

    def reset(self, it):
        """after a missed attempt: open, out of the shelf, fold towards the shelf side"""
        ms = self.ms
        geo = self.geometry(it)
        if ms.held is not None:
            ms.release(ms.held[0])
        ms.plan_move({"gripper": geo["g_open"]}, T=0.5)
        ms.wait_arm()
        self.retract(geo, back=geo["far_d"])

    def finish(self, it):
        """the scripted rig takes the arm back (unit held): up 2.5 cm + out along the approach (as pick_shelf)"""
        ms = self.ms
        geo = self.geometry(it)
        ms.phase, ms.sub = "lift", "lift + retract"
        T = self.kin.fk(ms.arm_vec())
        p_up = T[:3, 3] + np.array([0, 0, 0.025])
        self._line_to(p_up, T[:3, :3], n=5, T=0.8)
        a = geo["a_b"]
        back = p_up - np.array([a[0], a[1], 0.0]) / max(np.linalg.norm(a[:2]), 1e-6) * geo["far_d"]
        q, _ = self._line_to(back, T[:3, :3], n=10, T=1.4)
        if q is None:
            ms.plan_move({**ms.travel_pose()}, vscale=0.7)
            ms.wait_arm()

    # ---- verifier -------------------------------------------------------------------------------------------------
    def _verify(self, it, what: str, cam: str, prompt: str, truth: bool, rep: dict) -> bool | None:
        ms = self.ms
        if not self.available:
            return None
        verdict = None
        for k in (1, 2):
            ms.phase, ms.sub = "verify", f"Perceptron verifier: {what}"
            ms.wait(0.15 if k == 1 else 0.4)
            f = self.frame(cam)
            if f is None:
                return None
            fj = jpeg(f)
            self.k += 1
            stem = f"{self.k:04d}_{it['id']}_verify_{what}_{k}"
            self.save(stem + f"_{cam}.jpg", fj)
            r = self.client.ask(prompt.format(product=f"the {it['name'].lower()}"), [fj], tag=stem)
            if not r.get("error"):
                self.stats["perceptron_calls"] += 1
                self.stats["perceptron_latency_ms"].append(r.get("latency_ms"))
            verdict = r.get("verdict")
            ms.ev("verify", item=it["id"], name=it["name"], what=what, camera=cam, check=k,
                  answer=r.get("text", r.get("error", ""))[:160], verdict=verdict, sim_state=truth,
                  agrees=(verdict == truth) if verdict is not None else None, latency_ms=r.get("latency_ms"),
                  model=r.get("model"), img=stem)  # fmt: skip
            rep["verify"].append({"what": what, "check": k, "verdict": verdict, "sim": truth})
            if verdict is True:
                break
        return verdict

    def verify_held(self, it, rep):
        """gripper_holding as the live verifier judges it: Mk1.5 detects the product in the wrist frame; held = a box
        over the jaws (IoU with GRIPPER_ROI_WRIST > 0.2). Checked after the retract (a unit left on the shelf is then
        >= 12 cm away and small). A 'no' re-checks once with a fresh frame; the sim's attach state is logged with it."""
        ms = self.ms
        truth = ms.held is not None and ms.held[0] == it["path"]
        if not self.available:
            return None
        verdict = None
        for k in (1, 2):
            ms.phase, ms.sub = "verify", "Perceptron verifier: held?"
            ms.wait(0.15 if k == 1 else 0.4)
            f = self.frame("wrist")
            if f is None:
                return None
            fj = jpeg(f)
            self.k += 1
            stem = f"{self.k:04d}_{it['id']}_verify_held_{k}"
            self.save(stem + "_wrist.jpg", fj)
            r = self.client.detect([it["name"].lower()], fj, tag=stem)
            if r.get("error"):
                ms.ev("verify", item=it["id"], name=it["name"], what="held", camera="wrist", check=k,
                      answer=str(r["error"])[:160], verdict=None, sim_state=truth, img=stem)  # fmt: skip
                continue
            self.stats["perceptron_calls"] += 1
            self.stats["perceptron_latency_ms"].append(r.get("latency_ms"))
            verdict, box, iou = held_box(r.get("boxes") or [])
            answer = (
                f"{it['name']} box over the jaws (IoU {iou:.2f})"
                if verdict
                else (f"{it['name']} seen, not over the jaws (IoU {iou:.2f})" if box else f"no {it['name']} box")
            )
            ms.ev("verify", item=it["id"], name=it["name"], what="held", camera="wrist", check=k, answer=answer,
                  verdict=verdict, box=box, roi=list(GRIPPER_ROI_WRIST), iou=iou, sim_state=truth,
                  agrees=verdict == truth, latency_ms=r.get("latency_ms"), model=r.get("model"), img=stem)  # fmt: skip
            rep["verify"].append({"what": "held", "check": k, "verdict": verdict, "sim": truth, "iou": iou})
            if verdict:
                break
        return verdict

    def verify_basket(self, it, inside: bool):
        rep = self.items_report.setdefault(it["id"], {"name": it["name"], "attempts": [], "verify": []})
        rep["in_basket"] = bool(inside)
        ms = self.ms
        ms.wait_arm()
        if "basket" not in (ms.rec.ann if ms.rec else {}):
            return None
        return self._verify(it, "in_basket", "basket", BASKET_PROMPT, bool(inside), rep)

    def summary(self) -> dict:
        lat = sorted(v for v in self.stats["perceptron_latency_ms"] if v is not None)
        out = {
            **{k: v for k, v in self.stats.items() if k != "perceptron_latency_ms"},
            "perceptron_latency_ms_median": lat[len(lat) // 2] if lat else None,
            "perceptron_latency_ms_mean": round(sum(lat) / len(lat), 1) if lat else None,
            "items": self.items_report,
        }
        try:
            h = self.client.health()
            out["sidecar_calls"] = h.get("calls")
        except Exception:  # noqa: BLE001
            pass
        return out


def trip_vla(ms, rig: VlaRig, items: dict, plan_stops: list, planner, ctx: dict, replan) -> bool:
    """so101_demo.trip with Gemini's per-item step plan: approach -> look -> grasp (VLA) -> place -> verify."""
    ms.queue = list(plan_stops)
    while ms.queue:
        stop = ms.queue.pop(0)
        iid = stop["item"]
        it = items.get(iid)
        if it is None or iid in ms.done:
            continue
        steps, src = norm_steps(stop.get("steps"))
        ms.target = iid
        ms.phase, ms.sub = "drive", it["name"]
        ms.ev(
            "leg",
            item=iid,
            name=it["name"],
            section=it["section"],
            section_label=it.get("section_label", ""),
            reason=stop.get("reason", ""),
            steps=steps,
            steps_source=src,
            grasp_hint=str(stop.get("grasp_hint", ""))[:80],
        )
        ok = ms.drive_to(it["xy"], it["yaw"], label=iid)
        why = "has no collision-free path to its aisle"
        if ok:
            ms.ev("step_done", item=iid, step="approach")
            ms.wait_arm()
            if ms.director is not None:
                ms.director.frame_work(it["center"], it["xy"], it["yaw"])
            if "look" in steps:
                rig.look(it)
                ms.ev("step_done", item=iid, step="look")
            ok, why = rig.grasp(it, hint=str(stop.get("grasp_hint", "")))
        if not ok:
            ms.ev("pick_failed", item=iid, name=it["name"], why=why)
            ms.plan_move({**ms.travel_pose(), "gripper": 0.0}, vscale=0.85)
            ms.wait_arm()
            replan(ms, items, stop, f"{it['name']} {why}.", planner, ctx)
            continue
        ms.ev("step_done", item=iid, step="grasp", tier=rig.items_report.get(iid, {}).get("tier"))
        inside = ms.to_basket(it)
        ms.ev("step_done", item=iid, step="place_in_basket", inside=inside)
        if "verify" in steps:
            rig.verify_basket(it, inside)
            ms.ev("step_done", item=iid, step="verify")
        else:
            rig.items_report.setdefault(iid, {})["in_basket"] = bool(inside)
        ms.done.append(iid)
        ms.ev("item_done", item=iid, name=it["name"], n=len(ms.done))
        if ms.director is not None:
            ms.director.mode = "chase"
    return True
