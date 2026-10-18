import numpy as np

from arms.robot.so101.train_loop import DIMS, SteeringPolicy


def test_success_pulls_mean_and_narrows():
    p = SteeringPolicy()
    theta = {k: 0.5 * b for k, (_, b) in DIMS.items()}
    p.update(theta, success=True)
    assert all(np.isclose(p.mean[k], 0.6 * theta[k]) for k in DIMS)
    assert all(p.std[k] < s0 for k, (s0, _) in DIMS.items())
    assert (p.episodes, p.successes) == (1, 1)


def test_failure_widens_and_respects_caps():
    p = SteeringPolicy()
    theta = {k: 0.0 for k in DIMS}
    for _ in range(20):
        p.update(theta, success=False)
    assert all(np.isclose(p.std[k], 2 * s0) for k, (s0, _) in DIMS.items())


def test_samples_stay_in_bounds():
    p = SteeringPolicy()
    p.std = {k: 10 * b for k, (_, b) in DIMS.items()}
    rng = np.random.default_rng(0)
    for _ in range(50):
        s = p.sample(rng)
        assert all(abs(s[k]) <= b for k, (_, b) in DIMS.items())
