# Picky: judges' demo runbook (Sun Oct 18, judging 11:30 AM to 2:00 PM)

This is the live demo: suggestion cards from purchase history, then order, then the robot shops in Isaac Sim
(livestreamed), then photo checks, then the arm unloads the basket onto the counter, then the Visa payment agent, then
the receipt, then purchase history.

| What | Where |
|---|---|
| Web app (production build, gateway = `http://localhost:8100`) | **http://localhost:3001** (Home), then `/watch?trip=…` (projector page) |
| Gateway (FastAPI) | vhagar `localhost:8100` (tmux `picky-gw`), reached through an SSH tunnel on Mac `localhost:8100` |
| Isaac worker | vhagar tmux `picky-sim` (store_ave, Pico = rover-ave-1, LeKiwi + SO-101 rover, per-item grocery models, director camera) |
| Backend branch | `feat/judges-demo` (deployed copy: vhagar `/data/advaiv2/picky/repo-judges`) |
| Web app branch | `feat/frontend-judges-demo` (Mac: `…/scratchpad/picky-fe-judges/arms/frontend`) |
| Backup recordings | `…/scratchpad/judges-demo/` (see the end of this file) |

`…/scratchpad` = `$SCRATCH`

> **State at 09:15 Sun: LIVE on vhagar.** Deployed `release/demo-1018` (superset of `feat/judges-demo`) with
> `SRC=…/scratchpad/picky-release ./deploy_vhagar.sh` to `/data/advaiv2/picky/repo-judges`; gateway `picky-gw` with
> `PICKY_REAL_ARMS=robot,evidence,cameras,verifier,routing` (routing = OR-Tools, `uv sync --extra routing`), Isaac
> worker `picky-sim` (store_ave, chase cam for both rovers: `PICKY_SIM_DIRECTOR=all`, 960x540). Mac: the fallback gateway is stopped; the tunnel is a dedicated ssh
> (`ssh -o ControlMaster=no -o ControlPath=none -f -N -L 8100:localhost:8100 vhagar`; kill it with
> `kill $(lsof -tiTCP:8100 -sTCP:LISTEN)`). `/watch` has a view switcher over the big panel: Chase cam | Bird's-eye 1
> (ceiling-1, produce & entrance) | Bird's-eye 2 (ceiling-2, aisles). The stale picky-pvla Isaac worker was stopped.
>
> **Old state at handoff (Sun 05:10).** vhagar was unreachable from 03:39 to at least 05:10. Tailscale lists it as online,
> but it never completes a handshake, `tailscale ping` times out and the campus name does not resolve off campus. So
> the vhagar stack is **NOT deployed yet** and the Isaac rehearsal has not run. What is running on the Mac right now:
> the web app on **:3001** (production build for `http://localhost:8100`) and the **Mac fallback gateway on :8100**
> (`…/scratchpad/judges-demo/fallback/run_local_gw.sh`: 2D sim, real Perceptron boxes and photos, Gemini). This
> fallback passed the full automated click-through.
> **When vhagar is back:** (1) `lsof -tiTCP:8100 -sTCP:LISTEN | xargs kill` (stops the Mac fallback gateway),
> (2) `…/scratchpad/judges-demo/deploy/deploy_vhagar.sh` (section 0), (3) open the tunnel and follow section 1,
> (4) rehearse once with the harness (section 5) or by hand.

---

## 0. Deploy (once; skip if `ssh vhagar 'tmux ls' < /dev/null` already shows picky-gw + picky-sim from repo-judges)

```bash
cd …/scratchpad/judges-demo/deploy && ./deploy_vhagar.sh     # about 3-6 min
```

It rsyncs `…/scratchpad/picky-judges` (branch `feat/judges-demo`) to vhagar `/data/advaiv2/picky/repo-judges` and
installs `run_gw_judges.sh`, `run_sim_judges.sh` and `sim-judges.env`. The env is derived from `sim.env`: it keeps
`PICKY_ASSET_ROOT` and `PICKY_ROVER_USD`, and adds `PICKY_SIM_DIRECTOR=rover-ave-1` and `PICKY_SIM_STORES=store_ave`.
Then it runs `uv sync --extra perception --extra agent` and restarts tmux `picky-gw` (default socket) from repo-judges.
Once `nvidia-smi` shows at least 7 GB free (it waits up to 30 min), it (re)starts `picky-sim`. Last, it prints the
VLA tiers (`basket_unload`) and the camera list. `--no-sim` deploys the gateway only. It never touches the
`picky-pvla` / `picky-so101` / `picky-ft` sockets or `picky-vla`.

## 1. Bring it up (T-30 min)

