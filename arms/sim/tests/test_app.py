import sys
import threading

import pytest

from arms.sim.tests.sim_fixtures import planogram, products


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
        with pytest.raises(app.IsaacUnavailable, match="RTX 4080"):
            app.require_isaac()
    else:  # pragma: no cover - only on the GPU box
        assert app.require_isaac() is not None


def test_main_isaac_backend_fails_cleanly_without_isaac(capsys):
    pytest.importorskip("websockets")
    import arms.sim.app as app

    if "isaacsim" in sys.modules:  # pragma: no cover
        pytest.skip("Isaac installed")
    assert app.main(["--backend", "isaac"]) == 2
    assert "Isaac Sim 6.2" in capsys.readouterr().err


def test_link_end_to_end_with_kinematic_backend():
    """Worker connects outbound, says hello with the token, gets welcome, executes a command, returns its result."""
    from websockets.sync.server import serve

    import arms.sim.app as app
    from arms.sim.worker import NullBackend, WorkerCore
    from contracts.common import Pose2D
    from contracts.sim import CommandResult, NavigateCmd, SimHello, SimWelcome, parse_sim_message

    got: dict = {"hello": None, "result": None, "states": 0}
    done = threading.Event()

    def handler(ws):
        hello = parse_sim_message(ws.recv(timeout=5))
        got["hello"] = hello
        ws.send(SimWelcome(ok=hello.token == "tok", session_id="sess").model_dump_json())
        ws.send(NavigateCmd(cmd_id="n1", waypoints=[Pose2D(x=1.0, y=2.0, theta=0.0)]).model_dump_json())
        while not done.is_set():
            try:
                msg = parse_sim_message(ws.recv(timeout=5))
            except TimeoutError:
                break
            if msg.type == "robot_state":
                got["states"] += 1
            if isinstance(msg, CommandResult) and msg.cmd_id == "n1":
                got["result"] = msg
                done.set()

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        link = None
        core = WorkerCore(
            NullBackend(),
            emit=lambda m: link.send(m) if link else None,
            store_id="store_test",
            loader=lambda sid, seed: (planogram(), products()),
        )
        core.load_scene()
        link = app.GatewayLink(app.gateway_ws_url(f"http://127.0.0.1:{port}", "tok"), lambda: core.hello("tok"))
        link.start()
        try:
            app.run(NullBackend(), link, core, fps=20, is_running=lambda: not done.is_set(), max_seconds=20)
        finally:
            link.stop()
            server.shutdown()

    assert isinstance(got["hello"], SimHello) and got["hello"].store_id == "store_test"
    assert got["hello"].data_hash == core.spec.data_hash
    assert got["result"] is not None and got["result"].ok, got["result"]
    assert got["states"] >= 1
