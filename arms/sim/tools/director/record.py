"""Record one end-to-end run through the gateway for the director video: every ``/ws`` event, every live camera
frame of the robot (``/ws/frames``: front / wrist, incl. the T1 policy pictures while the arm moves), and the
shopper's approvals (answered over REST like the web app would).

    uv run --frozen --no-sync python arms/sim/tools/director/record.py --base http://localhost:8101 \\
        --text "tacos for 6, under \\$40, one vegetarian" --user maya --out /data/.../run1

Output in ``--out``: ``events.jsonl`` ({"t": epoch_s, "e": event}), ``frames/<camera_id>/<epoch_ms>.jpg`` (blurred
frames as served), ``state.jsonl`` (robot.state at full rate), ``approvals.jsonl``, ``request.json``,
``trips/<trip_id>.json`` (final), ``summary.json``. Talks to the gateway over HTTP / WS only (no arm imports).

Approvals: checkout -> approve (echoing ``cart_hash``); pick_choice -> the first option; recommendation / multi
"want any?" -> ``--recs`` (default reject, to keep the run short); everything else -> approve.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import websockets

TERMINAL_TRIP = {"paid", "followup", "done", "failed"}
TERMINAL_REQUEST = {"completed", "failed", "rejected", "needs_input"}


class Recorder:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base = args.base.rstrip("/")
        self.ws_base = "ws" + self.base[len("http") :]
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "frames").mkdir(exist_ok=True)
        self.events = (self.out / "events.jsonl").open("a")
        self.state = (self.out / "state.jsonl").open("a")
        self.approvals = (self.out / "approvals.jsonl").open("a")
        self.http = httpx.AsyncClient(base_url=self.base, timeout=30.0)
        self.answered: set[str] = set()
        self.stop = asyncio.Event()
        self.counts: dict[str, int] = {}
        self.frame_counts: dict[str, int] = {}
        self.request_id: str | None = None
        self.trip_ids: list[str] = []

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # ---- sockets ------------------------------------------------------------------------------------------------
    async def events_ws(self, ready: asyncio.Event) -> None:
        while not self.stop.is_set():
            try:
                async with websockets.connect(f"{self.ws_base}/ws", max_size=2**25, open_timeout=20) as ws:
                    ready.set()
                    async for raw in ws:
                        t = time.time()
                        try:
                            ev = json.loads(raw)
                        except ValueError:
                            continue
                        self.events.write(json.dumps({"t": t, "e": ev}) + "\n")
                        kind = ev.get("type", "?")
                        self.counts[kind] = self.counts.get(kind, 0) + 1
                        if kind == "approval.requested":
                            asyncio.create_task(self.answer(ev.get("request") or ev.get("approval") or {}))
                        elif kind in ("step.failed", "plan.completed", "vla.tier_changed", "trip.state_changed"):
                            self.log(f"{kind}: {self._brief(ev)}")
                        if self.stop.is_set():
                            break
            except Exception as exc:  # noqa: BLE001 - reconnect
                if self.stop.is_set():
                    break
                self.log(f"/ws error {exc!r}; reconnecting")
                await asyncio.sleep(2)

    async def frames_ws(self) -> None:
        url = f"{self.ws_base}/ws/frames?robot_id={self.args.robot}"
        while not self.stop.is_set():
            try:
                async with websockets.connect(url, max_size=2**25, open_timeout=20) as ws:
                    async for raw in ws:
                        t = time.time()
                        try:
                            ev = json.loads(raw)
                        except ValueError:
                            continue
                        kind = ev.get("type")
                        if kind == "camera.frame":
                            frame = ev.get("frame") or {}
                            cam = ev.get("camera_id") or frame.get("camera_id") or "unknown"
                            if not frame.get("blurred"):
                                continue  # never keep an unblurred frame (the gateway only sends blurred ones)
                            d = self.out / "frames" / cam
                            d.mkdir(parents=True, exist_ok=True)
                            (d / f"{int(t * 1000)}.jpg").write_bytes(base64.b64decode(frame.get("jpeg_b64", "")))
                            self.frame_counts[cam] = self.frame_counts.get(cam, 0) + 1
                        elif kind == "robot.state":
                            self.state.write(json.dumps({"t": t, "e": ev}) + "\n")
                        if self.stop.is_set():
                            break
            except Exception as exc:  # noqa: BLE001 - reconnect
                if self.stop.is_set():
                    break
                self.log(f"/ws/frames error {exc!r}; reconnecting")
                await asyncio.sleep(2)

    @staticmethod
    def _brief(ev: dict[str, Any]) -> str:
        if ev.get("type") == "step.failed":
            d = ev.get("decision") or {}
            return f"{ev.get('step_id')} -> {d.get('action')}: {d.get('reason', '')[:160]}"
        if ev.get("type") == "plan.completed":
            return f"{ev.get('plan_id')} {ev.get('status')} {str(ev.get('summary', ''))[:160]}"
        if ev.get("type") == "vla.tier_changed":
            return f"{ev.get('from_tier')} -> {ev.get('to_tier')}: {ev.get('reason', '')[:160]}"
        return f"{ev.get('trip_id')} {ev.get('from_state')} -> {ev.get('to_state')} {ev.get('reason', '')[:120]}"

    # ---- approvals ----------------------------------------------------------------------------------------------
    async def answer(self, req: dict[str, Any]) -> None:
        aid = req.get("approval_id")
        if not aid or aid in self.answered:
            return
        self.answered.add(aid)
        await asyncio.sleep(self.args.answer_delay_s)
        kind = req.get("kind")
        options = req.get("options") or []
        body: dict[str, Any] = {"decision": "approve", "user_id": req.get("user_id"), "channel": "web"}
        if kind == "checkout":
            body["cart_hash"] = req.get("cart_hash")
        elif kind == "pick_choice" and options:
            body = {**body, "decision": "choice", "choice": options[0]["option_id"]}
            if req.get("multi"):
                body["choices"] = [options[0]["option_id"]]
        elif kind == "recommendation" or (req.get("multi") and options):
            if self.args.recs == "reject":
                body["decision"] = "reject"
            elif options:
                body = {**body, "decision": "choice", "choices": [o["option_id"] for o in options[:1]]}
        try:
            r = await self.http.post(f"/api/approvals/{aid}", json=body)
            status = r.status_code
        except Exception as exc:  # noqa: BLE001
            status = repr(exc)
        rec = {
            "t": time.time(),
            "approval_id": aid,
            "kind": kind,
            "title": req.get("title"),
            "answer": body,
            "status": status,
        }
        self.approvals.write(json.dumps(rec) + "\n")
        self.approvals.flush()
        self.log(f"approval {kind} '{req.get('title')}' -> {body['decision']} ({status})")

    async def sweep_approvals(self) -> None:
        """Belt and braces: answer pending approvals the socket might have missed."""
        while not self.stop.is_set():
            await asyncio.sleep(5)
            try:
                r = await self.http.get("/api/approvals")
                for req in r.json():
                    if req.get("approval_id") not in self.answered:
                        asyncio.create_task(self.answer(req))
            except Exception:  # noqa: BLE001
                pass

    # ---- the run ------------------------------------------------------------------------------------------------
    async def watch(self, started: float) -> None:
        done_at: float | None = None
        while not self.stop.is_set():
            await asyncio.sleep(5)
            self.events.flush()
            self.state.flush()
            try:
                plan = (await self.http.get(f"/api/requests/{self.request_id}")).json()
                self.trip_ids = list(plan.get("trip_ids") or self.trip_ids)
                states = []
                for tid in self.trip_ids:
                    trip = (await self.http.get(f"/api/trips/{tid}")).json()
                    states.append(trip.get("state"))
                req_status = plan.get("status")
            except Exception as exc:  # noqa: BLE001
                self.log(f"watch error {exc!r}")
                continue
            elapsed = time.time() - started
            if int(elapsed) % 30 < 5:
                self.log(f"t+{elapsed:.0f}s request={req_status} trips={states} frames={self.frame_counts}")
            finished = (self.trip_ids and all(s in TERMINAL_TRIP for s in states)) or req_status in TERMINAL_REQUEST
            if finished and done_at is None:
                done_at = time.time()
                self.log(f"finished: request={req_status} trips={states}; recording {self.args.tail_s}s more")
            if done_at is not None and time.time() - done_at > self.args.tail_s:
                break
            if elapsed > self.args.timeout:
                self.log("timeout")
                break
        self.stop.set()

    async def run(self) -> int:
        ready = asyncio.Event()
        tasks = [asyncio.create_task(self.events_ws(ready))]
        if self.args.live_frames:  # off by default: every live stream is one more render product on the worker
            tasks.append(asyncio.create_task(self.frames_ws()))
        await asyncio.wait_for(ready.wait(), 30)
        await asyncio.sleep(1.0)
        tasks.append(asyncio.create_task(self.sweep_approvals()))
        try:  # the store layout for the minimap (served by the catalog: slot positions from the database)
            store = self.args.store or "store_ave"
            plan = (await self.http.get(f"/api/stores/{store}/planogram")).json()
            (self.out / "planogram.json").write_text(json.dumps(plan))
        except Exception as exc:  # noqa: BLE001
            self.log(f"planogram fetch failed: {exc!r}")
        started = time.time()
        body = {"text": self.args.text, "user_id": self.args.user}
        if self.args.store:
            body["store_id"] = self.args.store
        r = await self.http.post("/api/requests", json=body, timeout=120.0)
        plan = r.json()
        (self.out / "request.json").write_text(json.dumps(plan, indent=1))
        self.request_id = plan.get("request_id")
        self.trip_ids = list(plan.get("trip_ids") or [])
        self.log(
            f"POST /api/requests -> {r.status_code} request={self.request_id} status={plan.get('status')} "
            f"trips={self.trip_ids} reply={str(plan.get('reply', ''))[:200]}"
        )
        await self.watch(started)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        (self.out / "trips").mkdir(exist_ok=True)
        for tid in self.trip_ids:
            try:
                (self.out / "trips" / f"{tid}.json").write_text((await self.http.get(f"/api/trips/{tid}")).text)
            except Exception:  # noqa: BLE001
                pass
        try:
            final = (await self.http.get(f"/api/requests/{self.request_id}")).json()
            (self.out / "request_final.json").write_text(json.dumps(final, indent=1))
        except Exception:  # noqa: BLE001
            final = {}
        summary = {
            "started": started,
            "ended": time.time(),
            "request_id": self.request_id,
            "trip_ids": self.trip_ids,
            "event_counts": self.counts,
            "frame_counts": self.frame_counts,
            "status": final.get("status"),
        }
        (self.out / "summary.json").write_text(json.dumps(summary, indent=1))
        self.log(f"summary: {json.dumps(summary)[:600]}")
        for f in (self.events, self.state, self.approvals):
            f.close()
        await self.http.aclose()
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://localhost:8101")
    ap.add_argument("--text", required=True)
    ap.add_argument("--user", default="maya")
    ap.add_argument("--store", default=None)
    ap.add_argument("--robot", default="rover-ave-1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--recs", choices=["approve", "reject"], default="reject")
    ap.add_argument("--live-frames", action="store_true", help="also record /ws/frames (asks the worker for streams)")
    ap.add_argument("--answer-delay-s", type=float, default=1.5)
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--tail-s", type=float, default=8.0)
    return asyncio.run(Recorder(ap.parse_args()).run())


if __name__ == "__main__":
    raise SystemExit(main())
