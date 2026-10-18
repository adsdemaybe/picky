import base64
import io
import math

import pytest

from arms.sim.tests.sim_fixtures import loader, robots_loader

pytest.importorskip("mujoco")
pytest.importorskip("PIL")


def _image(frame):
    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64))).convert("RGB")


@pytest.fixture
def core():
    from arms.sim.mujoco_backend import MujocoBackend
    from arms.sim.worker import WorkerCore

    out: list = []
    c = WorkerCore(MujocoBackend(), out.append, loader=loader, robots_loader=robots_loader)
    c.load_stores(["store_test", "store_two"])
    return c


def test_every_camera_kind_renders_a_picture(core):
    for cid in ("rover-test-1.front", "rover-test-1.basket", "store_test.ceiling-1", "rover-two-1.wrist"):
        frame = core.capture(cid)
        img = _image(frame)
        assert img.size == (frame.w, frame.h) == (640, 480)
        colors = img.getcolors(maxcolors=1_000_000)
        assert colors is not None and len(colors) > 20, cid  # an actual picture, not a flat placeholder


def test_parked_unit_disappears_without_recompile(core):
    backend = core.backend
    core.capture("store_test.ceiling-1")
    builds = backend.builds
    key = next(k for k in backend.instance_prims if k[0] == "store_test")
    backend.remove_instance(*key)
    core.capture("store_test.ceiling-1")
    assert backend.builds == builds
    mid = backend._instance_mocap[key]
    assert backend._data.mocap_pos[mid][2] < -10


def test_camera_orientation_matches_core_projection():
    """A red box straight ahead of the camera lands in the image centre (MuJoCo -z forward, +y up)."""
    from arms.sim.geometry import CameraPose
    from arms.sim.mujoco_backend import _quat_from_axes

    cam = CameraPose(0.0, 0.0, 1.0, yaw=math.pi / 2, pitch=-0.3)
    r, u, f = cam.basis()
    q = _quat_from_axes(r, u, (-f[0], -f[1], -f[2]))
    assert abs(sum(c * c for c in q) - 1) < 1e-9
    # rotate local -z by q and compare with forward
    w, x, y, z = q
    fwd = (-(2 * (x * z + w * y)), -(2 * (y * z - w * x)), -(1 - 2 * (x * x + y * y)))
    assert all(abs(a - b) < 1e-9 for a, b in zip(fwd, f, strict=True))
