from utils.ids import IdFactory, is_id, new_id, seed_ids


def test_shape():
    i = new_id("trip")
    assert i.startswith("trip_") and len(i) == 13
    assert is_id(i, "trip") and not is_id(i, "cart") and not is_id("nope")


def test_deterministic():
    seed_ids(42)
    a = [new_id("trip") for _ in range(3)]
    seed_ids(42)
    b = [new_id("trip") for _ in range(3)]
    seed_ids(None)
    assert a == b and len(set(a)) == 3
    assert IdFactory(7)("x") == IdFactory(7)("x")


def test_bad_prefix():
    import pytest

    with pytest.raises(ValueError):
        new_id("Bad-Prefix")