```bash
# 1a. vhagar stack alive? (always < /dev/null; never kill-server)
ssh vhagar 'tmux ls; nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader' < /dev/null
#   expect picky-gw, picky-sim (and picky-vla: leave it)

# 1b. tunnel: the port must be free first (if 8100 is taken on the Mac, see "Port 8100 taken" below)
lsof -nP -iTCP:8100 -sTCP:LISTEN || ssh -f -N -L 8100:localhost:8100 vhagar

# 1c. gateway + Isaac + cameras
curl -s localhost:8100/health | python3 -m json.tool | head -8          # status ok, contracts 0.2.4, sim_connected true
curl -s localhost:8100/api/sim/status | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("isaac_robot_ids"), d.get("isaac_store_ids"))'
#   expect ['rover-ave-1', 'rover-ave-2'] ['store_ave']
curl -s localhost:8100/api/fleet/robots/rover-ave-1/vla | grep -o '"basket_unload": *true'   # the arm unload is on
curl -s 'localhost:8100/api/cameras?robot_id=rover-ave-1' | grep -o '"camera_id":"[^"]*"'   # .director + .front.perceived

# 1d. web app on :3001 (already built for http://localhost:8100; leave a teammate's :3000 alone)
lsof -nP -iTCP:3001 -sTCP:LISTEN || (cd …/scratchpad/picky-fe-judges/arms/frontend && npx -y pnpm@10.18.3 start -p 3001)
#   rebuild only if the gateway URL changes:
#   NEXT_PUBLIC_GATEWAY_URL=http://localhost:8100 npx -y pnpm@10.18.3 build && npx -y pnpm@10.18.3 start -p 3001

# 1e. warm the AI ranking of the cards (Gemini takes 5-8 s cold; cached 1 h per candidate set)
curl -s -o /dev/null localhost:8100/api/users/maya/suggestions; curl -s localhost:8100/api/users/maya/suggestions | grep -o '"ranked_by":"[a-z]*"' | sort | uniq -c
#   "llm" = AI-ranked (cards show an "AI" badge); "rules" = Gemini quota/timeout: fine, try again in a minute
```

Open **http://localhost:3001** in Chrome, full screen (projector: 1920x1080 works; `/watch` is laid out for it).

## 2. The click-through (about 5 minutes) and what to say

1. **Home, "Running low" card.** *"Maya never typed a list. Picky reads her purchase history: you bought salsa 12
   days ago and you usually buy it every 10. Gemini re-ranks the cards and rewrites the reasons, but only from these
   facts; any number that isn't in the data is thrown out. It only suggests what's in stock at this store."*
   Point at the badges: "AI" (LLM-ranked), "Bought together", "Substitute" (when a staple is out of stock), "Deal".
2. **Tap 3-4 cards** (salsa, tortilla chips, **lime** (produce, which gives the photo review), oat milk), then
   **"Order these (N) — $X"**. This opens `/watch`.
3. **/watch (projector page).** Left: the **Isaac Sim chase camera**, streamed live from the RTX 5090 on vhagar (badge
   "Live · Isaac Sim"). Right: **what the robot sees**: its on-board camera with **Perceptron Mk1.5** boxes on the
   products, plus the wrist camera inset. Below: the plan, "What Pico is thinking", the route map and the basket.
   *"One fleet manager turned the order into a goal, picked the robot in that store, and the robot's planner wrote
   this step-by-step plan. Every step is checked by vision before it counts."*
4. **Produce review** ("Which lime should it take?"): one photo per candidate with a freshness label. Take #1.
   *"For produce you choose the exact unit, like you would in the store."* (No answer in 30 s: robot's best guess,
   labelled as such.)
5. **Photo check after every pick** ("Is this the right Mild Chunky Salsa?", wrist photo): press **Looks right**.
   Press **Wrong item** once to show the safety net. On Isaac the robot keeps the unit and flags it for staff to swap
   (put-back exists only on the 2D sim). No answer in 20 s: auto-accepted, and it says so.
6. **Checkout: the arm unloads the basket.** *"Before anyone pays, the arm takes every item out of the basket and
   puts it on the counter, one at a time, and the camera confirms each one is on the counter."* The card shows
   **Basket | Gripper | Counter** and "Unloading 3/7: Mild Chunky Salsa → counter ✓". The chase camera shows the
   SO-101 reaching into the basket. Payment starts only after the basket is empty.
7. **Visa payment agent** (labelled *Sandbox / simulated — no real money*): cart mandate, then intent check (the
   spending rules are code, not the LLM), then passkey, then a Visa agentic token (no card number ever), then
   authorize + capture, then signed receipt, then ledger. Click **"Approve $X with passkey"**. *"The LLM proposes,
   code decides: nothing is charged without the mandate engine and your explicit approval."*
