# Deploying the Isaac sim worker on vhagar

The worker is one Python process on the GPU host **vhagar**. It hosts **every store** (side by side under
`/World/Stores/<store_id>`) and **every rover** from `data/fixtures/robots.yaml`, and connects **outbound** to the gateway's
`ws /ws/sim`. The gateway then routes each store whose `data_hash` matches to Isaac. All other stores stay on the 2D robot
fake, so nothing breaks while the worker is down.

**Verified on vhagar (2026-10-17):** Isaac Sim **6.0.0-rc.59** standalone (`python.sh` = Python 3.12.13), RTX 5090 32 GB.
Both stores + 3 rovers + all 12 cameras render; the taco request ran end to end on Isaac (`/api/requests` → shop goal on
Pico → 10/10 items picked → checkout).

Isaac Sim is installed at `/data/$USER/isaacsim`. That install is **read-only to us**: never `pip install` into it.
Our Python deps go into a `--target` dir that we add to `PYTHONPATH`.

## 0. Before you start (from the Mac)

```bash
ssh vhagar 'nvidia-smi --query-gpu=name,memory.total --format=csv; ls /data/$USER/isaacsim | head'
ssh vhagar '/data/$USER/isaacsim/python.sh -c "import sys; print(sys.version)"'   # 3.12 on Isaac 6.x
```
You need an RT-core GPU (RTX / L40S class). A100/H100 cannot render. If `ssh vhagar` (tailnet) times out, try the
campus hostname with the pinned key (`ssh -o HostKeyAlias=vhagar <user>@<campus FQDN>`). vhagar intermittently drops
NEW tcp connections (even IPv4 loopback) for minutes at a time; the worker reconnects by itself.

## 1. Put the repo on vhagar

The repo is private and may hold uncommitted work, so rsync it from the Mac (re-run after every pull):

```bash
# on the Mac, from the repo root
rsync -az --delete --exclude .venv --exclude .git --exclude node_modules --exclude arms/frontend --exclude __pycache__ \
  ./ vhagar:/data/$USER/picky/repo/
```

## 2. Python deps (outside the Isaac install)

Isaac 6.0's Python already ships everything else the worker needs: pydantic 2.11, websockets 12.0 (its sync client is
all `app.py` uses), PyYAML, Pillow, numpy and opencv. Only two small packages are missing; install them WITHOUT deps so
nothing Isaac ships gets shadowed:

```bash
ssh vhagar
PY=/data/$USER/isaacsim/python.sh
$PY -m pip install --no-deps --target /data/$USER/picky-pydeps pydantic-settings python-dotenv
```
Do NOT put websockets, pydantic, typing-extensions, numpy or torch into the deps dir (Kit extensions import Isaac's
copies). On an older Isaac without pydantic >= 2.8 or Pillow, add those to the same `--target` dir.

## 3. Environment

```bash
cat > /data/$USER/picky/sim.env <<'EOF'
export OMNI_KIT_ACCEPT_EULA=YES
export PY=/data/$USER/isaacsim/python.sh
export PYTHONPATH=/data/$USER/picky/repo:/data/$USER/picky-pydeps
export PICKY_DATA_DIR=/data/$USER/picky/repo/data
# The gateway: one on vhagar itself (below, simplest), the Mac's tailnet IP (gateway with GATEWAY_HOST=0.0.0.0),
# or http://localhost:8000 through a reverse tunnel (`ssh -N -R 8000:localhost:8000 vhagar` from the Mac).
export PICKY_GATEWAY_URL=http://localhost:8100      # localhost = ::1 first, then 127.0.0.1
export PICKY_SIM_TOKEN=dev-sim-token          # must equal the gateway's PICKY_SIM_TOKEN
export PICKY_SIM_STORES=store_ave,store_hill  # optional; default = every planogram in data/
export PICKY_SIM_WORKER_ID=isaac-vhagar
export PICKY_SIM_LABEL_DIR=/data/$USER/picky/labels
# export PICKY_SO101_URDF=/data/$USER/SO-ARM100/Simulation/SO101/so101_new_calib.urdf   # optional, experimental
# Optional visuals: build them first (arms/sim/ASSETS.md). Unset = primitive boxes / rovers as before; a path that
# does not exist yet only logs a warning and draws the primitives
export PICKY_ASSET_ROOT=/data/$USER/picky-assets/products                          # per-item grocery models
export PICKY_ROVER_USD=/data/$USER/picky-assets/robots/picky-rover/picky_rover.usda  # LeKiwi + SO-101 + lift
# export PICKY_SIM_ASSETS=off        # rollback switch: primitives for every SKU even with PICKY_ASSET_ROOT set
# export PICKY_SKU_ASSETS=...         # another SKU map (default $PICKY_DATA_DIR/sim/sku_assets.yaml)
EOF
```
Always start the worker **from the repo root with `-m`** (`cd /data/$USER/picky/repo && $PY -m arms.sim.app`): Isaac's
extension paths contain their own top-level `utils` package, and only the cwd entry puts ours first. Custom scripts
must also import `utils.config` (pydantic-settings) BEFORE creating `SimulationApp`: Kit prepends its own
typing-extensions, and a later first import fails with `cannot inherit from both a TypedDict type and a non-TypedDict
base class`.

