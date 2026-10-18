"""Export the gateway OpenAPI + the events/client-message JSON schemas for frontend TS generation.

``python -m arms.gateway.export_schemas [out_dir]`` (default ``arms/frontend/schemas/``), run by ``make gen-types``.
Writes ``openapi.json``, ``events.schema.json`` (``contracts.events.Event`` union) and ``client_messages.schema.json``
(``contracts.api.ClientMessage``). No services are constructed (the app lifespan does not run).
"""

import json
import sys
from pathlib import Path
from typing import Any

from contracts.api import CLIENT_MESSAGE_ADAPTER
from contracts.events import event_json_schema
from utils.config import repo_root

DEFAULT_OUT = Path("arms") / "frontend" / "schemas"


def schemas() -> dict[str, Any]:
    from arms.gateway.app import create_app

    app = create_app()
    return {
        "openapi.json": app.openapi(),
        "events.schema.json": event_json_schema(),
        "client_messages.schema.json": CLIENT_MESSAGE_ADAPTER.json_schema(mode="serialization"),
    }


def export(out_dir: Path | None = None) -> list[Path]:
    out = out_dir or (repo_root() / DEFAULT_OUT)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, schema in schemas().items():
        path = out / name
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[0]).resolve() if argv else None
    for path in export(out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
