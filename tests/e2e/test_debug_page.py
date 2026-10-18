"""Browser end-to-end test of the gateway's /debug page (Playwright, Chromium).

Starts a real gateway (all fakes unless PICKY_REAL_ARMS says otherwise) on a free port, opens /debug, sends the taco
request in natural language plus a staff shelf scan at the second store, and checks that the page shows what the
web app needs: three robots in two stores that actually move, goals, the robot brains' thinking log, and camera frames
(JPEG snapshots and a live MJPEG stream) that really decode (``naturalWidth > 0``). The dev server's approvals wait
for a person (no scripted shopper unless ``PICKY_SCRIPTED_SHOPPER=1``), so the test answers them by clicking the
page's approval cards, checkout payment included. Screenshots go to ``tests/e2e/artifacts/``.

Opt-in only (CI does not install the e2e group):

    uv sync --group e2e && uv run --group e2e playwright install chromium
    PICKY_E2E=1 uv run --group e2e pytest tests/e2e -s
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

if os.environ.get("PICKY_E2E") != "1":
    pytest.skip("browser e2e: set PICKY_E2E=1 (and install the e2e group)", allow_module_level=True)

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
SHOP_TEXT = "tacos for 6, under $40, one vegetarian"
HILL_TEXT = "Hazel, scan the dairy aisle at the Hill store"
BOOT_TIMEOUT_S = 40.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def gateway_url() -> Iterator[str]:
    port = _free_port()
    log_path = ARTIFACTS / "gateway.log"
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PICKY_LOG_LEVEL": os.environ.get("PICKY_LOG_LEVEL", "WARNING"), "PICKY_SCRIPTED_SHOPPER": "0"}
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "arms.gateway.app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + BOOT_TIMEOUT_S
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"gateway exited with {proc.returncode}; see {log_path}")
                with contextlib.suppress(httpx.HTTPError):
                    if httpx.get(f"{url}/health", timeout=1.0).status_code == 200:
                        break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"gateway did not come up in {BOOT_TIMEOUT_S:.0f}s; see {log_path}")
                time.sleep(0.25)
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture()
def page(gateway_url: str) -> Iterator[object]:
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1680, "height": 1050}, color_scheme="light")
        pg = context.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda exc: errors.append(str(exc)))
        pg.errors = errors  # type: ignore[attr-defined]
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def _shot(page: object, name: str, *, full: bool = True) -> Path:
    path = ARTIFACTS / name
    page.screenshot(path=str(path), full_page=full)  # type: ignore[attr-defined]
    print(f"screenshot: {path}")
    return path


def _robot_xy(page: object, name: str) -> tuple[float, float] | None:
    """Centre of a robot's marker on the store map (SVG <g><title>Name (status)</title><circle .../></g>)."""
    return page.evaluate(  # type: ignore[attr-defined]
        """(name) => {
            for (const g of document.querySelectorAll('#map g')) {
                const t = g.querySelector('title');
                const c = g.querySelector('circle');
                if (t && c && t.textContent.startsWith(name + ' '))
                    return [Number(c.getAttribute('cx')), Number(c.getAttribute('cy'))];
            }
            return null;
        }""",
        name,
    )


def _images_loaded(page: object, selector: str) -> dict[str, int]:
    return page.evaluate(  # type: ignore[attr-defined]
        """(sel) => {
            const imgs = [...document.querySelectorAll(sel)];
            return { total: imgs.length, loaded: imgs.filter(i => i.complete && i.naturalWidth > 0).length };
        }""",
        selector,
    )


def _answer_approvals(page: object) -> list[str]:
    """Click the page's approval cards like the shopper would: NO to a detour that busts the budget, the first option
    (the freshest) for a pick, Approve for everything else (incl. the checkout payment). Returns the answered titles."""
    answered: list[str] = []
    cards = page.locator("#approvals .appr")  # type: ignore[attr-defined]
    for i in range(cards.count()):
        card = cards.nth(i)
        try:
            title = card.locator(".t").inner_text(timeout=500)
            buttons = card.locator("button")
            button = buttons.filter(has_text="No").first if "over your" in title else buttons.first
            button.click(timeout=1_000)
            answered.append(title)
        except sync_api.Error:
            continue  # the list re-rendered under us; the next poll gets it
    return answered


