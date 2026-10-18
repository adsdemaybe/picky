"""Policy sidecar for ``teleop_app.py --demo --policy perceptron``: Perceptron Mk1.5 over localhost.

Isaac Sim's python has no httpx, and Perceptron's CDN refuses urllib's default client (HTTP 403), so the Isaac app
talks to this small server on 127.0.0.1 (stdlib urllib is fine there) and the server talks to Perceptron with httpx
and explicit headers, exactly like the live policy (``arms/robot/vla_mk15.Mk15Client``). Run it in the repo's uv venv:

    uv run python arms/sim/supermarket/demo/vla_sidecar.py --port 8766 --log RUN/vla_calls.jsonl

    POST /act  {"text": <user text>, "images": [mast b64, wrist b64], "tag": ...}
               -> {"tool", "args", "text", "latency_ms", "model", "attempts"}: one policy step (TOOLS,
                  tool_choice auto; a text answer gets one "You must call one of the tools now." retry, as live)
    POST /ask  {"prompt": ..., "images": [b64, ...], "tag": ...} -> {"text", "verdict": true|false|null, ...}
               (verifier / look-out questions)
    POST /detect {"classes": [...], "image": b64, "tag": ...} -> {"text", "boxes": [[x1, y1, x2, y2], ...]} (0-1000)
               (the live verifier's detect call: segment system prompt, box annotations, reasoning_effort none)
    GET /health -> {"ok", "model", "key", "calls"}

The key comes from PERCEPTRON_API_KEY (env, e.g. ``source ~/.config/picky/secrets.env``) and is never logged; the log
holds tags, tool calls, answers and latencies, never images. Every call counts against --budget.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vla_policy import (  # noqa: E402
    BASE_URL,
    DETECT_SYSTEM,
    MODEL,
    NUDGE_RETRY,
    TOOLS,
    answer_text,
    first_tool_call,
    messages,
    parse_boxes,
    yes_no,
)


class Mk15:
    def __init__(self, key: str, base_url: str, model: str, timeout_s: float, budget: int, log: Path | None) -> None:
        self.key, self.base_url, self.model = key, base_url.rstrip("/"), model
        self.http = httpx.Client(
            timeout=timeout_s, headers={"User-Agent": "picky-robot/0.2 (+httpx)", "Accept": "application/json"}
        )
        self.budget = budget
        self.calls = 0
        self.lat: list[float] = []
        self.lock = threading.Lock()
        self.log = log

    def _log(self, rec: dict) -> None:
        if self.log is None:
            return
        with self.lock, self.log.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), **rec}) + "\n")

    def chat(self, msgs: list[dict], tools: list[dict] | None, tag: str, extra: dict | None = None) -> dict:
        if not self.key:
            raise RuntimeError("PERCEPTRON_API_KEY not set")
        body: dict = {"model": self.model, "messages": msgs, "max_completion_tokens": 300, **(extra or {})}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"  # 'required' is rejected by the API
        last = ""
        for attempt in range(3):
            with self.lock:
                if self.calls >= self.budget:
                    raise RuntimeError(f"Perceptron call budget spent ({self.budget})")
                self.calls += 1
            t0 = time.monotonic()
            try:
                r = self.http.post(
                    f"{self.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {self.key}"}
                )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                self._log({"tag": tag, "error": last, "attempt": attempt})
                time.sleep(1.5 * (attempt + 1))
                continue
            ms = round((time.monotonic() - t0) * 1000, 1)
            self.lat.append(ms)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                self._log({"tag": tag, "error": last, "latency_ms": ms, "attempt": attempt})
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Perceptron HTTP {r.status_code}: {r.text[:200]}")
            out = r.json()
            out["_latency_ms"] = ms
            return out
        raise RuntimeError(f"Perceptron unavailable ({last})")

    def act(self, req: dict) -> dict:
        tag = str(req.get("tag", ""))
        t0 = time.monotonic()
        call, text, attempts = None, "", 0
        for attempt in range(2):
            msgs = messages(str(req["text"]), req.get("images") or [])
            if attempt:
                msgs.append({"role": "user", "content": NUDGE_RETRY})
            resp = self.chat(msgs, TOOLS, tag)
            attempts += 1
            call = first_tool_call(resp)
            text = answer_text(resp)
            if call is not None:
                break
        out = {
            "tool": call[0] if call else None,
            "args": call[1] if call else {},
            "text": text[:300],
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "model": self.model,
            "attempts": attempts,
            "calls": self.calls,
        }
        self._log({"tag": tag, "kind": "act", **{k: v for k, v in out.items() if k != "calls"}})
        return out

    def ask(self, req: dict) -> dict:
        tag = str(req.get("tag", ""))
        t0 = time.monotonic()
        content = [{"type": "text", "text": str(req["prompt"])}]
        for b in req.get("images") or []:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}})
        resp = self.chat([{"role": "user", "content": content}], None, tag)
        text = answer_text(resp)
        out = {
            "text": text[:300],
            "verdict": yes_no(text),
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "model": self.model,
            "calls": self.calls,
        }
        self._log({"tag": tag, "kind": "ask", "prompt": str(req["prompt"])[:200], **out})
        return out

    def detect(self, req: dict) -> dict:
        tag = str(req.get("tag", ""))
        t0 = time.monotonic()
        cats = ", ".join(str(c).replace(",", " ").strip() for c in req.get("classes") or [] if str(c).strip())
        msgs = [
            {"role": "system", "content": DETECT_SYSTEM.format(categories=cats)},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + req["image"]}}],
            },
        ]
        extra = {"reasoning_effort": "none", "vision_config": {"annotation_format": "box"}}
        resp = self.chat(msgs, None, tag, extra)
        text = answer_text(resp)
        out = {
            "text": text[:600],
            "boxes": [list(b) for b in parse_boxes(text)],
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "model": self.model,
            "calls": self.calls,
        }
        self._log({"tag": tag, "kind": "detect", "classes": cats, **out})
        return out


def make_handler(mk: Mk15):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # quiet: the JSONL log has the calls
            return

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/health"):
                lat = sorted(mk.lat)
                self._send(
                    200,
                    {
                        "ok": True,
                        "model": mk.model,
                        "key": bool(mk.key),
                        "calls": mk.calls,
                        "budget": mk.budget,
                        "latency_ms_median": lat[len(lat) // 2] if lat else None,
                    },
                )
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            try:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/act"):
                    self._send(200, mk.act(req))
                elif self.path.startswith("/ask"):
                    self._send(200, mk.ask(req))
                elif self.path.startswith("/detect"):
                    self._send(200, mk.detect(req))
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001 - the Isaac app decides what a failed step means
                mk._log({"tag": "error", "error": f"{type(exc).__name__}: {exc}"[:300]})
                self._send(503, {"error": f"{type(exc).__name__}: {exc}"[:300]})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--model", default=os.environ.get("PERCEPTRON_MODEL", MODEL))
    ap.add_argument("--base-url", default=os.environ.get("PERCEPTRON_BASE_URL", BASE_URL))
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--budget", type=int, default=600, help="max Perceptron calls for this sidecar's lifetime")
    a = ap.parse_args()
    key = os.environ.get("PERCEPTRON_API_KEY", "").strip()
    if a.log:
        a.log.parent.mkdir(parents=True, exist_ok=True)
    mk = Mk15(key, a.base_url, a.model, a.timeout, a.budget, a.log)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(mk))
    print(f"[vla-sidecar] {a.model} on 127.0.0.1:{a.port} (key {'set' if key else 'MISSING'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
