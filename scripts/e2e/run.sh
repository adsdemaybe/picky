#!/usr/bin/env bash
# Picky judges'-flow E2E harness. One command:
#   APP_URL=http://localhost:3001 GATEWAY_URL=http://localhost:8100 ./run.sh
#   ./run.sh install      # once: download the matching Chromium
# Env knobs: see e2e.py (Config.from_env) and README.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPS=(--no-project --with playwright==1.64.0 --with httpx==0.28.1 --with websockets==15.0.1)
if [[ "${1:-}" == "install" ]]; then
  exec uv run "${DEPS[@]}" playwright install chromium
fi
exec uv run "${DEPS[@]}" python "$HERE/e2e.py" "$@"
