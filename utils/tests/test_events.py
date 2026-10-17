import asyncio
import datetime as dt

import pytest
from pydantic import BaseModel

from utils.events import EventBus, event_to_dict, type_matches


class Ev(BaseModel):
    type: str
    trip_id: str = "t1"
    seq: int = 0
    ts: dt.datetime | None = None
    data: int = 0


async def test_fanout_prefix_and_seq():
    bus = EventBus()
    all_sub = bus.subscribe()
    route = bus.subscribe("route")
    multi = bus.subscribe(["robot.", "trip.state"])
    await bus.publish(Ev(type="route.planned"))
    await bus.publish({"type": "robot.state", "trip_id": "t1"})
    await bus.publish({"type": "routes.other"})
    await bus.publish({"type": "trip.state", "trip_id": "t2"})
    got = all_sub.drain()
    assert [e["type"] if isinstance(e, dict) else e.type for e in got] == [
        "route.planned",
        "robot.state",
        "routes.other",
        "trip.state",
    ]
    seqs = [e["seq"] if isinstance(e, dict) else e.seq for e in got]
    assert seqs == [1, 2, 3, 4] and bus.last_seq == 4
    assert got[0].ts is not None and isinstance(got[1]["ts"], str)
    assert [e.type for e in route.drain()] == ["route.planned"]
    assert [e["type"] for e in multi.drain()] == ["robot.state", "trip.state"]


async def test_trip_filter_and_publisher_untouched():
    bus = EventBus()
    sub = bus.subscribe(trip_id="t2")
    original = {"type": "x.y", "trip_id": "t2"}
    out = bus.publish_nowait(original)
    bus.publish_nowait({"type": "x.y", "trip_id": "t1"})
    assert "seq" not in original and out["seq"] == 1
    assert [e["trip_id"] for e in sub.drain()] == ["t2"]


async def test_drop_oldest():
    bus = EventBus(queue_size=3)
    sub = bus.subscribe()
    for i in range(5):
        bus.publish_nowait({"type": "tick", "i": i})
    assert [e["i"] for e in sub.drain()] == [2, 3, 4]
    assert sub.dropped == 2


async def test_async_iteration_and_close():
    bus = EventBus()
    received = []

    async def consumer(sub):
        async for ev in sub:
            received.append(ev["i"])

    async with bus.subscribe("tick") as sub:
        task = asyncio.create_task(consumer(sub))
        for i in range(3):
            await bus.publish({"type": "tick", "i": i})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    await asyncio.wait_for(task, 1)
    assert received == [0, 1, 2] and bus.subscriber_count == 0


async def test_get_timeout_and_replay():
    bus = EventBus()
    sub = bus.subscribe()
    with pytest.raises(TimeoutError):
        await sub.get(timeout=0.01)
    bus.publish_nowait({"type": "a.b", "trip_id": "t"})
    bus.publish_nowait({"type": "c", "trip_id": "t"})
    late = bus.subscribe("a", replay=True)
    assert [e["type"] for e in late.drain()] == ["a.b"]
    assert [e["seq"] for e in bus.recent(after_seq=1)] == [2]
    assert event_to_dict(Ev(type="z"))["type"] == "z"


def test_type_matches():
    assert type_matches("route.planned", frozenset({"route"}))
    assert not type_matches("routes", frozenset({"route"}))
    assert type_matches("anything", None)


def test_missing_type():
    with pytest.raises(ValueError):
        EventBus().publish_nowait({"no": "type"})
