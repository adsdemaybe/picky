"""Demo-video recorder inside the sim worker: renders the director camera (+ the rover's front / wrist cameras at a
lower rate for picture-in-picture) at a FIXED SIM-TIME step and writes JPEG frames + timing logs to disk.

Isaac-free (it renders through the ``SimBackend`` seam), so it also runs on the sketch / null backends and in tests.
Enabled with ``python -m arms.sim.app --record-control <file.json>``; the worker polls that JSON file (wall clock,
every ``POLL_S``), so a driver script starts / stops recordings and switches shots while Isaac keeps running::

    {"seq": 3, "active": true, "dir": "/data/.../recordings/taco1", "robot_id": "rover-ave-1",
     "fps": 15, "w": 1920, "h": 1080, "pip_every": 3, "quality": 92,
     "shot": {"kind": "chase"} | {"kind": "orbit", "center": [x, y, z], "radius": 3, "height": 2.4, "period_s": 30}
             | {"kind": "path", "keys": [[0, [ex, ey, ez], [tx, ty, tz]], [6, [...], [...]]]},
     "cut": false}

``seq`` must change for an edit to apply. While active, every main-loop iteration advances the sim by exactly
``1 / fps`` seconds and renders one director frame, whatever the wall time (the sim runs slower than real time when
rendering is slow; the gateway's command timeouts must allow for it: PICKY_STEP_TIMEOUT_SCALE / robot timeouts).

Output (``dir``): ``director/NNNNNN.jpg``, ``front/NNNNNN.jpg`` + ``wrist/NNNNNN.jpg`` (every ``pip_every`` frames),
``frames.jsonl`` (one line per director frame: n, sim_t, wall, shot, robot pose / status / lift / pan / holding /
basket count, pip flag) and ``commands.jsonl`` (motion command start / done with sim_t + wall + result).
"""

import json
import math
import os
import time
from pathlib import Path
from typing import Any

from arms.sim.director import Director, Shot, World
from arms.sim.worker import SimBackend, WorkerCore
from utils.log import get_logger

log = get_logger(__name__)

POLL_S = 0.5
DEFAULT_FPS = 15.0


