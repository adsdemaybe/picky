import datetime as dt

from pydantic import BaseModel

from utils.signing import canonical_json, sha256_hex, sign, verify


class Cart(BaseModel):
    total_cents: int
    lines: list[str]
    ts: dt.datetime


def test_canonical_is_order_independent():
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert sha256_hex({"a": 1, "b": 2}) == sha256_hex({"b": 2, "a": 1})
    assert len(sha256_hex({"a": 1})) == 64


def test_pydantic_and_exclude():
    c = Cart(total_cents=100, lines=["x"], ts=dt.datetime(2026, 10, 17, tzinfo=dt.UTC))
    d = c.model_dump(mode="json")
    assert canonical_json(c) == canonical_json(d)
    assert sha256_hex({**d, "signature": "zz"}, exclude={"signature"}) == sha256_hex(c)


def test_sign_verify():
    obj = {"cap_cents": 4000, "user": "maya"}
    sig = sign(obj, "k1")
    assert verify({"user": "maya", "cap_cents": 4000}, sig, "k1")
    assert not verify(obj, sig, "k2")
    assert not verify({**obj, "cap_cents": 4001}, sig, "k1")
    assert verify(obj, sign(obj))  # default key from settings
    assert not verify(obj, None)  # type: ignore[arg-type]