def _wait_answering(page: object, predicate_js: str, timeout_s: float, answered: list[str]) -> bool:
    """Poll ``predicate_js`` while answering approval cards (the robot brain blocks on them)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        answered.extend(_answer_approvals(page))
        if page.evaluate(predicate_js):  # type: ignore[attr-defined]
            return True
        page.wait_for_timeout(250)  # type: ignore[attr-defined]
    return False


def test_debug_page_shows_fleet_brains_and_cameras(page: object, gateway_url: str) -> None:  # noqa: PLR0915
    pg = page
    pg.goto(f"{gateway_url}/debug")  # type: ignore[attr-defined]

    # --- boot: three robots across two stores, sim pill, cameras that decode
    pg.wait_for_function("document.querySelectorAll('#robots .robot').length === 3", timeout=15_000)  # type: ignore[attr-defined]
    robots_text = pg.inner_text("#robots")  # type: ignore[attr-defined]
    for name in ("Pico", "Rhea", "Hazel", "store_ave", "store_hill"):
        assert name in robots_text, f"{name} missing from the fleet panel: {robots_text!r}"
    sim_pill = (pg.text_content("#sim") or "").lower()  # type: ignore[attr-defined]
    assert "2d fake" in sim_pill or "isaac" in sim_pill, sim_pill
    pg.wait_for_function(  # type: ignore[attr-defined]
        "(() => { const i = [...document.querySelectorAll('#cams img')];"
        " return i.length >= 3 && i.every(x => x.complete && x.naturalWidth > 0); })()",
        timeout=20_000,
    )
    cams = _images_loaded(pg, "#cams img")
    print(f"camera snapshots decoded: {cams}")
    _shot(pg, "01_boot.png")

    # --- shopper: natural-language request -> fleet -> Pico's brain
    pg.fill("#text", SHOP_TEXT)  # type: ignore[attr-defined]
    pg.click("#send")  # type: ignore[attr-defined]
    pg.wait_for_function("document.getElementById('reply').textContent.startsWith('executing')", timeout=15_000)  # type: ignore[attr-defined]
    print("reply:", pg.inner_text("#reply"))  # type: ignore[attr-defined]

    # --- staff at the second store: a shelf scan on Hazel at the same time
    pg.select_option("#role", "staff")  # type: ignore[attr-defined]
    pg.fill("#text", HILL_TEXT)  # type: ignore[attr-defined]
    pg.click("#send")  # type: ignore[attr-defined]
    pg.wait_for_function("document.getElementById('reply').textContent.includes('Hazel')", timeout=15_000)  # type: ignore[attr-defined]
    print("staff reply:", pg.inner_text("#reply"))  # type: ignore[attr-defined]

    # --- goals table: the shop goal (Pico) and the shelf scan (Hazel)
    pg.wait_for_function(  # type: ignore[attr-defined]
        "(() => { const t = document.getElementById('goals').innerText;"
        " return t.includes('shop') && t.includes('shelf_scan') && t.includes('Pico') && t.includes('Hazel'); })()",
        timeout=15_000,
    )

    # --- the brains think out loud (brain.step over /ws)
    pg.wait_for_function("document.querySelectorAll('#brain div').length >= 15", timeout=30_000)  # type: ignore[attr-defined]
    brain = pg.inner_text("#brain")  # type: ignore[attr-defined]
    assert "Pico" in brain and "tool_call" in brain, brain[:500]

    # --- approvals wait for a person: the first card stays until it is clicked (no scripted shopper on the server)
    answered: list[str] = []
    pg.wait_for_function("document.querySelectorAll('#approvals .appr').length >= 1", timeout=60_000)  # type: ignore[attr-defined]
    first_card = pg.inner_text("#approvals .appr .t")  # type: ignore[attr-defined]
    pg.wait_for_timeout(1_500)  # type: ignore[attr-defined]  # the fake shopper used to answer within 0.3 s
    assert first_card in pg.inner_text("#approvals"), "an approval was answered without anyone clicking"  # type: ignore[attr-defined]
    print("approval waiting for a click:", first_card)
    _shot(pg, "02a_approval_card.png")
    answered.extend(_answer_approvals(pg))
    assert answered, "could not click the approval card"

    # --- planner -> VLA -> verifier loop (plan mode): Pico's step list with step states + the verifier's verdicts
    pg.wait_for_function("document.querySelectorAll('#planSteps .step').length >= 20", timeout=30_000)  # type: ignore[attr-defined]
    assert "plan" in pg.inner_text("#execMode").lower()  # type: ignore[attr-defined]
    pg.wait_for_function("document.querySelectorAll('#verdicts .vd').length >= 1", timeout=60_000)  # type: ignore[attr-defined]
    assert "verified by" in pg.inner_text("#verdicts")  # type: ignore[attr-defined]
    assert "✓" in pg.inner_text("#planSteps")  # type: ignore[attr-defined]  # at least one step succeeded
    _shot(pg, "02b_plan_and_verifier.png")

    # --- robots move: Pico's marker on the store_ave map changes position
    pg.wait_for_function("document.querySelectorAll('#map g').length >= 1", timeout=15_000)  # type: ignore[attr-defined]
    first = _robot_xy(pg, "Pico")
    moved = False
    for _ in range(40):
        answered.extend(_answer_approvals(pg))
        pg.wait_for_timeout(500)  # type: ignore[attr-defined]
        now = _robot_xy(pg, "Pico")
        if first is not None and now is not None and abs(now[0] - first[0]) + abs(now[1] - first[1]) > 0.3:
            moved = True
            break
        first = first or now
    assert moved, f"Pico did not move on the map (start {first})"

    # --- live MJPEG: click Pico's front camera, the <img> stream must decode
    pg.click("#cams img[data-cam='rover-ave-1.front']")  # type: ignore[attr-defined]
    pg.wait_for_function(  # type: ignore[attr-defined]
        "(() => { const i = document.querySelector(\"#cams img[data-cam='rover-ave-1.front']\");"
        " return i && i.src.includes('stream.mjpg') && i.naturalWidth > 0; })()",
        timeout=15_000,
    )
    pg.click("#cams img[data-cam='store_ave.ceiling-1']")  # type: ignore[attr-defined]
    pg.wait_for_function(  # type: ignore[attr-defined]
        "(() => { const i = document.querySelector(\"#cams img[data-cam='store_ave.ceiling-1']\");"
        " return i && i.src.includes('stream.mjpg') && i.naturalWidth > 0; })()",
        timeout=15_000,
    )
    print(f"live streams decoded: {_images_loaded(pg, '#cams img[src*=stream]')}")
    _shot(pg, "02_running.png")

    # --- progress: Pico puts things in the basket (cart lines appear), Hazel's scan finishes
    assert _wait_answering(pg, "document.querySelectorAll('#cart tr').length >= 4", 90, answered), "no cart lines"
    _wait_answering(
        pg,
        "[...document.querySelectorAll('#goals tr')].some(r => r.innerText.includes('shelf_scan')"
        " && r.innerText.includes('completed'))",
        60,
        answered,
    )
    _shot(pg, "03_progress.png")
    pg.locator("#cams").screenshot(path=str(ARTIFACTS / "04_cameras_store_ave.png"))  # type: ignore[attr-defined]

    # --- the second store's cameras (Hazel + store_hill ceiling)
    pg.select_option("#camStore", "store_hill")  # type: ignore[attr-defined]
    pg.wait_for_function(  # type: ignore[attr-defined]
        "(() => { const i = [...document.querySelectorAll('#cams img')];"
        " return i.length >= 4"
        " && i.every(x => x.dataset.cam.startsWith('rover-hill-1') || x.dataset.cam.startsWith('store_hill'))"
        " && i.every(x => x.complete && x.naturalWidth > 0); })()",
        timeout=20_000,
    )
    pg.locator("#cams").screenshot(path=str(ARTIFACTS / "05_cameras_store_hill.png"))  # type: ignore[attr-defined]

    # --- the trip finishes once the shopper clicks the checkout approval on the page (explicit approval, §8)
    paid = _wait_answering(
        pg,
        "['paid', 'followup', 'done'].some(s => document.getElementById('state').textContent.startsWith(s))",
        180,
        answered,
    )
    print("trip state:", pg.inner_text("#state"))  # type: ignore[attr-defined]
    print(f"approvals answered on the page ({len(answered)}):", *answered, sep="\n  ")
    _shot(pg, "06_final.png")
    assert paid, f"trip did not reach paid: {pg.inner_text('#state')}"  # type: ignore[attr-defined]
    assert any("[checkout]" in t for t in answered), "the checkout payment must be approved by a click"

    assert not pg.errors, f"page errors: {pg.errors}"  # type: ignore[attr-defined]