8. **Receipt "Paid $X"**, then **"See it in Spending"**. The order is in the purchase history with the robot's
   photos of the exact units, and Home's cards change.

Answer any other card that pops up: detours ("Grab the Habanero salsa?") say **No thanks** to keep it short. "Want
any of these before checkout?" also says **No thanks**.

## 3. Fallbacks (nothing here needs a code change)

| Symptom | What happens / what to do |
|---|---|
| **Perceptron quota / errors** | Automatic: the boxes switch to labelled ground truth ("On-board camera · ground-truth boxes"), photo checks still work, T1 grasps fall back to the scripted grasp. To force it off: put `PICKY_PERCEIVED_PROVIDER=ground_truth` and `PICKY_VLA_T1=scripted` in vhagar `/data/advaiv2/picky/judges.env`, then restart the gateway (below). |
| **Gemini 429 / slow** | Automatic: cards in rule order with template reasons (no "AI" badge), payment narration from templates. |
| **Isaac hiccup** (worker crash, link drop, frozen chase cam) | Robots continue on the 2D sim from their last state; badges switch to "2D sim". Restart the worker: `ssh vhagar 'tmux kill-session -t picky-sim; tmux new -d -s picky-sim /data/advaiv2/picky/run_sim_judges.sh' < /dev/null` (link up after about 45-100 s; check `/api/sim/status`). Start a **new order** after it is back. Before starting it, check `nvidia-smi` shows at least 7 GB free (other jobs share the GPU). |
| **Gateway down** | `ssh vhagar 'tmux kill-session -t picky-gw; tmux new -d -s picky-gw /data/advaiv2/picky/run_gw_judges.sh' < /dev/null` (up in about 5 s; the Isaac worker reconnects by itself). |
| **Tunnel dead** (page says Reconnecting / Demo history) | `pkill -f "8100:localhost:8100"; ssh -f -N -L 8100:localhost:8100 vhagar`. |
| **vhagar unreachable** | Mac-only fallback on the same URL: `pkill -f "8100:localhost:8100"; …/scratchpad/judges-demo/fallback/run_local_gw.sh` (2D sim + real Perceptron boxes + Gemini; same app, same clicks; the chase cam is the top-down 2D view). |
| **Gateway unreachable entirely** | The app runs its in-browser demo replay of the same flow (labelled "Demo replay"). |
| **Stuck trip** | "Stop robot", then "New trip" on `/watch`; or answer the pending card. |
| **Port 8100 taken on the Mac** | Use 8110: `ssh -f -N -L 8110:localhost:8100 vhagar`, rebuild the app with `NEXT_PUBLIC_GATEWAY_URL=http://localhost:8110`. |
| **Everything is on fire** | Play the backup recording (below) and narrate over it. |

Rollback to the previous live gateway (pre-judges checkout in `/data/advaiv2/picky/repo`):
`ssh vhagar 'tmux kill-session -t picky-gw; tmux new -d -s picky-gw /data/advaiv2/picky/run_gw.sh' < /dev/null`.

## 4. Suraj's physical SO-101 "in tandem"

The robot in the sim is a LeKiwi base with a lift and the **same SO-101 arm**. Suraj's bench rig is that arm for real.

**Recommended: side by side, separate screen (zero risk to the live demo).** On Suraj's laptop (branch
`feat/robot-real-bridge`), in Terminal.app so it can use the camera:

```bash
python -m arms.robot.so101.chat            # asks "execute? [y/N]" before every motion; /dry, /home, /quit
you> pick up the oat milk and put it in the basket
```

Run it while the `/watch` page shows Pico picking in Isaac. *"Same arm, same perception: Perceptron finds the
product in the camera image, we turn the box into a grasp, and the arm picks it. The sim is where we scale this to a
fleet."* Perceptron grounding needs `PERCEPTRON_API_KEY` in `~/.config/picky/env` on that laptop (or `PICKY_VLM_URL`
for the self-hosted model).

**Integrated (only after the Isaac part, or not at all):** the bridge can stand in for Hazel at Capitol Hill
(`python -m arms.robot.so101.bridge --stand-in rover-hill-1 --default-prop gum --confirm`, with
`ssh -N -L 8100:localhost:8100 vhagar` and `PICKY_SIM_TOKEN=dev-sim-token`). The Isaac worker here hosts only
store_ave, so store_hill is free for it. **Caveat:** the gateway keeps one worker's hello at a time. When the bridge
connects after Isaac, Isaac's `basket_unload` / T1 capabilities (and its store routing) can look unavailable until
Isaac reconnects. Don't connect it during the Isaac order. Afterwards, restart `picky-sim` to restore Isaac.

