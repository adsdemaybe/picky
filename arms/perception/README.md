# perception

**Purpose.** Turns the robots' and stores' camera frames into facts: faces blurred, products identified (scan reports
that update the catalog), produce ranked by freshness (top-3 for the shopper), basket contents verified. Also owns the
**Cameras API** (`CameraService`): camera registry, blurred JPEG snapshots, live streams (MJPEG / WS) and `observe`
(the robot brain's `look` tool: camera → vision description).
Pipeline position (CLAUDE.md §2, SHOP): `robot.scan` → **`perception.blur_faces` / `identify`** → `catalog.apply_scan`
→ `scan.result` → produce: **`rank_freshness`** → `pick.choices` → `robot.pick` → `place_in_basket` →
**`verify_basket`** → `basket.verified`. Cameras: `robot.capture_camera` / `robot.frames` → **`CameraService`**
(blur → downscale) → GET `/api/cameras/{id}/snapshot.jpg` · `stream.mjpg` · `/ws/cameras` · POST `observe`.

## Scope (CLAUDE.md §3)
- [x] **P0** Face blurring on every camera frame before it is stored, streamed or sent to an LLM (YuNet; synthetic-frame rule as fallback)
- [x] **P0** Scan pass → `ScanReport` (detections, observed count, per-lot freshness, shelf-tag price, pose) that `catalog.apply_scan` consumes
- [x] **P0** Ground-truth bypass (sim reports instance → SKU) so the pipeline always runs
- [x] **P0** "Pick the exact item": rank produce by freshness, top-3 with reasons + instance_id for `robot.pick`
- [x] **P0** Basket verified at drop (multiset diff vs expected)
- [x] **P1** Barcode read (zxing-cpp) on crops → SKU (service.py, TODO 2 done; needs extra)
- [ ] **P1** SigLIP 2 embedding match vs our label textures (TODO 3)
- [ ] **P1** Claude vision (`claude-haiku-5-5`) pick-from-candidates on blurred crops (TODO 4)
- [x] **P1** Face-free freshness thumbnails cropped from the blurred frame (TODO 5 done; needs pillow)
- [ ] **P2** Freshness from pixels (multi-image Claude rank blended 0.6/0.4 with HSV heuristic) (TODO 6)
- [ ] **P2** Basket verify from pixels (cascade per basket crop) (TODO 7)
- [ ] **P2** Shelf-tag OCR for mispricing (TODO 8)
- [x] **P0** `ScanReport.lot_counts` (units per lot when the count is exact; v0.2) (fake)
- [x] **P0** Cameras API registry: robots.yaml cameras (`<robot_id>.front|wrist|basket`, `<store_id>.ceiling-<n>`) +
      cameras announced by the Isaac worker (`sim_link.status()`), live fps / last frame time (fake)
- [x] **P0** `snapshot(camera_id, max_width)`: fresh BLURRED frame from `robot.capture_camera`, Pillow downscale (fake)
- [x] **P0** `stream(camera_id, fps, max_width)`: `robot.frames(camera_id=...)`, latest-frame-wins at ≤ fps, 1 s stall →
      polls `capture_camera`; closing the stream closes the robot iterator (fake)
- [x] **P0** Every frame leaving the cameras API has `blurred=True` honestly: `blur_faces`, else "rendered by Isaac /
      the 2D fake" certification, else the whole frame is pixelated; undecodable → placeholder (fake)
- [x] **P0** Offline cameras (no robot service, robot error, sim gone) → rendered "offline" card, never a 500 (fake)
- [x] **P0** `observe` fake: ground-truth description (instances → items, counts, shelf tag, mispricing note, freshness;
      empty aimed slot; planogram near the robot when the frame has no instances) + keyword question answering (fake)
- [ ] **P1** `observe` real: `claude-haiku-5-5` vision on the blurred JPEG via `utils.llm`, ground truth as hints and
      fallback (built + unit-tested with a mocked client; not run live: no `GEMINI_API_KEY` yet)

## Contract
- Implements `contracts.services.PerceptionService` (`blur_faces`, `identify`, `rank_freshness`, `verify_basket`).
- Implements `contracts.services.CameraService` via `create_cameras` / `create_cameras_fake` (`list_cameras`,
  `get_camera` (KeyError → 404; `<robot>.head` accepted as `front`), `snapshot`, `stream`, `observe`). Out:
  `contracts.cameras.CameraInfo`, blurred `CameraFrame`, `VisionObservation`. Gateway helpers exported from
  `arms.perception`: `jpeg_bytes(frame)`, `mjpeg_part(frame)`, `MJPEG_CONTENT_TYPE` (`multipart/x-mixed-replace;
  boundary=frame`).
- In: RAW `contracts.sim.CameraFrame` (with ground-truth `instances`: sku, instance_id, lot_id, freshness, pixel bbox).
- Out: blurred `CameraFrame` (`blurred=True`), `contracts.perception.ScanReport`, `PickChoice` (≤ `PICK_CHOICE_MAX`=3
  `FreshnessCandidate`s, rank 1 = freshest), `BasketCheck` (`BasketCheck.compare`).
- Events: **none published by this arm.** The gateway wraps results in `scan.result`, `pick.choices`,
  `basket.verified`, `frame`.
- Calls (call time only): `services.catalog.get_planogram`, `services.catalog.get_product`; falls back to
  `utils.data` when catalog isn't wired. Cameras: `services.robot.capture_camera` / `frames(camera_id=...)`,
  `services.perception.blur_faces` (own blurrer if unwired), `services.sim_link.status()`; `utils.data.load_cameras`
  (built-in defaults with the fixed ids if robots.yaml is missing). Real `observe` goes through
  `utils.llm.complete_json` (`kind="fast"` = claude-haiku-5-5; image block before text).
- Cheat sheet: [contracts/OVERVIEW.md](../../contracts/OVERVIEW.md) · models: [contracts/perception.py](../../contracts/perception.py)

## Stack (docs/research.md §9.2)
| | Pick | Fallback |
|---|---|---|
| Identify | zxing-cpp → open_clip SigLIP 2 `hf-hub:timm/ViT-B-16-SigLIP2` → `claude-haiku-5-5` vision | ground-truth bypass; CLIP `ViT-B-32`/`laion2b_s34b_b79k` |
| Face blur | OpenCV YuNet `face_detection_yunet_2023mar.onnx` + Gaussian blur (20% pad) | synthetic-frame rule; MediaPipe BlazeFace |

Gotchas: pin **`opencv-python-headless>=4.10,<5`** (5.x untested with 2023mar). SigLIP 2 needs `open-clip-torch>=2.31`,
`timm>=1.0.15`; device `mps` on Mac. YuNet may miss low-poly sim faces. Claude vision is weak under 200 px and can't
count identical items. Barcodes need ≥ ~300 px width + quiet zone. Run perception in the gateway process, not Isaac's
Python; heavy work goes through `asyncio.to_thread`.

**Blur rule (why the fake only blurs synthetic frames).** `blurred=True` is a privacy claim the gateway relies on before
streaming. The fake has no detector, so it only certifies frames that can't contain a real face: frames carrying sim
ground truth (`instances`) or whose `frame_id` starts with `sim`/`fake`/`synthetic`/`canned`. Anything else comes back
`blurred=False` → the gateway must not stream/store it. The real service runs YuNet when available and falls back to
the same rule (fail closed).

**Cameras privacy rule.** `CameraService` never serves a frame whose `blurred` flag it can't stand behind: (1) run
`perception.blur_faces`; (2) if that can't certify it but the camera is rendered (`CameraInfo.source` `isaac` / `fake`:
every camera in this system is a sim camera), mark it blurred; (3) otherwise pixelate the WHOLE frame first; (4) an
undecodable frame becomes a placeholder card. `observe` sends only blurred frames to the LLM.