### Gateway on vhagar (no tailnet in the sim link)

```bash
cd /data/$USER/picky/repo && uv sync          # base deps, Python 3.12 via uv
tmux new -d -s picky-gw 'cd /data/$USER/picky/repo && PICKY_REAL_ARMS=robot GATEWAY_HOST=localhost GATEWAY_PORT=8100 \
  PICKY_GATEWAY_URL=http://localhost:8100 uv run python -m arms.gateway.app'
# from the Mac: ssh -N -L 8100:localhost:8100 vhagar   ->  http://localhost:8100/docs
```
`GATEWAY_HOST=localhost` binds BOTH `127.0.0.1` and `[::1]` (still loopback-only): during vhagar's network blips IPv4
loopback TCP stops accepting new connections while `::1` keeps working, so the sim link and the tunnel survive them.

## 4. Smoke test without rendering (seconds, no GPU)

```bash
cd /data/$USER/picky/repo && source /data/$USER/picky/sim.env
$PY -m arms.sim.scene_spec                               # prints both stores, offsets, rovers, cameras
$PY -m arms.sim.app --backend null --max-seconds 20      # links up with the gateway; grey frames
$PY -m arms.sim.app --backend sketch --max-seconds 40    # same link, Pillow-rendered frames
```
`curl -s localhost:8100/api/sim/status` should show `"connected": true` and both stores in `isaac_store_ids`.

## 5. Run Isaac in tmux

```bash
tmux new -s picky-sim          # re-attach later with: tmux a -t picky-sim
cd /data/$USER/picky/repo && source /data/$USER/picky/sim.env
$PY -m arms.sim.app 2>&1 | tee /data/$USER/picky/logs/sim.log
```
- Timings on vhagar (warm shader cache): `SimulationApp` up in ~9-10 s, store_ave (1,420 prims, 1,331 units) in ~10 s,
  store_hill in ~3.5 s, linked ~25 s after launch. The first launch on a fresh box compiles shaders (minutes).
- Each camera's render product is created on first use (~0.2-0.4 s, two synchronous replicator steps).
- Logs: `store store_ave built at (0.0, 0.0, 0.0)`, `sim link up`, `camera rover-ave-1.front: render product ... ready`,
  and every 200 frames `isaac render: ... avg N ms/frame` (gateway log: `sim worker isaac-vhagar attached`).
- GPU: ~3.6-4.6 GB for the worker with every camera used.
- Fallback: `$PY -m arms.sim.app --backend sketch` renders with Pillow (no Isaac). Use it if Isaac won't start.

## 6. Per-item models + rover USD (optional)

The assets live outside the repo, on vhagar: `/data/$USER/picky-assets/products` (~3 GB) and
`/data/$USER/picky-assets/robots/picky-rover/{picky_rover.usda,lekiwi.usd}`. `arms/sim/ASSETS.md` gives the exact
commands to rebuild them, plus the licenses and the credit line. With the two env lines above the worker logs these
lines:

```
per-item models from /data/.../products (125 SKUs mapped in .../data/sim/sku_assets.yaml); rover: .../picky_rover.usda
store store_ave: 1218 units on per-item models, 10 shelf tags     # SimLog: "117 SKUs on per-item models, ..."
rover USD ...: SO-101 puppet ready in 6.1s (reach table max error ... mm; z_ref 0.319 m)
```

Measured on vhagar on 2026-10-17 with the live worker (RTX 5090, both stores, 3 rovers, models + rover USD on;
taco request + staff shelf scan through the gateway on :8100):

