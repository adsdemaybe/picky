import math

import pytest

from utils.data import DataError, load_demo_fixtures, load_planogram, load_products


def data_ready() -> bool:
    try:
        load_products()
        load_planogram("store_ave")
        load_demo_fixtures()
        return True
    except (DataError, FileNotFoundError, OSError, ValueError):
        return False


needs_data = pytest.mark.skipif(not data_ready(), reason="data/ not written yet (data agent)")

FAST_FAKES = {"robot": {"realtime_factor": math.inf}, "messaging": {"reply_delay_s": 0.01}}


async def until(predicate, timeout_s: float = 20.0, step_s: float = 0.02) -> None:  # noqa: ANN001
    """Poll ``predicate()`` (sync or async) until truthy; TimeoutError after ``timeout_s``."""
    import asyncio
    import inspect

    for _ in range(int(timeout_s / step_s) + 1):
        res = predicate()
        if inspect.isawaitable(res):
            res = await res
        if res:
            return
        await asyncio.sleep(step_s)
    raise TimeoutError(f"condition not met within {timeout_s}s")
