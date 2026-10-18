#!/usr/bin/env bash
# Hi-fi recording switch for the LIVE judges' Isaac worker (tmux picky-sim). Runs ON vhagar.
#   bash /data/advaiv2/picky-rec/restart_sim_hifi.sh apply      # append sim-hifi.env block (+ JPEG patch), restart
#   bash /data/advaiv2/picky-rec/restart_sim_hifi.sh rollback   # remove the block (+ unpatch), restart
#   bash /data/advaiv2/picky-rec/restart_sim_hifi.sh status     # what is applied + what the gateway reports
# PATCH_JPEG=0 skips the 1-line arms/sim/app.py patch (JPEG quality env knob). Touches only:
#   /data/advaiv2/picky/sim-judges.env, /data/advaiv2/picky/repo-judges/arms/sim/app.py (1 line), tmux picky-sim,
#   and the arms.sim.app process whose cwd is /data/advaiv2/picky/repo-judges. Never the gateway, never kill-server.
set -uo pipefail
B=/data/advaiv2/picky
ENVF=$B/sim-judges.env
APP=$B/repo-judges/arms/sim/app.py
HIFI=/data/advaiv2/picky-rec/sim-hifi.env
GW=http://localhost:8100
OLD='JPEG_QUALITY = 75'
NEW='JPEG_QUALITY = int(os.environ.get("PICKY_SIM_JPEG_QUALITY") or 75)'
ts=$(date +%Y%m%d-%H%M%S)

ours() { for p in $(pgrep -u "$(id -u)" -f "arms.sim.app"); do [ "$(readlink /proc/$p/cwd 2>/dev/null)" = "$B/repo-judges" ] && echo "$p"; done; }

restart() {
  echo "stopping picky-sim (our worker: $(ours | tr '\n' ' '))"
  tmux kill-session -t picky-sim 2>/dev/null
  for _ in $(seq 1 40); do P=$(ours); [ -z "$P" ] && break; kill $P 2>/dev/null; sleep 1; done
  P=$(ours); [ -n "$P" ] && { echo "kill -9 $P"; kill -9 $P; sleep 3; }
  tmux new -d -s picky-sim "$B/run_sim_judges.sh"
  echo "picky-sim relaunched; waiting for Isaac (up to 6 min)..."
  for i in $(seq 1 72); do
    ids=$(curl -s -m 5 $GW/api/sim/status | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin).get("isaac_robot_ids", [])))' 2>/dev/null)
    [ -n "$ids" ] && { echo "Isaac up after ~$((i * 5)) s: $ids"; status_cams; return 0; }
    sleep 5
  done
  echo "Isaac did NOT come back in 6 min: tail $B/logs/judges-sim.log"; tail -5 $B/logs/judges-sim.log | cut -c1-200; return 1
}

status_cams() {
  curl -s -m 5 "$GW/api/cameras?store_id=store_ave" | python3 -c '
import json, sys
for c in json.load(sys.stdin):
    if c["kind"] in ("director", "ceiling") or c["camera_id"].endswith(".front"):
        m = c.get("mount") or {}
        print("  %-28s %sx%s source=%s fps=%s" % (c["camera_id"], m.get("width"), m.get("height"), c.get("source"), c.get("fps")))'
}

case "${1:-status}" in
  apply)
    grep -q '# >>> hifi' "$ENVF" || {
      cp -p "$ENVF" "$ENVF.bak-hifi-$ts"
      { echo '# >>> hifi (restart_sim_hifi.sh; rollback: restart_sim_hifi.sh rollback)'; grep -E '^export ' "$HIFI"; echo '# <<< hifi'; } >> "$ENVF"
      echo "appended hifi block to $ENVF (backup $ENVF.bak-hifi-$ts)"
    }
    if [ "${PATCH_JPEG:-1}" = 1 ] && grep -qx "$OLD" "$APP"; then
      cp -p "$APP" "$APP.bak-hifi-$ts"
      sed -i "s/^JPEG_QUALITY = 75\$/$NEW/" "$APP"
      grep -n "PICKY_SIM_JPEG_QUALITY" "$APP" && python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read()); print("app.py parses")' "$APP"; echo "patched $APP (backup $APP.bak-hifi-$ts)"
    fi
    restart
    ;;
  rollback)
    sed -i '/# >>> hifi/,/# <<< hifi/d' "$ENVF" && echo "removed hifi block from $ENVF"
    grep -q "PICKY_SIM_JPEG_QUALITY" "$APP" && sed -i "s/^JPEG_QUALITY = int(os.environ.get(\"PICKY_SIM_JPEG_QUALITY\") or 75)\$/$OLD/" "$APP" && echo "unpatched $APP"
    restart
    ;;
  status)
    sed -n '/# >>> hifi/,/# <<< hifi/p' "$ENVF"; grep -n "^JPEG_QUALITY" "$APP"; echo "worker pids: $(ours | tr '\n' ' ')"; status_cams
    ;;
  *) echo "usage: $0 apply|rollback|status"; exit 2 ;;
esac