- Launch to `sim link up`: ~46 s warm (store_ave ~20 s, the reach table 5.3 s once for all rovers, store_hill ~8 s).
  The first launch after a deploy took ~99 s: its first frames load every model texture (~56 s for store_ave).
- A new render product is ready in 0.55-0.7 s. The worker's `isaac render` average is ~80 ms per frame (12 render
  products, 4-6 active), and gateway snapshots took a median of 0.24 s (p90 0.65 s) while ~4 cameras were polled.
- GPU memory: ~3.8 GB after the build, ~5.9 GB with all 12 cameras used.
- `PICKY_SIM_AMBIENT` (default 0.1) sets the RTX ambient fill for the shelf interiors; 0.6 already washes the store out.
- A unit, SKU or rover whose files are missing falls back to the primitive with a warning; nothing else changes.
- Rollback: `PICKY_SIM_ASSETS=off` and / or `unset PICKY_ROVER_USD`, then restart the worker.

## 7. Verify

```bash
curl -s localhost:8100/api/sim/status | python3 -m json.tool | head -30     # isaac_store_ids / isaac_robot_ids
curl -s -o front.jpg localhost:8100/api/cameras/rover-ave-1.front/snapshot.jpg
curl -s -o ceiling.jpg localhost:8100/api/cameras/store_ave.ceiling-1/snapshot.jpg
curl -s -X POST localhost:8100/api/requests -H 'content-type: application/json' \
  -d '{"text":"tacos for 6, under $40, one vegetarian","user_id":"maya"}'
```

## How the Isaac backend renders (Isaac 6.0, measured on vhagar)

- The rgb annotator only fills after `rep.orchestrator.step()` has run, and plain `app.update()` gives no reliable
  freshness: after a robot + camera move it took 2-6 updates (5 for a resumed render product) depending on the number
  of enabled render products and GPU load, i.e. stale frames from the old pose.
- So every capture whose camera or scene changed runs ONE `rep.orchestrator.step(rt_subframes=1)` (default
  `delta_time`; `0.0` does not wait) and was exact in every trial. One step serves all enabled cameras, so stream frames
  due in the same tick share it. Cost: ~60-100 ms with 1 enabled render product, ~120-135 ms with 3, ~270 ms with 12;
  repeat captures of an unchanged scene are just a copy + JPEG.
- Render products idle for 3 s are paused (`hydra_texture.set_updates_enabled(False)`): a resumed one gets a second step.
- Snapshot latency through the gateway: ~60-150 ms warm, ~0.5 s for a camera's first use.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `gateway rejected sim worker: contracts mismatch` | rsync the repo again (both ends need the same CONTRACTS_VERSION major.minor) |
| rejected with a token error | `PICKY_SIM_TOKEN` differs from the gateway's |
| `Connection refused` / `timed out` loop | gateway not listening (0.0.0.0 for remote workers), wrong tailnet IP, tunnel down, or vhagar's network blip (it reconnects by itself) |
| store stays on the fake (`fake_store_ids`) | data drift: the gateway sends a store-level `ResetCmd` with its planogram, and the worker rebuilds it (`SimHosted` follows). Check the worker log for errors |
| snapshots are the gateway's "offline (not_ready)" placeholder | the annotator returned nothing: the worker must run `rep.orchestrator.step` (current `app.py` does); check the log for `empty annotator frame` |
| GPU memory pressure | each camera id gets its own render product, created on first use; idle ones pause after 3 s. Limit `PICKY_SIM_STORES` or the web app's live streams |
| `ModuleNotFoundError: pydantic_settings` / `No module named 'utils.log'` | step 2 into the deps dir; run from the repo root with `-m` |
| `cannot inherit from both a TypedDict type and a non-TypedDict base class` | `utils.config` imported after `SimulationApp` (custom scripts): import it first |
| Isaac crashes on import of our deps | a deps-dir package shadows an Isaac one. Remove it (usually pydantic / typing-extensions / websockets) |

## 8. T1 grasps with the Perceptron Mk1.5 policy (contracts 0.2.2, feat/perceptron-vla)

