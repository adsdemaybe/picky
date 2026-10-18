"""Director (chase) camera: pure geometry + env parsing (no Isaac)."""

import math
from types import SimpleNamespace

from arms.sim.director import EYE_Z_M, Director, chase_pose
from contracts.common import Pose2D


def test_chase_pose_sits_behind_right_and_above_looking_down_at_the_rover():
    pose = Pose2D(x=5.0, y=5.0, theta=0.0)
    cam = chase_pose(pose, 0.0)
    assert cam.x < pose.x and cam.y < pose.y  # behind (-x) and to the right (-y) of a rover facing +x
    assert cam.z == EYE_Z_M and cam.pitch < 0
    r, u, f = cam.basis()
    # the rover is in front of the camera and projects inside the image
    assert cam.project((pose.x, pose.y, 0.5)) is not None
    assert abs(math.atan2(f[1], f[0])) < math.radians(60)


def test_chase_pose_is_clamped_inside_the_store():
    cam = chase_pose(Pose2D(x=0.5, y=0.5, theta=0.0), 0.0, bounds=(0.0, 0.0, 14.0, 22.0))
    assert 0.3 <= cam.x <= 13.7 and 0.3 <= cam.y <= 21.7


def test_director_from_env_and_jobs(tmp_path):
    assert Director.from_env({}) is None
    d = Director.from_env(
        {"PICKY_SIM_DIRECTOR": "r1, r2", "PICKY_SIM_DIRECTOR_DIR": str(tmp_path), "PICKY_SIM_DIRECTOR_FPS": "5"}
    )
    assert d is not None and d.robot_ids == ["r1", "r2"] and d.fps == 5.0
    core = SimpleNamespace(rovers={"r1": SimpleNamespace(pose=Pose2D(x=3, y=4, theta=1.0), store_id="s")}, stores={})
    jobs = d.jobs(core, now=10.0)
    assert [j[0] for j in jobs] == ["r1.director"] and jobs[0][1] == "s"

    class Backend:
        def render(self, camera_id, store_id, cam):
            return b"\xff\xd8jpeg", cam.w, cam.h

    assert d.capture(Backend(), jobs)
    assert len(list((tmp_path / "r1" / "director").glob("*.jpg"))) == 1
    assert not d.due()


def test_director_tees_frames_of_followed_robots(tmp_path):
    import base64

    from contracts.sim import CameraFrame

    d = Director.from_env({"PICKY_SIM_DIRECTOR": "r1", "PICKY_SIM_DIRECTOR_DIR": str(tmp_path)})
    jpeg = base64.b64encode(b"\xff\xd8x").decode()
    for rid, cam in (("r1", "r1.policy_side"), ("r1", "r1.policy_side"), ("r2", "r2.front")):
        d.tee(CameraFrame(frame_id="f", camera_id=cam, robot_id=rid, jpeg_b64=jpeg, w=8, h=8))
    assert len(list((tmp_path / "r1" / "policy_side").glob("*.jpg"))) == 2
    assert not (tmp_path / "r2").exists()
