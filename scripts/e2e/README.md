# Picky judges'-flow E2E harness

Playwright + httpx. It drives the web app the way a judge would, records every gateway call the browser makes, checks
that the camera streams really update, and checks the real SO-101 when asked to. It lives outside the repos and needs
only `uv` (Python >= 3.11).

```bash
./run.sh install                                                      # once: Chromium for playwright 1.64.0
APP_URL=http://localhost:3001 GATEWAY_URL=http://localhost:8100 ./run.sh             # live stack (vhagar tunnel)
APP_URL=http://localhost:3001 GATEWAY_URL=http://localhost:8100 EXPECT_REAL_ROBOT=1 ./run.sh
```

For the live stack, open the tunnel first with `ssh -N -L 8100:localhost:8100 vhagar`, and build or run the app with
`NEXT_PUBLIC_GATEWAY_URL=http://localhost:8100`. The harness warns when the app calls a different origin.

Exit code: 0 = no check failed, 1 = at least one failed, 2 = the app or the gateway is unreachable.
Each run writes to `../runs/<timestamp>/` (`../runs/latest` points at the newest run):
`report.md`, `report.json`, numbered screenshots, `run.webm` (video), and `network.har` (no bodies).

## What it checks

| Group | Checks |
|---|---|
| UI | Home shows suggestion cards from the gateway (not the "Demo history" fallback) and their photos load. It taps `PICKS` cards (in-stock first) and clicks "Order these", then lands on `/watch` (or `/live`, `/trip`). It answers every card: produce pick = first, detours = `DETOUR_ANSWER`, extras = "none" when there is one, checkout = "Approve and pay". Then it waits for the receipt ("Paid $…"). |
| API (browser traffic) | `GET /health`; `GET /api/users/{USER_ID}/suggestions` (200, ≥1 card); `GET /api/products/<sku>/image.jpg` (200 image/jpeg); `POST /api/requests` whose body has `items` (2xx, returns a trip id); `WS /ws?trip_id=` with counts of `route.planned`, `robot.state`, `item.picked`, `cart.updated`, `approval.requested`, `checkout.completed`; `POST /api/approvals/{id}` (200); `<robot>.director` and `<robot>.front.perceived` `stream.mjpg` (200 multipart/x-mixed-replace); no 5xx. Chromium hides the status of a blocked cross-origin `<img>` (`ERR_BLOCKED_BY_ORB`), so the harness fetches those URLs itself to show what the gateway really returned. |
| Streams | Every camera `<img>` on the live page must have `naturalWidth > 0` and change between 2 samples taken 2.2 s apart. It uses a canvas, or an element screenshot with overlays masked when the canvas is cross-origin. Panel badges must not say "Isaac Sim" when `GET /api/cameras` says the camera is rendered by the fake. It reads the MJPEG endpoint directly (≥ `MJPEG_MIN_PARTS` JPEG parts in `MJPEG_SECONDS`, fps, resolution, first-frame latency) and `snapshot.jpg` twice. `WS /ws/cameras/{id}` frame ids catch the gateway's 200 "offline" placeholder card (`synthetic-offline-*`). |
| Real robot | `/api/sim/status` lists a `/ws/sim` worker hosting `REAL_ROBOT_ID` (the SO-101 bridge); `GET /api/fleet/robots` lists it as connected; `GET /api/cameras` lists its camera, whose MJPEG, snapshot and WS frames must be real; the app shows it on `REAL_ROBOT_PAGES`. If no page shows it, the result is a `gap`, not a failure. |

Director and perceived cameras are required only when Isaac drives a fleet robot (`/api/sim/status.isaac_robot_ids`).
On the 2D fake they are reported `not_available` (set `EXPECT_ISAAC=1` to force them).

## Env

| Var | Default | |
|---|---|---|
| `APP_URL` | `http://localhost:3001` | web app |
| `GATEWAY_URL` | `http://localhost:8100` | gateway (vhagar tunnel) |
| `ROBOT_ID` | `rover-ave-1` | robot whose streams are read directly (the robot the fleet assigns is read too) |
| `STORE_ID` | `store_ave` | its ceiling camera is read too |
| `USER_ID` | `maya` | suggestions user |
| `EXPECT_REAL_ROBOT` | `0` | `1` = real-robot checks fail instead of being skipped |
| `REAL_ROBOT_ID` | `so101-real` | |
| `REAL_ROBOT_PAGES` | `/,/live,/watch` | pages searched for the real robot |
| `EXPECT_ISAAC` | auto | `1` / `0` overrides the director / perceived requirement |
| `PICKS` | `3` | cards to tap |
| `TRIP_TIMEOUT_S` | 300 (fake), 900 (Isaac) | wait for the receipt |
| `DETOUR_ANSWER` | `no` | `yes` adds the detour items |
| `MJPEG_SECONDS` / `MJPEG_MIN_PARTS` | `5` / `3` | |
| `HEADED`, `SLOW_MO_MS`, `VIEWPORT` | `0`, `0`, `1600x1000` | watch it run |
| `HAR` | `1` | write `network.har` |
| `SKIP_UI` | `0` | `1` = only the direct stream checks and the real-robot checks |
| `RUNS_DIR` | `../runs` | |

## Local validation stack (no vhagar)

```bash
# gateway copy on :8040 (all fakes, 2D sim), app copy on :3040
GATEWAY_PORT=8040 uv run python -m arms.gateway.app            # in a copy of the backend repo
NEXT_PUBLIC_GATEWAY_URL=http://localhost:8040 npx -y pnpm@10.18.3 dev --port 3040   # in a copy of arms/frontend
# optional: the SO-101 bridge with no hardware, dialing that gateway's /ws/sim
uv run python -m arms.robot.so101.bridge --dry --camera fake --gateway http://localhost:8040
APP_URL=http://localhost:3040 GATEWAY_URL=http://localhost:8040 EXPECT_REAL_ROBOT=1 ./run.sh
```
