#!/usr/bin/env bash
# Deploy the judges' demo backend to vhagar as the LIVE stack (default tmux socket): picky-gw (:8100) + picky-sim.
#   ./deploy_vhagar.sh            rsync + deps + restart the gateway + (GPU permitting) start the Isaac worker
#   ./deploy_vhagar.sh --no-sim   gateway only
# Never touches other tmux sockets (picky-pvla, picky-so101, picky-ft) or picky-vla; never kill-server.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC:-$HERE/../../..}"   # repo root (this script lives in scripts/demo/vhagar)
B=/data/advaiv2/picky
SIM=1; [ "${1:-}" = "--no-sim" ] && SIM=0
r() { ssh -o ConnectTimeout=15 vhagar "$@" < /dev/null; }

echo "== reach vhagar"; r 'hostname; tmux ls 2>/dev/null || true'
echo "== rsync $SRC -> $B/repo-judges"
rsync -az --delete --exclude .venv --exclude .git --exclude node_modules --exclude arms/frontend --exclude __pycache__ \
  --exclude .pytest_cache --exclude .ruff_cache --exclude var/ "$SRC/" vhagar:$B/repo-judges/
scp -q "$HERE/run_gw_judges.sh" "$HERE/run_sim_judges.sh" vhagar:$B/
r "chmod +x $B/run_gw_judges.sh $B/run_sim_judges.sh; mkdir -p $B/logs $B/judges-var"

echo "== sim-judges.env (from sim.env: repo -> repo-judges, + director camera, store_ave only)"
r "set -e; cd $B; if [ ! -f sim-judges.env ]; then
  sed -E 's#(/picky/repo)([:/\"]|\$)#\\1-judges\\2#g' sim.env > sim-judges.env
  { echo; echo '# judges demo (deploy_vhagar.sh)';
    echo 'export PICKY_SIM_DIRECTOR=all';
    echo 'export PICKY_SIM_DIRECTOR_SIZE=960x540';
    echo 'export PICKY_SIM_STORES=store_ave';
    echo 'export PICKY_GATEWAY_URL=http://localhost:8100'; } >> sim-judges.env
  grep -q PICKY_ASSET_ROOT= sim-judges.env || echo 'export PICKY_ASSET_ROOT=/data/advaiv2/picky-assets/products' >> sim-judges.env
  grep -q PICKY_ROVER_USD= sim-judges.env || echo 'export PICKY_ROVER_USD=/data/advaiv2/picky-assets/robots/picky-rover/picky_rover.usda' >> sim-judges.env
fi; grep -E '^export (PYTHONPATH|PICKY_DATA_DIR|PICKY_GATEWAY_URL|PICKY_SIM_[A-Z_]*|PICKY_ASSET_ROOT|PICKY_ROVER_USD)=' sim-judges.env"

echo "== gateway deps (uv sync --frozen --extra perception --extra agent --extra routing)"
r "export PATH=\$HOME/.local/bin:\$PATH; cd $B/repo-judges && uv sync --frozen --extra perception --extra agent --extra routing 2>&1 | tail -2"
r "test -f \$HOME/.config/picky/secrets.env && grep -oE '^(export )?[A-Z_]+=' \$HOME/.config/picky/secrets.env | tr '\n' ' '; echo"

echo "== restart picky-gw from repo-judges"
r "tmux kill-session -t picky-gw 2>/dev/null || true; tmux new -d -s picky-gw $B/run_gw_judges.sh"
r 'for i in $(seq 1 60); do curl -s -m 2 localhost:8100/health >/dev/null && break; sleep 1; done; curl -s localhost:8100/health | head -c 400; echo'

if [ "$SIM" = 1 ]; then
  echo "== GPU check (need >= 7000 MiB free; waits up to 30 min, re-checking every 2 min)"
  for i in $(seq 1 16); do
    FREE=$(r 'nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1' | tr -dc 0-9)
    echo "   free ${FREE} MiB"; [ "${FREE:-0}" -ge 7000 ] && break
    [ "$i" = 16 ] && { echo "!! not enough GPU memory: Isaac NOT started (gateway runs the 2D sim)"; exit 3; }
    sleep 120
  done
  echo "== (re)start picky-sim from repo-judges"
  r "tmux kill-session -t picky-sim 2>/dev/null || true; tmux new -d -s picky-sim $B/run_sim_judges.sh"
  r 'for i in $(seq 1 60); do curl -s localhost:8100/api/sim/status | grep -q "\"isaac_robot_ids\":\[\"rover" && break; sleep 5; done; curl -s localhost:8100/api/sim/status | head -c 600; echo'
fi
echo "== checks"
r 'curl -s localhost:8100/api/fleet/robots/rover-ave-1/vla | grep -o "\"tier\":\"[a-z0-9_]*\",\"available\":[a-z]*\|\"basket_unload\":[a-z]*" | tr "\n" " "; echo;
   curl -s "localhost:8100/api/cameras?robot_id=rover-ave-1" | grep -o "\"camera_id\":\"[^\"]*\",[^}]*\"source\":\"[a-z]*\"" | sed -E "s/,.*\"source\"/ source/"'
