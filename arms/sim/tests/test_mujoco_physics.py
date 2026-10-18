import base64
import io
import time

import pytest

from arms.sim.tests.sim_fixtures import loader, robots_loader

mujoco = pytest.importorskip("mujoco")
pytest.importorskip("PIL")


@pytest.fixture
def core():
    from arms.sim.mujoco_physics import MujocoPhysicsBackend, so101_xml_path
    from arms.sim.worker import WorkerCore

    if so101_xml_path() is None:
        pytest.skip("SO-101 MJCF not fetched (arms/robot/so101/get_assets.sh)")
    c = WorkerCore(MujocoPhysicsBackend(), [].append, loader=loader, robots_loader=robots_loader)
    c.load_stores(["store_test", "store_two"])
    return c


def test_scene_has_real_arm_and_sleeping_stock(core):
    b = core.backend
    b.step()
    m = b._model
    assert all(rid in b._arm for rid in b.rovers)  # every rover carries the SO-101 with its 6 servos
    assert m.nu == 6 * len(b.rovers)
    start = {k: b._data.xpos[bid].copy() for k, bid in b._body.items()}
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.0:
        b.step()
        time.sleep(0.01)
    moved = [k for k, bid in b._body.items() if abs(b._data.xpos[bid][2] - start[k][2]) > 0.01]
    assert not moved, moved  # stocked units stay on their shelves (asleep until touched)


def test_cameras_render_with_physics(core):
    from PIL import Image

    for cid in ("rover-test-1.front", "rover-test-1.wrist", "store_test.ceiling-1"):
        frame = core.capture(cid)
        img = Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64))).convert("RGB")
        assert img.size == (frame.w, frame.h)
        assert len(img.getcolors(maxcolors=1_000_000) or []) > 20, cid
