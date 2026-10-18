#!/usr/bin/env bash
# One-shot setup of the Picky sim on the Linux GPU box (DGX Spark / Dell Pro Max GB10, aarch64; x86_64 also works).
#   1. Isaac Sim 5.1 standalone            -> ~/isaacsim             (~11 GB zip on aarch64)
#   2. tools venv (py3.12, uv)             -> ~/.picky-tools         (mujoco, trimesh, mujoco-usd-converter, hf hub)
#   3. DISCOVERSE rm2_car + AIRBOT meshes  -> ~/assets/DISCOVERSE    (sparse + LFS, ~5 MB)
#   4. robot build: MJCF + basket -> USD   -> arms/sim/supermarket/assets/build/rm2_basket.usd
#   5. Genie Sim supermarket scene         -> ~/assets/geniesim      (~0.4 GB for supermarket_12)
# Re-runnable: every step skips if its output exists.
#
#   bash arms/sim/supermarket/setup_linux.sh                 # everything
#   SCENE=supermarket_06 bash arms/sim/supermarket/setup_linux.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
ISAAC_DIR="${ISAAC_DIR:-$HOME/isaacsim}"
ASSETS="${ASSETS:-$HOME/assets}"
TOOLS="${TOOLS:-$HOME/.picky-tools}"
SCENE="${SCENE:-supermarket_12}"
ARCH="$(uname -m)"
ISAAC_ZIP="isaac-sim-standalone-5.1.0-linux-${ARCH}.zip"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

log "host: $(hostname) arch=$ARCH"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader || echo "WARNING: nvidia-smi failed"
df -h "$HOME" | tail -1

log "system packages"
need=()
for p in git-lfs unzip curl; do command -v "$p" >/dev/null || need+=("$p"); done
if ((${#need[@]})); then
  if command -v apt-get >/dev/null; then sudo apt-get update -qq && sudo apt-get install -y -qq "${need[@]}"
  elif command -v dnf >/dev/null; then sudo dnf install -y -q "${need[@]}"   # RHEL/Fedora (git-lfs may need EPEL)
  else echo "install manually: ${need[*]}"; exit 1; fi
fi
git lfs install --skip-repo >/dev/null
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }

log "1/5 Isaac Sim 5.1 ($ARCH)"
if [[ -x "$ISAAC_DIR/isaac-sim.sh" ]]; then
  echo "already installed at $ISAAC_DIR"
else
  mkdir -p "$ASSETS/downloads"
  curl -L --fail -C - -o "$ASSETS/downloads/$ISAAC_ZIP" "https://downloads.isaacsim.nvidia.com/$ISAAC_ZIP"
  mkdir -p "$ISAAC_DIR" && unzip -q "$ASSETS/downloads/$ISAAC_ZIP" -d "$ISAAC_DIR"
  (cd "$ISAAC_DIR" && ./post_install.sh)
fi

log "2/5 tools venv"
if [[ ! -x "$TOOLS/bin/python" ]]; then
  uv venv -q -p 3.12 "$TOOLS"
fi
uv pip install -q -p "$TOOLS" mujoco-usd-converter trimesh pillow "huggingface_hub>=0.30"

log "3/5 DISCOVERSE (rm2_car + AIRBOT Play)"
D="$ASSETS/DISCOVERSE"
if [[ ! -d "$D/.git" ]]; then
  GIT_LFS_SKIP_SMUDGE=1 git clone -q --depth 1 --filter=blob:none --sparse https://github.com/discoverse-dev/DISCOVERSE "$D"
fi
git -C "$D" sparse-checkout set models/mjcf/mobile_chassis/rm2_car models/meshes/airbot_play
git -C "$D" lfs pull --include="models/meshes/airbot_play/*"

log "4/5 build robot (rm2_car + basket -> USD)"
OUT="$REPO/arms/sim/supermarket/assets/build"
"$TOOLS/bin/python" "$REPO/arms/sim/supermarket/robot/build_rm2_basket.py" --discoverse "$D" --out "$OUT/rm2_basket" --check
rm -rf "$OUT/rm2_basket/usd"
"$TOOLS/bin/mujoco_usd_converter" "$OUT/rm2_basket/rm2_basket.xml" "$OUT/rm2_basket/usd"
"$TOOLS/bin/python" "$REPO/arms/sim/supermarket/robot/finalize_usd.py" "$OUT/rm2_basket/usd/picky_rm2_basket.usda" "$OUT/rm2_basket.usd"

log "5/5 Genie Sim scene: $SCENE"
"$TOOLS/bin/python" "$REPO/arms/sim/supermarket/tools/fetch_geniesim.py" --scene "$SCENE" --root "$ASSETS/geniesim"

log "done. launch with:"
echo "  bash $REPO/arms/sim/supermarket/run_sim.sh            # SCENE=$SCENE"
