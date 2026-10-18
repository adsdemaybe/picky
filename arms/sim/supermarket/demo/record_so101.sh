#!/usr/bin/env bash
# One command: record the SO-101 rover supermarket demo in Isaac Sim (Gemini-planned trip, 30 fps of sim time) and
# cut the videos on the GPU host (frames stay there).
#
#   bash arms/sim/supermarket/demo/record_so101.sh                 # full run -> $RUN/video/*.mp4 + .gif
#   bash arms/sim/supermarket/demo/record_so101.sh --plan-only     # map + shopping list + Gemini plan only
#   RUN=/data/advaiv2/picky-so101/runs/x bash .../record_so101.sh --max-items 10
#
# Env: ISAAC_PY (Isaac Sim 6.x python.sh), PICKY_SO101 (robot build dir), SCENE_USD, ROBOT_USD, RUN, LIGHT_SCALE.
# The Gemini key comes from GEMINI_API_KEY or ~/.config/picky/secrets.env (never logged).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
APP="$REPO/arms/sim/supermarket"
ISAAC_PY="${ISAAC_PY:-/data/advaiv2/isaacsim/python.sh}"
PICKY_SO101="${PICKY_SO101:-/data/advaiv2/picky-so101}"
SCENE_USD="${SCENE_USD:-/data/advaiv2/picky-suraj/geniesim/background/supermarket/supermarket_06/background.usda}"
ROBOT_USD="${ROBOT_USD:-$PICKY_SO101/build/picky_so101_rover.usd}"
RUN="${RUN:-$PICKY_SO101/runs/$(date +%m%d_%H%M)}"
mkdir -p "$RUN"
if [[ ! -f "$ROBOT_USD" ]]; then
  PICKY_SO101="$PICKY_SO101" bash "$APP/robot/setup_so101_rover.sh"
fi
export OMNI_KIT_ACCEPT_EULA=YES
"$ISAAC_PY" "$APP/teleop_app.py" --scene "$SCENE_USD" --robot "$ROBOT_USD" --demo --record --out "$RUN" \
  --light-scale "${LIGHT_SCALE:-0.8}" "$@" 2>&1 | tee "$RUN/sim.log"
[[ " $* " == *" --plan-only "* ]] && exit 0
cd "$APP/demo"
uv run --no-project --with "imageio[ffmpeg]" --with pillow --with numpy \
  python edit_so101.py "$RUN" "$RUN/video" 2>&1 | tee "$RUN/edit.log"
ls -la "$RUN/video"
