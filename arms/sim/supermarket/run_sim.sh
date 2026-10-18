#!/usr/bin/env bash
# Launch the Picky supermarket sim. Extra args go to app.py, e.g.:
#   bash arms/sim/supermarket/run_sim.sh --livestream     # view/drive from another machine with the Isaac Sim WebRTC Streaming Client
#   bash arms/sim/supermarket/run_sim.sh --smoke          # headless self-test, screenshots in $PICKY_DATA/smoke
#   bash arms/sim/supermarket/run_sim.sh                  # local GUI window (needs a display)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
PICKY_DATA="${PICKY_DATA:-/data/picky}"                     # tools venv, assets, robot build (see README)
SCENE="${SCENE:-supermarket_12}"
SCENE_USD="${SCENE_USD:-$PICKY_DATA/geniesim/background/supermarket/$SCENE/background.usda}"
ROBOT_USD="${ROBOT_USD:-$PICKY_DATA/build/rm2_basket.usd}"

# Isaac Sim python: pip install (~/isaacsim-env, Isaac Sim 6.x) or standalone zip (~/isaacsim/python.sh).
if [[ -n "${ISAAC_PY:-}" ]]; then :
elif [[ -x "$HOME/isaacsim-env/bin/python" ]]; then ISAAC_PY="$HOME/isaacsim-env/bin/python"
else ISAAC_PY="$HOME/isaacsim/python.sh"; fi

if [[ "$(uname -m)" == "aarch64" ]]; then   # required by Isaac Sim on DGX Spark; harmless elsewhere
  export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}/lib/aarch64-linux-gnu/libgomp.so.1"
fi
export OMNI_KIT_ACCEPT_EULA=YES

extra=()
[[ " $* " == *" --smoke "* ]] && extra+=(--out "$PICKY_DATA/smoke")
exec "$ISAAC_PY" "$REPO/arms/sim/supermarket/teleop_app.py" --scene "$SCENE_USD" --robot "$ROBOT_USD" "${extra[@]}" "$@"
