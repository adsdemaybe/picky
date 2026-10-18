import base64
import sys
import threading

import pytest

from arms.sim.tests.sim_fixtures import loader, robots_loader


def test_import_does_not_pull_isaac():
    import arms.sim.app as app

    assert "isaacsim" not in sys.modules and "pxr" not in sys.modules
    assert app.gateway_ws_url("http://localhost:8000", "dev tok") == "ws://localhost:8000/ws/sim?token=dev%20tok"
    assert app.gateway_ws_url("https://gw.example.com/api/") == "wss://gw.example.com/api/ws/sim"


def test_require_isaac_explains_itself():
    import arms.sim.app as app

    try:
        import isaacsim  # noqa: F401
    except ImportError:
        with pytest.raises(app.IsaacUnavailable, match="python.sh"):
            app.require_isaac()
    else:  # pragma: no cover - only on the GPU box
        assert app.require_isaac() is not None


def test_main_isaac_backend_fails_cleanly_without_isaac(capsys):
    import arms.sim.app as app

    if "isaacsim" in sys.modules:  # pragma: no cover
        pytest.skip("Isaac installed")
    assert app.main(["--backend", "isaac"]) == 2
    assert "Isaac Sim" in capsys.readouterr().err


def test_sketch_backend_renders_real_frames():
    pytest.importorskip("PIL")
    from PIL import Image

    from arms.sim.sketch import SketchBackend
    from arms.sim.worker import WorkerCore

    out: list = []
    core = WorkerCore(SketchBackend(), out.append, loader=loader, robots_loader=robots_loader)
    core.load_stores(["store_test", "store_two"])
    import io

    for cid in ("rover-test-1.front", "rover-test-1.basket", "store_test.ceiling-1", "rover-two-1.wrist"):
        frame = core.capture(cid)
        img = Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64)))
        assert img.size == (frame.w, frame.h) == (640, 480)
        colors = img.convert("RGB").getcolors(maxcolors=1_000_000)
        assert colors is not None and len(colors) > 20, cid  # an actual picture, not a flat placeholder


def test_link_end_to_end_multi_robot():
    """Worker connects outbound, says hello (stores/robots/cameras), gets welcome, sends a state per robot, executes
    commands for two robots in two stores, answers an unparsable command, streams frames."""
    pytest.importorskip("websockets")
    from websockets.sync.server import serve

    import arms.sim.app as app
    from arms.sim.worker import NullBackend, WorkerCore
    from contracts.sim import (
        CameraFrame,
        CommandResult,
        NavigateCmd,
        SimHello,
        SimWelcome,
        parse_sim_message,
    )

    got: dict = {"hello": None, "results": {}, "states": set(), "frames": set()}
    done = threading.Event()

    def handler(ws):
        hello = parse_sim_message(ws.recv(timeout=5))
        got["hello"] = hello
        ws.send(SimWelcome(ok=hello.token == "tok", session_id="sess").model_dump_json())
        ws.send(NavigateCmd(cmd_id="n1", robot_id="rover-test-1", waypoints=[{"x": 2.5, "y": 2.0}]).model_dump_json())
        ws.send(NavigateCmd(cmd_id="n2", robot_id="rover-two-1", waypoints=[{"x": 2.0, "y": 2.5}]).model_dump_json())
        ws.send('{"type": "navigate", "cmd_id": "bad", "robot_id": "rover-test-1", "waypoints": "nope"}')
        while not done.is_set():
            try:
                msg = parse_sim_message(ws.recv(timeout=5))
            except TimeoutError:
                break
            if msg.type == "robot_state":
                got["states"].add(msg.robot_id)
            if isinstance(msg, CameraFrame):
                got["frames"].add(msg.camera_id)
            if isinstance(msg, CommandResult):
                got["results"][msg.cmd_id] = msg
            if {"n1", "n2", "bad"} <= set(got["results"]) and len(got["frames"]) >= 3:
                done.set()

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        link = None
        backend = NullBackend()
        core = WorkerCore(
            backend, emit=lambda m: link.send(m) if link else None, loader=loader, robots_loader=robots_loader
        )
        core.load_stores(["store_test", "store_two"])
        link = app.GatewayLink(app.gateway_ws_url(f"http://127.0.0.1:{port}", "tok"), lambda: core.hello("tok"))
        link.start()
        try:
            app.run(backend, link, core, is_running=lambda: not done.is_set(), max_seconds=30)
        finally:
            link.stop()
            server.shutdown()

    hello = got["hello"]
    assert isinstance(hello, SimHello) and set(hello.store_ids()) == {"store_test", "store_two"}
    assert set(hello.robot_ids()) == {"rover-test-1", "rover-test-2", "rover-two-1"}
    res = got["results"]
    assert res["n1"].ok and res["n1"].robot_id == "rover-test-1", res["n1"]
    assert res["n2"].ok and res["n2"].state.store_id == "store_two", res["n2"]
    assert res["bad"].ok is False and res["bad"].error_code == "internal"
    assert got["states"] >= {"rover-test-1", "rover-test-2", "rover-two-1"}
    assert got["frames"] == {"rover-test-1.front", "rover-test-2.front", "rover-two-1.front"}  # default streams