def _encode(rgb: Any, quality: int) -> bytes | None:
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

        arr = np.ascontiguousarray(np.asarray(rgb)[..., :3]).astype("uint8")
        ok, buf = cv2.imencode(".jpg", arr[..., ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return bytes(buf) if ok else None
    except ImportError:
        pass
    try:
        import io

        import numpy as np
        from PIL import Image

        out = io.BytesIO()
        Image.fromarray(np.asarray(rgb)[..., :3].astype("uint8")).save(out, format="JPEG", quality=quality)
        return out.getvalue()
    except ImportError:
        return None


class Recorder:
    def __init__(self, core: WorkerCore, backend: SimBackend, control_path: str) -> None:
        self.core, self.backend = core, backend
        self.control_path = Path(control_path)
        self.cfg: dict[str, Any] = {}
        self.seq: Any = None
        self.active = False
        self.dir: Path | None = None
        self.robot_id: str | None = None
        self.fps = DEFAULT_FPS
        self.pip_every = 3
        self.quality = 92
        self.director: Director | None = None
        self.n = 0
        self._last_poll = 0.0
        self._frames_f: Any = None
        self._cmds_f: Any = None
        self._t_render = 0.0
        self.core.on_command = self._on_command

    @property
    def dt(self) -> float:
        return 1.0 / max(1.0, self.fps)

    # -- control -------------------------------------------------------------------------------------------------------
    def poll(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if now - self._last_poll < POLL_S:
            return
        self._last_poll = now
        try:
            cfg = json.loads(self.control_path.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - half-written file: try again next poll
            log.debug("recorder control unreadable: %s", exc)
            return
        if not isinstance(cfg, dict) or cfg.get("seq") == self.seq:
            return
        self.seq = cfg.get("seq")
        self.apply(cfg)

    def apply(self, cfg: dict[str, Any]) -> None:
        was_active, old_dir = self.active, self.dir
        self.cfg = cfg
        self.active = bool(cfg.get("active"))
        self.robot_id = cfg.get("robot_id") or self.robot_id or next(iter(self.core.rovers), None)
        self.fps = float(cfg.get("fps", self.fps))
        self.pip_every = max(0, int(cfg.get("pip_every", self.pip_every)))
        self.quality = int(cfg.get("quality", self.quality))
        new_dir = Path(cfg["dir"]) if cfg.get("dir") else old_dir
        if self.active and (new_dir != old_dir or not was_active):
            self._open(new_dir)
        if not self.active and was_active:
            self._close()
            log.info("recorder: stopped after %d frames", self.n)
        rover = self.core.rovers.get(self.robot_id or "")
        if rover is not None:
            scene = self.core.stores.get(rover.store_id)
            w, h = int(cfg.get("w", 1920)), int(cfg.get("h", 1080))
            if self.director is None or (self.director.w, self.director.h) != (w, h) or scene is None:
                world = (
                    World.from_planogram(scene.planogram.bounds, scene.planogram.fixtures)
                    if scene is not None
                    else World(0, 0, 20, 20)
                )
                self.director = Director(world=world, w=w, h=h)
            self.director.set_shot(Shot.from_dict(cfg.get("shot")), cut=bool(cfg.get("cut")))
        self._log_cmd({"event": "control", "cfg": {k: v for k, v in cfg.items() if k != "shot"}, "shot": cfg.get("shot")})

    def _open(self, d: Path | None) -> None:
        self._close()
        if d is None:
            self.active = False
            log.warning("recorder: no dir in the control file")
            return
        self.dir = d
        for sub in ("director", "front", "wrist"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        existing = sorted((d / "director").glob("*.jpg"))
        self.n = int(existing[-1].stem) + 1 if existing else 0  # resume numbering in the same dir
        self._frames_f = open(d / "frames.jsonl", "a", buffering=1)  # noqa: SIM115 - kept open while recording
        self._cmds_f = open(d / "commands.jsonl", "a", buffering=1)  # noqa: SIM115
        log.info("recorder: recording %s at %.0f fps (sim time) into %s", self.robot_id, self.fps, d)

    def _close(self) -> None:
        for f in (self._frames_f, self._cmds_f):
            if f is not None:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass
        self._frames_f = self._cmds_f = None

    def _log_cmd(self, row: dict[str, Any]) -> None:
        if self._cmds_f is None:
            return
        row = {"sim_t": round(self.core.sim_time, 4), "wall": round(time.time(), 4), "frame": self.n, **row}
        try:
            self._cmds_f.write(json.dumps(row, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _on_command(self, phase: str, cmd: Any, res: Any) -> None:
        if not self.active:
            return
        row: dict[str, Any] = {
            "event": phase,
            "type": getattr(cmd, "type", None),
            "cmd_id": getattr(cmd, "cmd_id", None),
            "robot_id": getattr(cmd, "robot_id", None),
            "slot_id": getattr(cmd, "slot_id", None),
            "sku": getattr(cmd, "sku", None),
        }
        if res is not None:
            row.update(
                ok=getattr(res, "ok", None),
                error_code=getattr(res, "error_code", None),
                instance_id=getattr(res, "instance_id", None),
                n_instances=len(getattr(res, "instances", None) or []),
            )
        self._log_cmd(row)

    # -- frames --------------------------------------------------------------------------------------------------------
    def _render(self, camera_id: str, store_id: str, cam: Any) -> bytes | None:
        raw = getattr(self.backend, "render_rgb", None)
        if raw is not None:
            rgb = raw(camera_id, store_id, cam)
            return _encode(rgb, self.quality) if rgb is not None else None
        out = self.backend.render(camera_id, store_id, cam)
        return out[0] if out else None

    def capture(self) -> None:
        """One director frame (+ PiP) for the current sim state; call right after ``core.tick(self.dt)``."""
        if not self.active or self.dir is None or self.director is None:
            return
        rover = self.core.rovers.get(self.robot_id or "")
        if rover is None:
            return
        t0 = time.monotonic()
        active = rover.active
        mode: Any = "unloading" if type(active).__name__ == "_Unload" else rover.status
        cam = self.director.update(self.dt, rover.pose, mode, rover.lift)
        name = f"{self.n:06d}.jpg"
        jpeg = self._render(f"director.{rover.robot_id}", rover.store_id, cam)
        if jpeg is not None:
            (self.dir / "director" / name).write_bytes(jpeg)
        pip = bool(self.pip_every) and self.n % self.pip_every == 0
        if pip:
            for kind in ("front", "wrist"):
                cid = f"{rover.robot_id}.{kind}"
                try:
                    j = self._render(cid, rover.store_id, self.core.camera_pose(cid))
                except Exception as exc:  # noqa: BLE001 - PiP is best effort
                    log.debug("recorder pip %s failed: %s", cid, exc)
                    j = None
                if j is not None:
                    (self.dir / kind / name).write_bytes(j)
        self._t_render += time.monotonic() - t0
        row = {
            "n": self.n,
            "sim_t": round(self.core.sim_time, 4),
            "wall": round(time.time(), 4),
            "shot": self.director.shot.kind,
            "mode": str(getattr(mode, "value", mode)),
            "pose": [round(rover.pose.x, 3), round(rover.pose.y, 3), round(rover.pose.theta, 4)],
            "lift": round(rover.lift, 3),
            "pan": round(rover.pan, 3),
            "holding": rover.holding or rover.carry,
            "basket": len(rover.basket),
            "cmd": getattr(getattr(active, "cmd", None), "type", None),
            "pip": pip,
            "ok": jpeg is not None,
            "eye": [round(v, 3) for v in cam.basis()[2]] and [round(cam.x, 3), round(cam.y, 3), round(cam.z, 3)],
        }
        if self._frames_f is not None:
            self._frames_f.write(json.dumps(row) + "\n")
        self.n += 1
        if self.n % 150 == 0:
            log.info(
                "recorder: %d frames, %.0f ms/frame render+write (sim %.1fs)",
                self.n,
                1000 * self._t_render / 150,
                self.core.sim_time,
            )
            self._t_render = 0.0


def recorder_from_env(core: WorkerCore, backend: SimBackend, path: str | None) -> Recorder | None:
    path = path or os.environ.get("PICKY_SIM_RECORD_CONTROL") or ""
    if not path:
        return None
    log.info("recorder: polling %s", path)
    return Recorder(core, backend, path)


__all__ = ["Recorder", "recorder_from_env", "math"]
