# Perceptron mapping: verifier + grounding

The verifier is provider-agnostic. `services.VisionProvider` is the slot, `fakes/perceptron_provider.py` is the
reference Perceptron implementation, and the perception arm owns the real one behind `PICKY_REAL_ARMS=verifier`. A
provider only answers questions. **Code** turns answers into pass / fail / uncertain with fixed rules
(`verify.PERCEPTRON_CALLS`, `verify.combine_verdicts`), so Perceptron never decides money, mandates or approvals.

## SDK facts (perceptron ≥ 0.4)

- Install: the `perceptron` extra. Key: `PERCEPTRON_API_KEY`. Model: `perceptron-mk1.5` (fallback `perceptron-mk1`).
  `isaac-0.1` is served only through the `fal` provider, and `isaac-0.2*` is retired on the Perceptron API.
- Use ONE `AsyncClient` per event loop (its connection pool belongs to the loop). Don't call the sync helpers inside
  `asyncio.to_thread` from the gateway loop.
- Wrap every call in `asyncio.wait_for(PICKY_VERIFY_TIMEOUT_S)`. The SDK default timeout is 125 s, Multilook waits at
  least 305 s, and the SDK does not retry.
- Coordinates are integers on a 0–1000 grid, origin top-left (`grounding.NormBox` / `NormPoint`; `to_px(w, h)`).
  Annotations without `asset_idx` refer to the last image in the request. Two-image compares therefore put the "after"
  frame last.
- Errors: `RateLimitError`, `QuotaExceededError` (not retryable), `TimeoutError`, and `SDKError(.code)`. The provider
  returns them as `VisionAnswer.call.error_code` and never raises into the runner.
- Privacy: every provider method raises `ValueError` on `frame.blurred == False`. Frames come from
  `EvidenceCameras` / `CameraService.snapshot`, which are blurred by contract. The ground-truth sidecar is never sent.

## Which call backs which check

