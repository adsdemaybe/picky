#!/usr/bin/env bash
# LIVE judges' demo gateway: repo-judges (feat/judges-demo) on localhost:8100, default tmux socket, session picky-gw.
#   tmux new -d -s picky-gw /data/advaiv2/picky/run_gw_judges.sh
# Rollback to the previous live gateway: tmux kill-session -t picky-gw; tmux new -d -s picky-gw /data/advaiv2/picky/run_gw.sh
set -uo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
B=/data/advaiv2/picky
cd "$B/repo-judges"
set -a; . "$HOME/.config/picky/secrets.env"; set +a                # PERCEPTRON_API_KEY, GEMINI_API_KEY (never printed)
[ -f "$B/judges.env" ] && { set -a; . "$B/judges.env"; set +a; }    # overrides (PICKY_VLA_T1=scripted, ...)
export GATEWAY_HOST=localhost GATEWAY_PORT=8100 PICKY_GATEWAY_URL=http://localhost:8100
export PICKY_REAL_ARMS="${PICKY_REAL_ARMS:-robot,evidence,cameras,verifier,routing}"
export PICKY_EXEC_MODE="${PICKY_EXEC_MODE:-plan}"
export PICKY_VLA_T1="${PICKY_VLA_T1:-perceptron}"                   # Mk1.5 T1 shelf grasps; scripted = T0 only
export PICKY_MODEL_FAST="${PICKY_MODEL_FAST:-gemini-3.5-flash}"     # gemini-3.8-flash answers 429 (quota) tonight
export PICKY_PURCHASES_DB="${PICKY_PURCHASES_DB:-$B/judges-var/purchases.sqlite}"
export PICKY_SIM_TOKEN="${PICKY_SIM_TOKEN:-dev-sim-token}"
mkdir -p "$B/logs" "$B/judges-var"
uv run --frozen --no-sync python -m arms.gateway.app 2>&1 | tee -a "$B/logs/judges-gw.log"
