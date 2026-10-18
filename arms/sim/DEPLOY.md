# Deploying the Isaac sim worker on vhagar

The worker is one Python process on the GPU host **vhagar**. It hosts **every store** (side by side under
`/World/Stores/<store_id>`) and **every rover** from `data/fixtures/robots.yaml`, and connects **outbound** to the gateway's
`ws /ws/sim`. The gateway then routes each store whose `data_hash` matches to Isaac. All other stores stay on the 2D robot
fake, so nothing breaks while the worker is down.

Isaac Sim is already installed at `/data/$USER/isaacsim`. That install is **read-only to us**: never `pip install` into it.
Our Python deps go into a `--target` dir that we add to `PYTHONPATH`.

## 0. Before you start (from the Mac)

```bash
ssh vhagar 'nvidia-smi --query-gpu=name,memory.total --format=csv; ls /data/$USER/isaacsim | head'
ssh vhagar '/data/$USER/isaacsim/python.sh -c "import sys; print(sys.version)"'   # expect 3.11 (Isaac 5.x) or 3.12 (6.x)
```
You need an RT-core GPU (RTX / L40S class). A100/H100 cannot render.

## 1. Put the repo on vhagar

The repo is private and may hold uncommitted work, so rsync it from the Mac (re-run after every pull):

```bash
# on the Mac, from the repo root
rsync -az --delete --exclude .venv --exclude node_modules --exclude .next --exclude __pycache__ \
  ./ vhagar:/data/$USER/picky/
```
(If `gh` is set up on vhagar, `git clone https://github.com/adsdemaybe/picky /data/$USER/picky` works too.)

## 2. Python deps (outside the Isaac install)

```bash
ssh vhagar
PY=/data/$USER/isaacsim/python.sh
DEPS=/data/$USER/picky-pydeps
mkdir -p $DEPS
# What does Isaac already ship? Don't shadow a pydantic >= 2.8 it already has.
$PY -c "import pydantic; print('pydantic', pydantic.VERSION)" || true
$PY -c "import PIL; print('pillow', PIL.__version__)" || true
$PY -c "import numpy; print('numpy', numpy.__version__)"
# Always needed:
$PY -m pip install --target $DEPS "websockets>=13" "pydantic-settings>=2.4" python-dotenv pyyaml
# Only if Isaac's pydantic is missing or < 2.8 (the contracts need pydantic v2):
$PY -m pip install --target $DEPS "pydantic>=2.8"
# Only if Isaac has no Pillow (needed for label textures + JPEG; cv2 is used instead if present):
$PY -m pip install --target $DEPS "pillow>=10.4"
```
Do NOT install numpy or torch into `$DEPS`. Use Isaac's copies.

## 3. Environment

```bash
cat > /data/$USER/picky-sim.env <<'EOF'
export OMNI_KIT_ACCEPT_EULA=YES
export PYTHONPATH=/data/$USER/picky-pydeps:/data/$USER/picky
export PICKY_DATA_DIR=/data/$USER/picky/data
# The gateway: the Mac's tailnet IP (Mac: `tailscale ip -4`; run the gateway with GATEWAY_HOST=0.0.0.0),
# or http://localhost:8000 with a reverse tunnel (below), or a gateway running on vhagar itself.
export PICKY_GATEWAY_URL=http://100.x.y.z:8000
export PICKY_SIM_TOKEN=dev-sim-token          # must equal the gateway's PICKY_SIM_TOKEN
export PICKY_SIM_STORES=store_ave,store_hill  # optional; default = every planogram in data/
export PICKY_SIM_WORKER_ID=isaac-vhagar
export PICKY_SIM_LABEL_DIR=/data/$USER/picky-sim-labels
# export PICKY_SO101_URDF=/data/$USER/SO-ARM100/Simulation/SO101/so101_new_calib.urdf   # optional, experimental
EOF
```
If the tailnet route is blocked, tunnel from the Mac instead: `ssh -N -R 8000:localhost:8000 vhagar`, then use
`PICKY_GATEWAY_URL=http://localhost:8000`.

## 4. Smoke test without rendering (seconds, no GPU)

```bash
cd /data/$USER/picky && source /data/$USER/picky-sim.env
$PY -m arms.sim.scene_spec                               # prints both stores, offsets, rovers, cameras
$PY -m arms.sim.app --backend null --max-seconds 20      # links up with the gateway; grey frames
```
On the Mac, `curl localhost:8000/api/sim/status` should show `"connected": true` and both stores in `isaac_store_ids`.

## 5. Run Isaac in tmux

```bash
tmux new -s picky-sim          # re-attach later with: tmux a -t picky-sim
cd /data/$USER/picky && source /data/$USER/picky-sim.env
$PY -m arms.sim.app 2>&1 | tee /data/$USER/picky-sim.log
```
- The **first launch compiles shaders (10–20+ min)**. Later launches take about 1 minute. Building the stores takes about 2,000
  product units with label quads, roughly 10–30 s.
- Logs: `store store_ave built at (0.0, 0.0, 0.0)`, then `sim link up`, and `sim worker isaac-vhagar attached` in the gateway log.
- Fallback: `$PY -m arms.sim.app --backend sketch` renders with Pillow (no Isaac). Use it if Isaac won't start.

## 6. Verify from the Mac

```bash
curl -s localhost:8000/api/sim/status | python3 -m json.tool | head -30   # isaac_store_ids / isaac_robot_ids
open "http://localhost:8000/api/cameras/rover-ave-1.front/snapshot.jpg"
open "http://localhost:8000/api/cameras/store_ave.ceiling-1/snapshot.jpg"
PICKY_REAL_ARMS=robot make demo        # the taco trip, driven by Isaac
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `gateway rejected sim worker: contracts mismatch` | rsync the repo again (both ends need the same CONTRACTS_VERSION major.minor) |
| rejected with a token error | `PICKY_SIM_TOKEN` differs from the gateway's |
| `Connection refused` loop | gateway not listening on 0.0.0.0, wrong tailnet IP, or the tunnel is down |
| store stays on the fake (`fake_store_ids`) | data drift: the gateway sends a store-level `ResetCmd` with its planogram, and the worker rebuilds it (`SimHosted` follows). Check the worker log for errors |
| black / empty frames | first frames after a build are black; wait a few seconds. Check `nvidia-smi` for the process |
| GPU memory pressure | each camera id gets its own render product, created on first use. Idle ones pause after 5 s. Limit `PICKY_SIM_STORES` or the web app's live streams |
| `ModuleNotFoundError: pydantic_settings / websockets / yaml` | step 2 into `$DEPS`, and check `PYTHONPATH` |
| Isaac crashes on import of our deps | a `$DEPS` package shadows an Isaac one. Remove it from `$DEPS` (usually pydantic / typing-extensions) |
