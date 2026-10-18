#!/usr/bin/env bash
# LIVE judges' demo Isaac worker: repo-judges (basket_unload + director camera), default tmux socket, session picky-sim.
#   tmux new -d -s picky-sim /data/advaiv2/picky/run_sim_judges.sh
set -uo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
B=/data/advaiv2/picky
. "$B/sim-judges.env"
cd "$B/repo-judges"
mkdir -p "$B/logs"
$PY -m arms.sim.app 2>&1 | tee -a "$B/logs/judges-sim.log"
