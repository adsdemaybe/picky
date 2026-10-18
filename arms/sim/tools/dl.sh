#!/bin/bash
# Picky grocery product assets: download step (run on vhagar inside tmux picky-assets-products-dl; arms/sim/ASSETS.md)
# Env: PICKY_ASSET_ROOT (default /data/$USER/picky-assets/products); optional PICKY_ASSET_ENV_SH (sourced first:
# caches / proxies for huggingface_hub).
set -uo pipefail
R=${PICKY_ASSET_ROOT:-/data/$USER/picky-assets/products}
T=$(cd "$(dirname "$0")" && pwd)   # this tools dir: hf_folders.py, hf_ids.txt, rzip.py, rc_select.json
if [ -n "${PICKY_ASSET_ENV_SH:-}" ] && [ -f "$PICKY_ASSET_ENV_SH" ]; then source "$PICKY_ASSET_ENV_SH"; fi
export HF_HUB_ENABLE_HF_TRANSFER=0
mkdir -p $R/_logs
PY=$R/.venv/bin/python
ts() { date +%s; }

echo "== [1/3] NVIDIA SimReady (HF nvidia/simready-assets, CC-BY-4.0)"
t0=$(ts)
$PY -I $T/hf_folders.py nvidia/simready-assets $T/hf_ids.txt $R/simready --exclude '/\.thumbs/' --exclude '/source_assets/.*\.(png|jpg)$' > $R/_logs/simready_dl.log 2>&1
echo "simready rc=$? secs=$(( $(ts)-t0 )) size=$(du -sh $R/simready | cut -f1)"

echo "== [2/3] Lightwheel kitchen objects (HF nvidia/PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF, CC-BY-4.0)"
LW="flour_bag aluminum_foil sandwich_bread hotdog_bun lettuce honey_bottle mayonnaise cream_cheese_stick juice shrimp turkey_slice paprika cinnamon turmeric syrup_bottle salt_and_pepper_shaker jar tray tupperware pizza mustard"
INC=(); for n in $LW; do INC+=(--include "objects_lightwheel/$n.zip"); done
t0=$(ts)
$R/.venv/bin/hf download nvidia/PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF --repo-type dataset \
  --local-dir $R/lightwheel/_zips "${INC[@]}" --include "README.md" > $R/_logs/lightwheel_dl.log 2>&1
rc=$?
for z in $R/lightwheel/_zips/objects_lightwheel/*.zip; do (cd $R/lightwheel && unzip -q -o "$z" -x "__MACOSX/*"); done
echo "lightwheel rc=$rc secs=$(( $(ts)-t0 )) size=$(du -sh $R/lightwheel | cut -f1)"

echo "== [3/3] RoboCasa aigen objects (Box: utexas nwi1vrn5pgbo95kushkasa3nx1i012ff.zip, CC-BY-4.0), selective HTTP-range extraction"
t0=$(ts)
RX=$($PY -I -c "import json;d=json.load(open('$T/rc_select.json'));print(' '.join('^aigen_objs/%s/%s/'%(c,o) for c,v in d.items() for o in v))")
$PY -I $T/rzip.py extract https://utexas.box.com/shared/static/nwi1vrn5pgbo95kushkasa3nx1i012ff.zip $R/robocasa $RX > $R/_logs/robocasa_dl.log 2>&1
echo "robocasa rc=$? secs=$(( $(ts)-t0 )) size=$(du -sh $R/robocasa | cut -f1)"
echo "== done $(date)"
