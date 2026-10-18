"""The rover's mast screen in the MuJoCo store: a live texture showing what the robot is doing.

A daemon thread polls the gateway (``GET /api/fleet/robots/{id}``, the same state the web app's robot screen page
shows) every ``POLL_S`` seconds; the backend redraws each rover's texture about once a second from that plus the
worker's own state (status, holding, basket) and re-uploads it to every GL context (offscreen renderers, the window).
No gateway -> the screen still shows the worker's local state.
"""

import json
import textwrap
import threading
import time
import urllib.request
from typing import Any

import numpy as np

SCREEN_W, SCREEN_H = 512, 320
SCREEN_SIZE_M = (0.26, 0.165)  # 12" class display
SCREEN_LOCAL = (-0.29, 0.0, 1.02)  # rover frame: rear post, facing backwards at shopper chest height
POLL_S = 2.0
REDRAW_S = 1.0


class ScreenFeed:
    """Background poller: robot_id -> latest RobotInfo dict from the gateway."""

    def __init__(self, gateway_url: str | None, robot_ids: list[str]) -> None:
        self.url = (gateway_url or "").rstrip("/")
        self.robot_ids = list(robot_ids)
        self.info: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, list[dict[str, Any]]] = {}
        self._stop = threading.Event()
        if self.url:
            threading.Thread(target=self._run, name="screen-feed", daemon=True).start()

    def _run(self) -> None:
        while not self._stop.is_set():
            for rid in self.robot_ids:
                try:
                    with urllib.request.urlopen(f"{self.url}/api/fleet/robots/{rid}", timeout=1.5) as r:
                        self.info[rid] = json.load(r)
                    with urllib.request.urlopen(f"{self.url}/api/approvals?robot_id={rid}", timeout=1.5) as r:
                        self.approvals[rid] = json.load(r)
                except Exception:  # noqa: BLE001 - offline gateway: keep the last state
                    pass
            self._stop.wait(POLL_S)

    def stop(self) -> None:
        self._stop.set()


def draw(name: str, color: str, local: dict[str, Any], info: dict[str, Any] | None) -> np.ndarray:
    """RGB uint8 (H, W, 3) screen image."""
    from PIL import Image, ImageDraw, ImageFont

    def font(px: int):
        try:
            return ImageFont.load_default(size=px)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()

    img = Image.new("RGB", (SCREEN_W, SCREEN_H), (14, 20, 28))
    d = ImageDraw.Draw(img)
    accent = (
        tuple(int(color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)) if color.startswith("#") else (60, 180, 120)
    )
    d.rectangle((0, 0, SCREEN_W, 56), fill=accent)
    d.text((18, 28), f"Hi, I'm {name}", fill=(255, 255, 255), font=font(30), anchor="lm")
    status = str((info or {}).get("status") or local.get("status") or "idle").replace("_", " ")
    battery = (info or {}).get("battery_pct", local.get("battery"))
    right = f"{status}" + (f"  ·  {battery:.0f}%" if isinstance(battery, (int, float)) else "")
    d.text((SCREEN_W - 16, 28), right, fill=(255, 255, 255), font=font(20), anchor="rm")
    y = 74
    holding = local.get("holding")
    basket = local.get("basket", 0)
    d.text(
        (18, y),
        f"Basket: {basket} item{'s' if basket != 1 else ''}" + (f"   Holding: {holding}" if holding else ""),
        fill=(190, 205, 220),
        font=font(20),
    )
    y += 34
    said = (info or {}).get("last_thought") or (info or {}).get("error") or ""
    pending = local.get("approvals") or []
    checkout = next((a for a in pending if a.get("kind") == "checkout"), None)
    ask = checkout or (pending[0] if pending else None)
    if ask is not None:
        box = (12, y - 4, SCREEN_W - 12, SCREEN_H - 52)
        d.rounded_rectangle(box, radius=12, fill=(250, 250, 245) if checkout else (36, 52, 70))
        ink = (20, 24, 30) if checkout else (255, 255, 255)
        head = "Ready to check out" if checkout else "Quick question"
        d.text((26, y + 6), head, fill=(200, 40, 50) if checkout else (170, 230, 190), font=font(22))
        amt = ask.get("amount_cents")
        if isinstance(amt, int):
            d.text((SCREEN_W - 26, y + 6), f"${amt // 100}.{amt % 100:02d}", fill=ink, font=font(30), anchor="ra")
        for i, line in enumerate(textwrap.wrap(str(ask.get("title") or ""), 38)[:3]):
            d.text((26, y + 42 + i * 28), line, fill=ink, font=font(21))
        d.text(
            (26, box[3] - 30),
            "Approve on my screen page or in the app",
            fill=(110, 120, 130) if checkout else (170, 185, 200),
            font=font(18),
        )
    elif said:
        for line in textwrap.wrap(str(said), 40)[:5]:
            d.text((18, y), line, fill=(255, 255, 255), font=font(22))
            y += 30
    else:
        d.text((18, y), "Ask me where anything is.", fill=(255, 255, 255), font=font(26))
        y += 36
        d.text((18, y), "I can walk you to the shelf or pick it for you.", fill=(190, 205, 220), font=font(20))
    d.rectangle((0, SCREEN_H - 44, SCREEN_W, SCREEN_H), fill=(30, 40, 52))
    d.text(
        (18, SCREEN_H - 22), f"Talk to me: Picky app › Robot › {name}", fill=(170, 230, 190), font=font(19), anchor="lm"
    )
    return np.asarray(img, dtype=np.uint8)


class ScreenTicker:
    def __init__(self) -> None:
        self.last = 0.0

    def due(self) -> bool:
        now = time.monotonic()
        if now - self.last < REDRAW_S:
            return False
        self.last = now
        return True