A worker from this branch advertises `arm_joints` + `arm_ik`. With `PICKY_VLA_T1=perceptron` on the gateway (and
`PERCEPTRON_API_KEY`), every `grasp` step resolves to T1: the robot arm (backend) runs the loop, one `ArmJointsCmd` per
action chunk; Perceptron Mk1.5 sees the side + wrist policy cameras and calls a tool (`reach_point` on an image ->
ray to the planogram's shelf face -> `ArmIkCmd` on the worker -> joint rows at 30 Hz; `set_gripper close` -> the
sticky grasp attaches the unit). Pictures while moving: side + wrist every 8 rows (~4 pairs/s) stream live to the
robot's front / wrist view; the front camera takes 2 pictures/s (blurred, retained evidence) while any VLA step
drives. A separate test stack on vhagar keeps the live demo (:8100) untouched:

```bash
# /data/$USER/picky/sim-pvla.env = sim.env with repo -> repo-pvla, :8100 -> :8101, PICKY_SIM_WORKER_ID=isaac-pvla,
# PICKY_SIM_STORES=store_ave, PICKY_SIM_LABEL_DIR=.../labels-pvla
tmux new -d -s picky-pvla-gw  /data/$USER/picky/run_pvla_gw.sh    # PICKY_REAL_ARMS=robot,evidence,cameras PICKY_DEV=1
                                                                  # PICKY_VLA_T1=perceptron GATEWAY_PORT=8101 + secrets
tmux new -d -s picky-pvla-sim /data/$USER/picky/run_pvla_sim.sh   # source sim-pvla.env; $PY -m arms.sim.app
curl -s localhost:8101/api/fleet/robots/rover-ave-1/vla          # t1_pretrained available, "perceptron-mk1.5 (tool policy)"
curl -s -X POST localhost:8101/api/dev/plans -H 'content-type: application/json' \
  -d '{"robot_id":"rover-ave-1","store_id":"store_ave","text":"pick up a jar of salsa"}'
grep -E 'Mk1.5 step|sticky grasp|T1 rover' /data/$USER/picky/logs/pvla-gw.log
```

Measured on vhagar (2026-10-18, RTX 5090, store_ave, rover-ave-1 = LeKiwi + SO-101 rover USD, fake catalog / planner
/ verifier, real robot + evidence): 4 of 4 dev plans (salsa x2, olive oil, diced tomatoes) completed end to end
(navigate -> align -> scan -> ground -> **T1 grasp** -> T0 place -> verify -> checkout -> handoff). Each grasp took 2
Mk1.5 calls (`reach_point(side, x, y)` then `set_gripper(close)`; 0.37-0.90 s per call), IK error 0.0 cm, the sticky
grasp attached a unit of the target slot 0.0 cm from the jaw center, ~4-5 s from joint mode to attached, 3.4-4.0
side + wrist pairs/s while the arm moved (all 22 policy pictures of a grasp reached `/ws/frames`). Navigation: 2.0
front pictures/s while driving (26-33 per drive); the Perceptron look-out answered on every approach / arrival (the
products are seen from above there, so it mostly says "not visible": advisory only). Extra worker GPU: ~3.3 GB.

## 9. The whole loop on the test stack: real catalog database + director video (2026-10-18)

Same separate stack as §8, now with every loop service real: `PICKY_REAL_ARMS=robot,evidence,cameras,verifier,agent,catalog`
(Gemini planner + request interpreter, Perceptron Mk1.5 verifier, Mk1.5 T1 grasps, and the catalog on a real Postgres).
Everything runs on vhagar's own tmux socket, so the default socket (live demo) is never touched:

```bash
tmux -L picky-pvla ls        # picky-pvla-db (Postgres :5433), picky-pvla-gw (:8101), picky-pvla-sim (worker)
```

- **Catalog database, no Docker:** `/data/$USER/picky-pg` holds a pip `pgserver` venv (Postgres 16 + pgvector
  binaries) and `run_db.sh` (`postgres -D pgdata -p 5433`, localhost only). Seed it from the planogram after every
  rsync: `cd repo-pvla && DATABASE_URL=postgresql+psycopg://picky@localhost:5433/picky uv run --frozen --no-sync python -m arms.catalog.seed --schema --apply`.
  Every slot location the robot drives to (aisle, fixture, level, item xyz, approach pose) and every stock count comes
  from the `slots` / `inventory` / `lots` tables per call; the gateway log prints `catalog db: <SKU> @ store_ave -> slot ...`
  for each lookup.
- **Deps:** the gateway venv needs `uv sync --frozen --extra catalog --extra perception --extra agent` (psycopg +
  greenlet, the perceptron SDK, google-genai) and must start with `uv run --frozen --no-sync` (a plain `uv run` re-syncs
  to base deps and drops them).