## Files
- `__init__.py` — `create` → `RealPerception`, `create_fake` → `FakePerception`, `create_cameras` → `RealCameras`
  (raises without `GEMINI_API_KEY` so the gateway uses the fake), `create_cameras_fake` → `FakeCameras`
- `cameras.py` — Cameras API (registry, snapshot / stream / observe, frame helpers, placeholder renderer)
- `fake.py` — ground-truth bypass (base deps only), helpers `is_synthetic_frame`, `freshness_reason`
- `service.py` — YuNet blur + cascade skeleton (barcode done; SigLIP/VLM TODO) over the ground-truth report
- `tests/test_perception.py` — Protocol conformance + demo behavior (avocado top-3, mispricing, empty slot, basket diff)
- `tests/test_cameras.py` — CameraService: registry (defaults, robots.yaml, sim overlay), snapshot scaling + privacy,
  placeholders, live / stalled / legacy streams, fake observe, real observe with a mocked Claude client

## How to run
```bash
uv run pytest arms/perception                       # base deps; data test skips until data/planograms/store_ave.yaml exists
make demo                                           # all fakes (perception = ground-truth bypass)
uv sync --extra perception                          # opencv, pillow, zxing-cpp (+ open-clip-torch torch timm for TODO 3)
curl -L -o models/face_detection_yunet_2023mar.onnx \
  https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
PICKY_REAL_ARMS=perception make demo                # real blur + cascade, ground truth fills the gaps
PICKY_REAL_ARMS=cameras make dev                    # real observe (needs GEMINI_API_KEY); the rest stays fake
curl -s localhost:8000/api/cameras | jq '.[].camera_id'
curl -s -o pico.jpg 'localhost:8000/api/cameras/rover-ave-1.front/snapshot.jpg?w=320'
curl -s -XPOST localhost:8000/api/cameras/rover-ave-1.front/observe -d '{"question":"is the mild salsa here?"}'
```
Fallback flag: drop `perception` (and `cameras`) from `PICKY_REAL_ARMS`. `cameras` is real when `cameras` OR
`perception` is listed; without an API key the real factory raises and the gateway boots the fake.

