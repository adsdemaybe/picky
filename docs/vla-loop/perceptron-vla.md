# Perceptron robotics VLA: client, pipeline, policy backends

Perceptron's robotics service ("Isaac SO100 participant API", `https://robotics.perceptron.inc`; spec: its
`/openapi.json` and the `/docs` guide) trains an SO100/SO101 VLA on a LeRobot dataset, deploys it on a GPU, and serves
action chunks to a robot session. This page covers how Picky drives that service and how a trained deployment plugs
into the VLA tier as a `PolicyBackend`.

| File | What it holds |
|---|---|
| `arms/robot/perceptron_vla.py` | typed async client (`RoboticsClient`, `InferenceSession`), dataset pre-flight, `run_pipeline`, the CLI |
| `arms/robot/policy_backends.py` | `PolicyBackend` protocol + `PerceptronBackend`, `SmolVLABackend`, `ScriptedBackend`, `SyncPolicyBackend`, `policy_capability` |
| `arms/robot/tests/test_perceptron_vla.py` | 36 tests against an in-process fake of the service (httpx `MockTransport`, no network, no key) |

## Keys: two different Perceptron keys

| Name | What it is for | Status |
|---|---|---|
| `PERCEPTRON_API_KEY` | hosted Mk1.5 vision model (the verifier's grounding / OCR) | we have it; **robotics.perceptron.inc rejects it (401)** |
| `PERCEPTRON_ROBOTICS_API_KEY` | team key for datasets, jobs, checkpoints, deployments, session grants | **issued by Perceptron staff; we don't have it yet** |

The client looks up `PERCEPTRON_ROBOTICS_API_KEY` in this order: the Settings field (once the key-vault PR lands),
then the process env, then the key vault (`utils.secrets`, or the stores it uses directly: macOS Keychain service
`picky`, then `~/.config/picky/secrets.env`), then the repo `.env`. Store the key once with
`uv run python -m utils.secrets set PERCEPTRON_ROBOTICS_API_KEY`. Key values are never printed. `doctor` shows only
`set (…last4)` and where the key came from.

Optional env: `PERCEPTRON_ROBOTICS_BASE_URL` (default `https://robotics.perceptron.inc`),
`PERCEPTRON_ROBOTICS_DEPLOYMENT_ID` (the deployment the T1 backend uses; it defaults to the last successful `pipeline`
run, which is recorded in `~/.cache/picky/perceptron-vla.json`; that file holds ids only, never keys or tokens). These
are not Settings fields yet, so add them in a `utils/` PR before relying on `.env` alone. Until then they resolve from
env / `.env` through `perceptron_vla.resolve_setting`.

## Runbook: the real Perceptron VLA test (what the lead does once)

Status on 2026-10-18 at 03:40 PT:
- `GET /healthz` answers 200.
- No team key exists in the repo `.env`, the vault, the macOS Keychain or vhagar's `~/.config/picky/secrets.env`.
- The key pasted in chat is the same one as `PERCEPTRON_API_KEY`, the Mk1.5 key. `/v1/capabilities` rejected it with
  401 at 01:20 PT.
- So no dataset, job or deployment exists on the service yet.
- The dataset is ready and validated on vhagar (`dataset.md`).
- The loop already runs in Isaac with the Mk1.5 tool policy as T1 (`arms/sim/DEPLOY.md` §8–9). It stands in for the
  trained VLA, so step 4 below only switches T1 to the trained deployment.

1. **Get the team key.** Ask Perceptron staff (the DubHacks sponsor team) for the **robotics team API key** for
   `https://robotics.perceptron.inc` (the "Isaac SO100 participant API"). The `platform.perceptron.inc` Mk1.5 key
   won't work there: it gets 401.
2. **Store it** (once per machine; do it on vhagar, where the dataset lives). Never paste it into chat or a file in
   the repo.
   - Once the key-vault PR (`utils/keyvault`, #18) is merged, use
     `make secret NAME=PERCEPTRON_ROBOTICS_API_KEY`. It prompts with hidden input and writes
     `~/.config/picky/secrets.env` with mode 600.
   - On this branch, before #18 lands, the client reads that file directly. Write the key there with hidden input:
     ```bash
     install -d -m 700 ~/.config/picky && (umask 077; read -rs -p 'team key: ' K && echo && \
       printf 'PERCEPTRON_ROBOTICS_API_KEY=%s\n' "$K" >> ~/.config/picky/secrets.env; unset K)
     ```
   - On the Mac, the Keychain also works (it prompts): `security add-generic-password -s picky -a PERCEPTRON_ROBOTICS_API_KEY -w`.

   Check with `uv run --frozen python -m arms.robot.perceptron_vla doctor`. You want the key shown as `set (…last4)`
   with its source, `/healthz` 200, and `/v1/capabilities` 200. A 401 means the key is the Mk1.5 key or has been
   revoked.
3. **The one command.** Run it on vhagar in the test copy `/data/$USER/picky/repo-pvla`, never in the live demo repo.
   It uploads, validates, trains, deploys, reads the contract, opens a session and runs one predict:
   ```bash
   cd /data/$USER/picky/repo-pvla
   uv run --frozen python -m arms.robot.perceptron_vla pipeline /data/$USER/picky-vla/datasets/picky_shelf_pick_v1 \
     --steps 1000 --instruction "pick up the mild chunky salsa 16 oz and put it in the basket"
   ```
   - Exit codes: 0 ok, 1 the local dataset check failed, 2 config (the key), 3 API error, 4 timeout.
   - Re-running the same command resumes. `--fresh` submits again on purpose.
   - Optional smoke test first, with no upload: `... pipeline lerobot/svla_so100_stacking --steps 1000` (a public
     LeRobot SO100 dataset).
   - The deployment id lands in `~/.cache/picky/perceptron-vla.json` (ids only). From there the T1 backend picks it
     up; `PERCEPTRON_ROBOTICS_DEPLOYMENT_ID` overrides it.
4. **In-sim test: the trained deployment as the T1 grasp policy.** This uses the separate test stack on vhagar
   (gateway :8101, worker `isaac-pvla`, its own tmux socket `-L picky-pvla`; `arms/sim/DEPLOY.md` §8–9). The live
   demo on :8100 is never touched.
   ```bash
   # in /data/$USER/picky/run_pvla_gw.sh set PICKY_VLA_T1=perceptron_vla (was: perceptron = the Mk1.5 stand-in), then
   tmux -L picky-pvla kill-session -t picky-pvla-gw
   tmux -L picky-pvla new -d -s picky-pvla-gw /data/$USER/picky/run_pvla_gw.sh
   curl -s localhost:8101/api/fleet/robots/rover-ave-1/vla | python3 -m json.tool   # t1_pretrained available, model = the deployment
   # one grasp:
   curl -s -X POST localhost:8101/api/dev/plans -H 'content-type: application/json' \
     -d '{"robot_id":"rover-ave-1","store_id":"store_ave","text":"pick up a jar of salsa"}'
   # the whole trip, recorded (events + approvals answered like the web app):
   uv run --frozen --no-sync python arms/sim/tools/director/record.py --base http://localhost:8101 --store store_ave \
     --text "tacos for 6, under \$40, one vegetarian" --out /data/$USER/picky/pvla-runs/vla1
   grep 'T1 rover-ave-1 ' /data/$USER/picky/logs/pvla-gw.log    # per grasp: ok | code, chunks, pairs/s, infer ms
   ```
   - Render the video with `arms/sim/tools/director/render.py` (§9).
   - SmolVLA comparison: set `PICKY_VLA_T1=smolvla` (vhagar :8200) and run the same request.
   - A failed or slow T1 grasp falls back to the T0 scripted grasp, so the trip still completes. Count the
     `T1 ... ok` lines to score the policy.

## The one command

```bash
uv run python -m arms.robot.perceptron_vla doctor                          # key present? service up? key accepted?
uv run python -m arms.robot.perceptron_vla pipeline /path/to/lerobot_export --steps 1000 \
    --instruction "pick up the item from the shelf"
# or a public HF dataset:
uv run python -m arms.robot.perceptron_vla pipeline lerobot/svla_so100_stacking --steps 1000
```

`pipeline` runs these steps, printing progress (never secrets):

1. Local checklist pre-flight (`dataset-check`, skip it with `--skip-check`).
2. Upload: register (`source_kind=upload`) → reserve signed URLs in batches of ≤ 500 (size + lowercase SHA-256) →
   `PUT` the raw bytes with exactly the returned method and headers, **without the team key** → finalize. An HF repo
   id is registered instead and needs no upload.
3. Poll `GET /v1/datasets/{id}` until ready, then read `/validation`. A failed validation stops the run and prints the
   issues.
4. `GET /v1/capabilities` (this only warns if our settings aren't listed), then `POST /v1/jobs`, then follow status +
   logs + metrics with their cursors.
5. Checkpoint → wait for a **ready** deployment (matched by the job's deployment or checkpoint id; Modal production is
   preferred over private-cluster qualification).
6. `GET /v1/deployments/{id}/contract`, printing its summary (cameras, state order, units, timing).
7. `PerceptronBackend`: grant (team key) → create a robot session (session tokens) → one `predict` on synthetic
   side + wrist frames at the rest pose → delete the session.

Re-running the same command **resumes** instead of duplicating, because the idempotency keys are derived from the
request body. An upload dataset's key is its name + manifest digest. The job's key is its full body. An
already-finalized dataset answers 409 on reserve, and the pipeline then goes straight to polling. Pass `--fresh` to
make a deliberate second submission. Exit codes: 0 ok, 1 local check failed, 2 configuration (no key / bad input),
3 API error, 4 timeout (re-run to resume).

Other subcommands: `capabilities`, `dataset-check DIR` (no key needed), `dataset-hf REPO [--wait]`,
`dataset-upload DIR [--wait]`, `dataset-status ID`, `train DATASET_ID [--steps N --backend standard|shared
--mode expert_only|backbone_lora|full --horizon 20|30 --no-rtc --wait]`, `jobs`, `job ID`, `cancel ID`,
`logs ID [--cursor N --follow]`, `metrics ID [--cursor N]`, `checkpoint ID`, `deployments`, `contract ID`,
`session [DEPLOYMENT_ID]` (grant + create + health + delete; tokens never shown),
`predict-test [DEPLOYMENT_ID] [--side F --wrist F --frames-blurred] [--state 6 deg] [--n 2]` (the second predict and
later ones exercise RTC). Add `--json` for raw records.

## Dataset checklist (what `dataset-check` verifies locally)

- LeRobot `codebase_version` v2.0 / v2.1 / v3.0; `fps` 30.
- `observation.images.side` + `observation.images.wrist`: 480×640×3 RGB, as videos.
- `observation.state` + `action`: shape [6] in the order `shoulder_pan.pos, shoulder_lift.pos, elbow_flex.pos,
  wrist_flex.pos, wrist_roll.pos, gripper.pos`.
- `total_episodes` ≥ 2 (at least one train and one validation episode), data parquet + both camera videos present.
- Units can't be checked without reading the parquet. Arm values must be **degrees** and the gripper **0–100**. Our
  rover is LeIsaac's `lekiwi.usd`, so arm degrees are the USD joint angles as they are. The gripper maps USD
  −10..100° linearly onto 0..100, the same as LeIsaac's `convert_leisaac_action_to_lerobot` and the recorder in
  `arms/sim/tools/record_lerobot`.
- Uploads skip hidden paths and unsupported extensions (allowed: `.json .jsonl .parquet .mp4 .png .jpg .jpeg`), and
  reject files that are empty or larger than 4 GiB.

## Protocol rules the client enforces

- **The team key goes to the control plane only.** A `_KeyGuard` request hook (it compares SHA-256 fingerprints)
  refuses any upload PUT, 303 polling GET or inference request that would carry it. Inference sends
  `Authorization: Bearer <application_session_token>` + `Modal-Authorization: Bearer <modal_session_token>`. Tokens
  are `SecretStr` and are never printed or persisted.
- `Idempotency-Key` is sent on `POST /v1/datasets` and `POST /v1/jobs`. Reusing a key with a different body gives 409
  `idempotency_conflict`, which is surfaced as is.
- Errors: `{"error": {"code", "message", "fields"}}` → `RoboticsAPIError(status, code, message, fields)`. A 401 on the
  control plane adds the "this is not the Mk1.5 key" hint. 429 waits for `Retry-After` (capped at 60 s) and retries.
  5xx / network errors are retried (exponential backoff, 3 tries) only for GETs, idempotency-keyed POSTs, reserve and
  cancel. The session grant and finalize are never blindly retried.
- 303: polled at the exact `Location` (path + query kept), same origin only, GET, no credentials, the original body
  never replayed. A cross-origin `Location` is refused (`unsafe_redirect`).
- Authenticated JSON bodies are capped locally at 512 KiB. Inference observations are kept under it by re-encoding
  JPEGs (≤ 200 KB per image).
- Inference base: the guide calls the routes relative to the grant's `endpoint_url` (`$ENDPOINT_URL/sessions/...`),
  while the reference lists `/v1/inference/...`. A bare origin gets `/v1/inference` appended (and a `/v1` path gets
  `/inference`). Any other path is used as it is (`inference_base`).
- Response bodies are untyped in the OpenAPI document, so records keep every field and read them tolerantly:
  `status | state | phase`, `checkpoint_id | checkpoint.id`, action rows under `actions | action_chunk | chunk.actions`,
  and an execution prefix given as an int, `{start_index, length}` or a list of rows. `contract` / `--json` print the
  raw records so a human can confirm the field names on the first real run.

## PolicyBackend: what the exec agent plugs in

```python
from arms.robot.policy_backends import create_policy_backend, policy_capability

backend = create_policy_backend("perceptron")        # or "smolvla", "scripted"; None = PICKY_VLA_POLICY_PROTOCOL
await backend.reset("pick up the salsa jar")          # new episode (grasp attempt)
chunk = await backend.predict(state_deg, front_jpeg, wrist_jpeg, frames_blurred=True)   # first call: no RTC
for row in chunk.rows_to_execute(backend.info.default_execute):   # USD joint DEGREES, SO-101 order
    ...                                                            # at chunk.action_hz
chunk = await backend.predict(state_deg, front_jpeg, wrist_jpeg, frames_blurred=True,
                              executed=next_row_index_in_previous_chunk, delay=rows_during_inference)
await backend.close()
```

- `predict(state_deg[6], side_jpeg, wrist_jpeg, *, frames_blurred, executed=None, delay=0) -> ActionChunk`.
  - `side` is our robot **front** camera (`CAMERA_MAP`).
  - `frames_blurred=False` raises `ValueError` (CLAUDE.md §8: blur with perception's YuNet first).
- `ActionChunk` fields:
  - `actions_deg` (N×6 USD degrees, clipped to the joint limits; a value more than 60° outside its limit, or NaN →
    `policy_diverged`).
  - `execute_from` / `execute_count` (with RTC the committed rows come first, so `execute_from = delay`, unless the
    service sends its own execution prefix), `chunk_id`, `sequence`, `latency_ms`, `rtc_used`, `clipped`.
  - `policy_actions`: the same rows in policy units.
- Errors: `PolicyBackendError.code` ∈ `policy_unavailable | policy_timeout | policy_diverged | internal`. These are
  `VlaErrorCode`s, so the VLA service reports them as they are; the circuit breaker counts the `t1_strike` ones.
- `PerceptronBackend`:
  - `reset` opens a session (contract + grant with the team key, then create with the tokens). With the same
    instruction it reuses the session through `POST .../reset`. A new instruction opens a new session, because the
    instruction is bound when the session is created.
  - RTC: `executed` / `delay` become `rtc = {prior_chunk_id, prior_start_index=executed, delay,
    committed_actions = prior rows[executed : executed+delay]}`, in contract units, exactly as the service returned
    them.
  - An expired session (401 / 403 / 404 / 410 on predict) gets one fresh grant + new session, without RTC.
  - `sequence` and `timestamp_seconds` increase on every request.
  - Units come from the contract (`So101UnitMap.from_contract`): arm degrees pass through (radians are converted if
    the contract says so); gripper 0–100 ↔ USD −10..100°.
- `SmolVLABackend`: vhagar's SmolVLA server (`POST /infer`; images `front` + `wrist` as base64 JPEG; `state_deg` and
  `actions_deg` already in USD degrees). No RTC; it runs 20 of its 50 rows by default. From the Mac, reach it through
  `ssh -N -L 8200:127.0.0.1:8200 vhagar`.
- `ScriptedBackend`: deterministic chunks toward a target pose, for executor tests.
- `SyncPolicyBackend(factory)`: wraps any backend on its own event-loop thread so the Isaac worker's synchronous
  policy thread can call `reset` / `predict` / `close`.
- `policy_capability(robot_id, name, robot_backend, *, tier="t1_pretrained", reachable=None)` returns the T1
  `VlaCapabilities` row. Perceptron is available only with the team key + a deployment id + an Isaac store.
  `input_frames_blurred=True`, because predict refuses unblurred frames.
- Selecting a backend by env: `PICKY_VLA_POLICY_PROTOCOL=perceptron_vla | smolvla | scripted`
  (`backend_name_from_settings`). The existing values `openpi_ws` / `perceptron_tool` stay with `arms/robot/t1.py`.
  Wiring T1 to a `PolicyBackend` is the exec agent's change in `t1.py` / the T1 executor.

Tier note: a Perceptron deployment trained on our own sim demonstrations is, strictly speaking, `t2_finetuned`. The
capability helper defaults to `t1_pretrained` because that is what the loop wires today; pass `tier="t2_finetuned"`
once T2 is wired.

## Known limitations

- The robotics team key is missing, so no real dataset / job / deployment / predict has run yet. Every flow is tested
  only against the fake service, which follows the OpenAPI document + guide. Field names of the untyped responses
  (job → checkpoint / deployment link, contract layout, predict response) are best-effort, with tolerant fallbacks.
  Check the printed raw records on the first real run.
- **Stand-in policy.** Until a deployment exists, T1 in the Isaac loop is `PICKY_VLA_T1=perceptron`: Perceptron Mk1.5
  (the vision model) calls Cartesian tools (`vla_mk15.py`: `reach_point` → IK → joint rows, then `set_gripper`). It is
  not a trained VLA. It drives the same joint-level path (`ArmJointsCmd`, the policy cameras, chunks, the fallback to
  T0), so swapping in `perceptron_vla` changes only the policy.
- **Sticky grasp.** The Isaac worker runs no contact physics. A gripper close attaches the target unit rigidly when
  the jaw center is within `STICKY_RADIUS_M` (3 cm) of the unit's box (`arms/sim/arm_joints.py`). A real gripper
  might slip on a close that is a few cm off, but here it still counts as a grasp. Judge a policy by its T1 ok
  rate, its chunks and the verifier, not by physical realism.
- `dataset-check` can't verify units or per-frame agreement (it needs pyarrow / PyAV); the service's validation is
  authoritative.
- Inference latency includes a WAN round trip to the Modal GPU, while chunks are 1 s at 30 Hz (30 rows). Use RTC
  (`executed` / `delay`) so the arm never stalls between chunks.