- **Request:** `POST /api/requests {"text": "tacos for 6, under $40, one vegetarian", "user_id": "maya", "store_id": "store_ave"}`.
  Without `store_id` the fleet picks store_hill (cheaper, on the 2D fake), not the Isaac store.
- **Director video:** `PICKY_SIM_DIRECTOR=rover-ave-1` (+ `_DIR`, `_FPS`, `_VIEW`) in `sim-pvla.env` turns on the chase
  camera and the frame tee (`arms/sim/director.py`). Record a run with
  `uv run --frozen --no-sync python arms/sim/tools/director/record.py --store store_ave --text "..." --out RUN`
  (events, approvals answered like the web app, store planogram) and composite it with the SmolVLA venv's PyAV:
  `$V arms/sim/tools/director/render.py RUN --director $PICKY_SIM_DIRECTOR_DIR/rover-ave-1 --out run.mp4 --speed 4 --gif run.gif`.
- **Shared GPU:** when other jobs load vhagar (a SmolVLA fine-tune at ~90 % GPU and load ~28 on 16 cores during this
  test) the worker's replicator steps go from ~15-30 ms to 150-560 ms. Keep live streams off (`record.py` does not ask
  for `/ws/frames` unless `--live-frames`): every streamed camera is one more render product in every step.

Measured (2026-10-18 02:00-02:30, taco request, Gemini plans, Mk1.5 T1 + verifier, catalog on Postgres; the GPU shared
with a SmolVLA fine-tune): T1 Mk1.5 grasps 10 of 11 ok in run 3 (one `policy_timeout` after 9 chunks -> T0 scripted
grasp, ok); 0.6-1.2 s per Mk1.5 call; 1.3-1.8 side + wrist pairs/s while the arm moved; navigation 30-90 s per leg
(one 90 s `navigate_to` deadline hit, retried). What broke and is fixed on this branch: the basket audit failed on
occlusion (top-down camera sees 2 of 11 units) and a passing re-verify did not close the provisional lines (checkout
stayed `basket_audit_pending`); Perceptron called a held cheese bag / meat tray "gripper empty" and overrode the sim's
grasp state (redo loops, one item skipped); a silent evidence capture tripped the stall detector; Mk1.5 re-closed the
gripper where it had just missed.

Open at 03:40 PT:
- **Run 5** (02:36, with most of the fixes above) had picked 4 items when vhagar went offline at about 02:47. Its
  outcome is unknown.
- Commit 095f604 (Mk1.5 re-aims after a missed close) is pushed but not deployed.

When the host is back:
1. Read run 5: `python3 /data/$USER/picky/pvla-tools/timeline.py /data/$USER/picky/pvla-runs/run5` and its
   `summary.json`.
2. Render it with `render.py`.
3. Rsync the branch to `repo-pvla`, re-seed the catalog, and restart `picky-pvla-gw` on `-L picky-pvla`.
4. Run the spread-out list (dish soap, sports drink, coffee, olive oil, rice, muffins, frozen peas, ground turkey:
   8 fixtures, shelf levels 0–2) and the same request with `PICKY_VLA_T1=smolvla`.

## 10. Judges' demo: checkout unload by the arm (contracts 0.2.4)

The worker now implements `pick_from_basket` / `place_on_counter` and advertises the `basket_unload` capability in its
hello: at the checkout counter the SO-101 (joint mode, IK on the rover model) lowers the lift to 0.32 m, swings back
over the basket, attaches one unit (magic grasp, decided by code), lifts it out, then raises the lift and lays the
unit on the counter spot the plan chose (`planogram.counter_spots`: bays of 3 on the checkout fixture's top,
left → right). The unit stays in the scene (`on_counter`) until the handoff's `unload_basket` (staff bag it).

- **Redeploy the worker** from this branch (`git pull` on vhagar, restart the tmux session as in section 5). A worker
  started from an older checkout still connects (0.2.x), but its hello lacks `basket_unload`: the gateway then skips
  the per-unit unload and the handoff unloads the basket at once (the 0.2.3 behaviour). `GET /api/fleet/robots/{id}/vla`
  shows `basket_unload: true` on the T0 tier when the connected worker has it.
- Nothing else to configure; `PICKY_UNLOAD_BY_ARM=false` on the gateway turns the unload sequence off.
- Smoke test without a GPU: `uv run pytest arms/sim/tests/test_worker_unload.py` (NullBackend).
