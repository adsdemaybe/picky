# utils/

Shared helpers that **two or more arms** use and that contain **no business logic**. Anything that knows about
trips, carts, mandates, SKUs or routes belongs in an arm (or, if it is an interface, in `contracts/`).

Base deps only (pydantic, pydantic-settings, pyyaml, numpy, httpx, anthropic). `utils` never imports `contracts`
or any arm, and `import utils` is free (no eager submodule imports, no env reads, no network).

## Module index

| Module | What it gives you |
|---|---|
| `config.py` | `settings` (lazy `Settings`, pydantic-settings, reads env + repo-root `.env`). Fields = the env vars in `.env.example`, lower-cased. Helpers: `settings.real_arms()`, `settings.is_real("routing")` (`PICKY_REAL_ARMS`, `all` = every arm), `settings.repo_root`, `settings.data_dir`. Tests: `set_settings(**overrides)`, `reset_settings()`. |
| `log.py` | `get_logger(__name__)`: `HH:MM:SS.mmm LEVEL [arm] module: msg`; level from `PICKY_LOG_LEVEL` (default INFO; process env only, not `.env`). |
| `db.py` | Shared async DB: `get_engine()` (one pool per process from `DATABASE_URL`), `async with connect()` (Core), `async with session()` (ORM `AsyncSession`), `dispose_all()`. SQLAlchemy comes from an arm extra (`uv sync --extra catalog`); imports are lazy, `DatabaseUnavailable` without it. Use this instead of building your own engine. |
| `money.py` | Int-cents only: `fmt_cents(1234) -> "$12.34"`, `parse_dollars("$12.34") -> 1234`, `sum_cents`, `line_total`, `apply_pct_off(cents, pct)`, `pct_off(orig, sale) -> int %`, `savings`. Floats raise `TypeError`. |
| `geometry.py` | World-frame math on tuples / `Pose` NamedTuple / anything with `.x .y [.theta]` (so contracts `Pose2D` works): `distance`, `heading`, `wrap_angle`, `angle_diff`, `polyline_length`, `interpolate_polyline`, `resample_polyline`, `to_local`/`to_world`, `rect_corners`, `point_in_rect(..., yaw, margin)`, `bbox`. (Grid frames: use `contracts.routing.GridSpec`.) |
| `ids.py` | `new_id("trip") -> "trip_k3f9x2ab"`; `seed_ids(42)` or env `PICKY_ID_SEED` (process env only, not `.env`) for deterministic runs; `IdFactory(seed)` for an independent stream (fakes). |
| `events.py` | `EventBus`: `await bus.publish(ev)` (pydantic model or dict with `type`; stamps monotonically increasing `seq` and empty `ts`), `bus.subscribe("route" \| ["robot", "trip.state"] \| None, trip_id=..., replay=...)` -> async-iterable `Subscription` with bounded queue + drop-oldest, `bus.recent(after_seq=...)` for websocket catch-up. |
| `llm.py` | `get_client()` (lazy `AsyncAnthropic`; `LLMNotConfigured` without `ANTHROPIC_API_KEY`), `model("brain"\|"fast"\|"smart")` / `MODEL_BRAIN`..., `complete_json(prompt, Model)` (structured output + pydantic validation + 1 retry; tool-forcing fallback), `run_tool_loop(messages, tools, handlers, max_iters=8)`, `cached_system` / `cached_tools` (prompt caching), `extract_json`, `tool_from_model`. Every call takes `client=` for tests. |
| `signing.py` | `canonical_json(obj, exclude=...)` (sorted keys, compact, pydantic-aware), `sha256_hex(obj)` (cart hash), `sign(obj, key=PICKY_SIGNING_KEY)` / `verify(obj, sig)` (HMAC-SHA256, constant-time). |
| `fsm.py` | `StateMachine(transitions, initial, any_to=[FAILED], terminal=[...], on_transition=cb)`: `transition(dst, reason)` raises `IllegalTransition`; `can`, `allowed`, `history`, `path()`. Used by the gateway's `TripState`. |
| `data.py` | Loaders for `data/` YAML (products, planograms, online store, users). Owned by the contracts author. |

## Examples

```python
from utils.config import settings
from utils.log import get_logger
from utils.money import fmt_cents
from utils.llm import complete_json, LLMNotConfigured

log = get_logger(__name__)
log.info("budget %s in %s", fmt_cents(4000), settings.picky_default_store)

try:
    plan = await complete_json("tacos for 6", ShoppingList, kind="brain")
except LLMNotConfigured:
    plan = fake_plan()  # every LLM caller needs a deterministic fallback
```

## Contributing to utils

1. **Check it belongs here.** Used by 2+ arms (or about to be), feature-agnostic, base deps only. One arm's helper
   stays in that arm. Interfaces/data shapes go in `contracts/`, not here.
2. **Branch** `utils/<slug>` and keep the PR to utils only (don't mix with arm changes).
3. **Don't break callers.** Add functions/keyword args; never rename or change the meaning of existing ones. If you
   must, grep `arms/` for every caller and fix them in the same PR.
4. **Rules:** no `contracts`/`arms` imports; no I/O, env reads or network at import time; money stays int cents;
   geometry stays meters/radians in the world frame; Python 3.11-compatible syntax; heavy deps only as lazy imports.
5. **New env var?** Add it to `Settings` in `config.py` **and** `.env.example` together (`test_config.py` checks
   they match), and mention it in CLAUDE.md if arms need it.
6. **Tests** in `utils/tests/test_<module>.py`, no network (inject `client=` for `llm`). Before the PR:
   `uv run ruff check --fix utils && uv run ruff format utils && uv run pytest utils`.
7. Update the module index above.
