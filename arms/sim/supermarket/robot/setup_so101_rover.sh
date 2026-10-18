#!/usr/bin/env bash
# Fetch the open-source robot designs and build Picky's SO-101 rover v3 USD (+ .kin.json). Re-runnable.
#   1. TheRobotStudio/SO-ARM100  Simulation/SO101   (SO-101 URDF + STLs, Apache-2.0)            -> $SRC/SO-ARM100
#   2. pal-robotics/pmb2_robot   pmb2_description   (PAL PMB2 / TIAGo base xacros + STLs, Apache-2.0) -> $SRC/pmb2_robot
#   3. tools venv: numpy trimesh scipy networkx shapely mapbox_earcut rtree fast-simplification usd-core==25.5.1
#   4. build_so101_rover.py -> $OUT/picky_so101_rover.usd + picky_so101_rover.kin.json, then reach_check.py
#
#   PICKY_SO101=/data/advaiv2/picky-so101 bash arms/sim/supermarket/robot/setup_so101_rover.sh [build args...]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${PICKY_SO101:-$HOME/picky-so101}"
SRC="$ROOT/src"
OUT="$ROOT/build"
VENV="$ROOT/venv"
SO_ARM_COMMIT="${SO_ARM_COMMIT:-a758567c3978dfeefe282ede0500085a48fe8f78}"
PMB2_COMMIT="${PMB2_COMMIT:-e6584136f15356b8f4944c25d70fb8689e3355af}"
mkdir -p "$SRC" "$OUT"

fetch() {  # repo dir subdir commit
  if [[ ! -d "$SRC/$2/.git" ]]; then
    git clone -q --filter=blob:none --sparse "https://github.com/$1.git" "$SRC/$2"
  fi
  git -C "$SRC/$2" sparse-checkout set "$3"
  git -C "$SRC/$2" fetch -q --depth 1 origin "$4" 2>/dev/null || true
  git -C "$SRC/$2" checkout -q "$4"
}
fetch TheRobotStudio/SO-ARM100 SO-ARM100 Simulation/SO101 "$SO_ARM_COMMIT"
fetch pal-robotics/pmb2_robot pmb2_robot pmb2_description "$PMB2_COMMIT"

command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[[ -x "$VENV/bin/python" ]] || uv venv -q -p 3.12 "$VENV"
# usd-core 25.5 writes crate files that Isaac Sim 6.0 (Kit 110) reads
uv pip install -q -p "$VENV/bin/python" numpy trimesh scipy networkx shapely mapbox_earcut rtree fast-simplification \
  "usd-core==25.5.1" pillow "imageio[ffmpeg]"

"$VENV/bin/python" -W ignore "$HERE/build_so101_rover.py" --so101 "$SRC/SO-ARM100/Simulation/SO101" \
  --pmb2 "$SRC/pmb2_robot/pmb2_description" --out "$OUT/picky_so101_rover.usd" "$@"
"$VENV/bin/python" "$HERE/reach_check.py" "$OUT/picky_so101_rover.kin.json" --json "$OUT/reach_check.json"