## 5. Reference

- Endpoints the page uses: `GET /api/users/maya/suggestions`, `POST /api/requests {"user_id","items":[{"sku","qty"}],"store_id"}`,
  `WS /ws?trip_id=…` (events `checkout.unload`, `item.verification`, `payment.agent_step`, `approval.requested`, …),
  `POST /api/approvals/{id}`, `GET /api/cameras/rover-ave-1.director/stream.mjpg`,
  `GET /api/cameras/rover-ave-1.front.perceived/stream.mjpg`, `GET /api/trips/{id}/payment`,
  `GET /api/users/maya/purchases`.
- Gateway config (vhagar `/data/advaiv2/picky/run_gw_judges.sh`): `PICKY_REAL_ARMS=robot,evidence,cameras,verifier`,
  `PICKY_EXEC_MODE=plan`, `PICKY_VLA_T1=perceptron`, `PICKY_MODEL_FAST=gemini-3.5-flash`,
  `PICKY_PURCHASES_DB=/data/advaiv2/picky/judges-var/purchases.sqlite`; overrides in `/data/advaiv2/picky/judges.env`.
- Logs: vhagar `/data/advaiv2/picky/logs/judges-gw.log`, `/data/advaiv2/picky/logs/judges-sim.log`.
- Re-run the automated rehearsal (Playwright; records video + screenshots):
  `cd …/scratchpad/e2e/harness && APP_URL=http://localhost:3001 GATEWAY_URL=http://localhost:8100 PICKS=4 ./run.sh`
  (writes `…/scratchpad/e2e/runs/<timestamp>/report.md`, `run.webm`; it answers one photo check "Wrong item").

## 6. Backup recordings

| Recording | What it shows |
|---|---|
| `…/scratchpad/judges-demo/rehearsal-mac-fallback-2d/run.webm` (+ PNGs) | The whole judges' click-through on the Mac fallback, recorded by Playwright: cards, then order, then `/watch` (2D sim + live Perceptron Mk1.5 boxes), produce review, photo checks (one "Wrong item", re-picked), unload 1/7 to 7/7 onto the counter, payment agent, receipt, purchase DB |
| `…/scratchpad/pvla/video/run3_taco_full.mp4`, `run3_clip_t1_success.mp4` | Isaac director view of an earlier full taco run with Perceptron Mk1.5 T1 grasps (pre-0.2.4: no per-item counter unload) |
| `…/scratchpad/interim-video/picky_isaac_taco_trip_interim.mp4` | Earlier Isaac taco trip (interim) |
| `…/scratchpad/judges-demo/fe/demo-*.png`, `gw-*.png` | Web app screenshots of the judges' flow |

## Full LLM -> VLA -> verifier loop (09:28-09:47 PT, Oct 18)

- vhagar `/data/advaiv2/picky/judges.env` (sourced by `run_gw_judges.sh`; no keys in it):
  `PICKY_REAL_ARMS=robot,evidence,cameras,verifier,routing,agent` (agent = agent + brain + planner + runner real),
  `PICKY_EXEC_MODE=plan`, `PICKY_VLA_T1=perceptron`, `PICKY_MODEL_FAST=gemini-3.5-flash`,
  `PICKY_MODEL_BRAIN=gemini-3.5-flash`, `PICKY_PLAN_TIMEOUT_S=30`.
- gemini-3.5-flash answers 429 on every call tonight; utils.llm retries on gemini-3.7-flash. Since f52bd8d the plan
  card names the model that really answered (`plan.created.plan.planner.model`).
- Only ONE Isaac process fits on the GPU: start picky-sim only when `/data/advaiv2/picky-so101/GPU_FREE` exists and
  `GPU_BUSY` does not.
- Rollback to the scripted planner: remove `,agent` from PICKY_REAL_ARMS in judges.env (or delete the file), then
  `tmux kill-session -t picky-gw; tmux new -d -s picky-gw /data/advaiv2/picky/run_gw_judges.sh`.
- Rollback of f52bd8d on vhagar: `cp /data/advaiv2/picky/judges-var/bak-0940/llm.py utils/` and `planner.py arms/agent/`.
- Evidence: `llm-loop/events-2d.jsonl`, `llm-loop/events-isaac.jsonl`, `llm-loop/events-isaac-2.jsonl`,
  `llm-loop/isaac-watch-*.png`; summarize with `e2e/tools/summ.py <events.jsonl>`.