## Demo-ready checklist (CLAUDE.md §6)
- [ ] Every Protocol method works with `PICKY_REAL_ARMS=perception`; no `NotImplementedError` reaches a demo path (stages raise internally, cascade skips them)
- [ ] `make demo` completes a trip with perception real: scan → `scan.result`, avocados → `pick.choices` top-3, drop → `basket.verified ok`
- [ ] Every frame on `/ws/frames` has `blurred=true`; a face billboard in the sim is visibly blurred
- [ ] Undecodable JPEG, missing model/extra, unknown store, empty slot → degrade to ground truth, log, no crash
- [ ] Switchable back to the fake by env flag alone
- [ ] `uv run pytest arms/perception` green; README updated

## Known limitations
- Fake thumbnails are `None` (no image decoding with base deps); the real service crops them with pillow.
- `identify` without ground truth and without a matching `frame.slot_id` echoes the planogram qty as a non-exact count
  (so `apply_scan` won't zero stock on a blind frame).
- `CameraFrame` has no `source` field, so "synthetic" is inferred from `instances` / `frame_id` prefix (see Blur rule).
- `ScanReport.trip_id` is left `None` (not in the `identify` signature); the gateway can set it.
- Freshness is ground truth only (P2 pixel freshness not built).
- Real `observe` has only been run against a mocked client (no API key yet). VLM counts are replaced by sim ground
  truth when the SKU is in the hints (Claude can't count identical items); VLM SKUs not in the hints become `None`.
- Fake `observe` answers questions by keyword overlap with item labels/SKUs (no NLU): "any limes?" works, "something
  spicy?" does not.
- Camera `source` is inferred from `sim_link.status()` (worker cameras + `isaac_robot_ids` / `isaac_store_ids`);
  the robot arm's per-store choice can briefly differ during an Isaac reconnect.
- `stream` drops intermediate live frames (latest wins at the requested fps; max 15). The fps shown in
  `CameraInfo.fps` is an EMA over frames this service served (snapshots + streams), not the sim's render rate.

## How to contribute (this arm)
Full flow: CLAUDE.md §5. Short version: work on branch `arm/perception` (after merge: `feat/perception-<slug>` /
`fix/perception-<slug>`), touch only `arms/perception/**`, never `contracts/` (that's a `contracts/<slug>` PR). Keep
`fake.py` base-deps-only and green; heavy imports stay inside functions. Before pushing: `uv run ruff check --fix
arms/perception && uv run ruff format arms/perception && uv run pytest arms/perception`, then
`PICKY_REAL_ARMS=perception make demo`. Commit messages: `perception: <imperative summary>`. Tick the scope +
demo-ready boxes in the arm's issue/PR as items land.

**Owner:** TBD · **Branch:** `arm/perception`