| Capability | Camera | Perceptron call | Code decides |
|---|---|---|---|
| `ground_target`: box + grasp point of the exact unit | front (re-ground: wrist on the 2nd recovery) | `detect(image, classes=["{brand} {name} {size}"])`, then `question(image, "Point to the best grasp spot on the {name}", expects="point")` | best box (slot-position tie-break) → `GroundedTarget.box`, point → `grasp_point`; sim: `instance_id` = the unit whose bbox contains the point; no box / conf < `min_confidence` → `found=False` → recovery `grounding` (realign ▸ renavigate ▸ ask) |
| `gripper_holding` / `gripper_empty` | wrist | `client.chat.completions.multilook(model="perceptron-mk1.5", context=[image], prompts=["Box the object held between the gripper jaws", "Is the gripper empty? Answer yes or no."])` (one prefill, both answers) | held box overlaps the gripper ROI (IoU > 0.2) and its mention matches the product → holding; "yes" → empty; a held box when empty was expected sets `observed.held_sku` (grasp pre-check: same SKU → `adopt_held`, other SKU → `return_held`). Then `combine_verdicts` with `RobotState.holding`: the camera overrides state only at ≥ 0.85 confidence, else uncertain. This catches a policy that "succeeded" while closing on air |
| `label_matches` (advisory) | wrist | `ocr(image, prompt="Read the brand, product name and size on the label of the held item")` | casefold substring of `expect_text` (+ `label_hints`) → pass; text read but no match → FAIL (→ `wrong_item_grasped` → put back); nothing read → uncertain, ignored because it is advisory |
| `basket_delta` / `basket_contains` | basket | `detect(image, classes=[names of expected SKUs])` on the baseline frame and on the current frame (or `compare(before, after, "Box every {name} in the second image")`) | per-class counts → `perception.BasketCheck.compare(expected, observed)`; delta = after − before |
| `item_on_shelf` (advisory) / `slot_visible` (advisory) | front | `detect(image, classes=["{brand} {name} {size}"])` | visible count ≥ `min_count` → pass; fewer → uncertain, never fail (back rows are hidden; the `scan_shelf` step's exact count is the only out-of-stock authority) |
| `shelf_tag_reads` | front | `ocr(image, prompt="Read the price on the shelf tag under the {name}")` | parsed integer cents == expected cents |

Atoms on the same camera are batched into one Multilook round trip. State atoms (`robot_at`, `slot_reachable`,
`battery_above`, `approval_granted`, `target_grounded`) never call Perceptron.

## Modes and honesty (`VerifierMode`, `Verdict.decided_by`)

| mode | who decides | Perceptron | default for |
|---|---|---|---|
| `decide` | Perceptron (+ state fusion) | called | stores on Isaac (readable label textures) |
| `shadow` | sim ground truth | called; its answer is shown next to the verdict (`Verdict.shadow`, `ground_truth_agrees`) | 2D-fake stores (Pillow shelves can fool a VLM) |
| `gt_only` | sim ground truth | not called | CI, no key |

Each verdict carries `provider: ProviderCall` (provider, model, request_id, latency, error_code, `attribution`), which
renders as "Perceptron Mk1.5" / "Sim ground truth" / "Robot state". Switch modes live with
`PUT /api/verifier/mode`.

## Budgets, geometry and matching (code, not the model)

- Time: the runner waits `PICKY_VERIFY_TIMEOUT_S` (8 s) and sends `deadline_s` = 7 s (`verify.verify_deadline`). Each
  provider call gets `timeout_s` = min(`PICKY_VISION_CALL_TIMEOUT_S` (4 s), what is left); pass it to the provider
  method, which passes it to both `asyncio.wait_for` and the SDK's own `timeout=`. Out of time: undecided camera atoms
  are 'uncertain' and the verdict is `degraded`; never overrun.
- Gripper ROI: `grounding.GRIPPER_ROI_WRIST` (+ `HELD_ROI_MIN_IOU` 0.2, `box_iou`). Calibrate it on the Isaac wrist
  camera before using mode `decide`.
- Mentions: `verify.match_mention_to_sku(mention, {sku: class string})` (ambiguous -> None, never a match) and
  `count_boxes_by_sku`; shelf tags: `verify.parse_price_cents`.
- Confidence: Perceptron has no calibrated score. Use >= 0.9 only when two cues agree (held box in the ROI AND "no" to
  "is the gripper empty?"), 0.7 for one cue, so a single noisy answer never overrides the robot state
  (`VISION_OVERRIDE_CONFIDENCE` = 0.85).
- Instance ids: map the grasp point to a unit with `EvidenceCameras.get_frame(frame_id).instances` (pixel bboxes kept
  on retained frames; `point_in_box` after `NormBox.from_px`). The robot arm's `TruthSource.ground_truth(frame_id)`
  raises KeyError for perception-retained frames: fall back to `truth(robot_id)` + those instances.
- Front-camera atoms and grounding need the head aimed at the slot: capture with `aim_slot_id` (Isaac: robot.scan).

## Fallback chain (never blocks the trip)

1. Perceptron answers → code decides (mode `decide`).
2. Perceptron errors or times out → ground truth decides (sim only), with `degraded=True` and
   `methods_tried=["perceptron","ground_truth"]` (fault `provider_down` exercises this).
3. The verifier service itself is down or slow (`VerifierUnavailableError` / the runner's `wait_for`) → the runner's
   `RetryPolicy.degrade` takes over:
   - `state_only` (default): `verify.evaluate_on_state` checks from RobotState by code (gripper, basket, pose,
     battery). Camera-only atoms are uncertain; advisory ones are ignored. This still catches a false grasp
     (fault `verifier_unavailable`).
   - `uncertain`: every check is uncertain, so the uncertain rules apply (reverify, then ask / accept provisionally).
4. Grounding unavailable → T0 grasps from the planogram slot (`GroundedTarget.decided_by="state"`).

## With no key (and in every test)

`PerceptronVisionProvider` answers every call with `error_code="unavailable"` (or `"sdk_missing"`) and no boxes. It
never raises. The fake verifier (`fakes/fake_verifier.py`) decides exactly from the fake camera's ground-truth sidecar.
Each camera atom is judged from what THAT frame showed, so a stale frame would mislead it unless `not_before` rejects
the frame. Its `decided_by` is `ground_truth` and its attribution is "Fake verifier". The demo story
(`drop_on_first_grasp`) runs end to end with no key at all.
