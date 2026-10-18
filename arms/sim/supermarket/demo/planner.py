"""Gemini route planner for the --demo autopilot: the rover's navigation brain.

The app builds a store map (sections, obstacles, robot start, checkout) and hands it plus the shopping list to Gemini,
which splits the list between the robots, orders each robot's stops and says why ("drinks are on the south wall right
by the start..."). The rovers then execute the plan with A* between stops; when a stop fails (no free approach pose,
product not where the map said) the app calls `replan` with what happened and Gemini re-orders what's left.

Plain urllib (Isaac Sim's python has no requests). The key comes from GEMINI_API_KEY or ~/.config/picky/secrets.env
and is never logged. Every request/response is appended to the log file (JSONL) without the key.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

MODELS = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-flash-latest"]
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM = (
    "You are the navigation brain of Picky, a fleet of shopping rovers in a supermarket. Each rover is a 4-wheel "
    "swerve base (can drive in any direction) with a 6-DoF arm and a basket. You get a store map (sections with "
    "bounding boxes, obstacles, rover start poses, the checkout counter) and a shopping list where every item "
    "already has the section it is stocked in and the aisle side a rover must stand on to pick it. Plan the trip: "
    "assign every item to exactly one rover (balance the work and keep each rover in its own part of the store so "
    "they don't block each other), order each rover's stops to keep driving short (think about which aisles connect), "
    "and end every route at the checkout. Explain each leg in one short, concrete sentence a shopper would understand "
    "(mention the section and why it comes next). Answer with JSON only."
)

SCHEMA_HINT = {
    "summary": "one or two sentences: the overall strategy",
    "robots": [
        {
            "robot": "<robot name>",
            "stops": [{"item": "<item id>", "section": "<section id>", "side": "<north|south|east|west>",
                       "reason": "<why this stop comes next, <= 20 words>"}],
            "checkout_reason": "<why/when it heads to the checkout>",
        }
    ],
}  # fmt: skip


def load_key() -> str | None:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    f = Path.home() / ".config" / "picky" / "secrets.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


class Planner:
    def __init__(self, log_path: str, models=MODELS):
        self.key = load_key()
        self.models = list(models)
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_used = None

    def _log(self, rec: dict) -> None:
        with self.log_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def _call(self, prompt: str, kind: str) -> dict:
        if not self.key:
            raise RuntimeError("GEMINI_API_KEY not set (env or ~/.config/picky/secrets.env)")
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
        }
        last = None
        for model in self.models:
            for attempt in range(2):
                t0 = time.time()
                req = urllib.request.Request(
                    URL.format(model=model) + "?key=" + self.key,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(req, timeout=90) as r:
                        resp = json.loads(r.read())
                    text = "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])
                    out = json.loads(text[text.find("{") : text.rfind("}") + 1])
                    self.model_used = model
                    self._log({"t": time.time(), "kind": kind, "model": model, "latency_s": round(time.time() - t0, 2),
                               "prompt": prompt, "response": out})  # fmt: skip
                    return out
                except urllib.error.HTTPError as e:
                    last = f"{model}: HTTP {e.code}"
                    self._log({"t": time.time(), "kind": kind, "model": model, "error": last})
                    if e.code in (404, 400):
                        break  # model not available -> next model
                    time.sleep(3 * (attempt + 1))
                except Exception as e:  # noqa: BLE001  (timeouts, malformed JSON)
                    last = f"{model}: {type(e).__name__}: {e}"
                    self._log({"t": time.time(), "kind": kind, "model": model, "error": last})
                    time.sleep(2)
        raise RuntimeError(f"all Gemini models failed ({last})")

    def plan(self, store: dict, robots: list[dict], items: list[dict], checkout: dict) -> dict:
        prompt = (
            "STORE MAP\n" + json.dumps(store, separators=(",", ":")) + "\n\nROVERS\n" + json.dumps(robots)
            + "\n\nCHECKOUT COUNTER\n" + json.dumps(checkout) + "\n\nSHOPPING LIST\n" + json.dumps(items)
            + "\n\nReturn JSON shaped like:\n" + json.dumps(SCHEMA_HINT)
        )  # fmt: skip
        return self._call(prompt, "plan")

    def replan(self, store: dict, robot: dict, remaining: list[dict], event: str, checkout: dict) -> dict:
        prompt = (
            "STORE MAP\n" + json.dumps(store, separators=(",", ":")) + "\n\nREPLAN for rover " + robot["name"]
            + f" now at {robot['pose']}.\nWHAT HAPPENED: {event}\n\nITEMS STILL TO PICK (with alternatives)\n"
            + json.dumps(remaining) + "\n\nCHECKOUT COUNTER\n" + json.dumps(checkout)
            + "\n\nRe-order this rover's remaining stops (you may switch an item to one of its listed alternatives "
            "by using the alternative's id, or drop an item if nothing works). Return JSON shaped like:\n"
            + json.dumps({"summary": "...", "robots": [SCHEMA_HINT["robots"][0]]})
        )  # fmt: skip
        return self._call(prompt, "replan")
