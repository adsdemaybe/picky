# Picky hi-fi re-sim recording: runbook

`H=$SCRATCH/hifi` (Mac).
On vhagar, everything lives in `/data/advaiv2/picky-rec/` (`rec_hifi.py`, `sim-hifi.env`, `restart_sim_hifi.sh` are already copied there).
Total time: about 15 to 25 min (Isaac restart 2 to 4 min, trip 4 to 10 min, copy 1 min, edit 2 to 4 min).

## 0. Preconditions (1 min)
- The white-rover agent is finished and nobody else places orders until step 6 is done (the recorder records the
  first NEW store_ave goal on an Isaac robot).
- Mac tunnel up: `curl -s localhost:8100/health`. If it is down: `ssh -fN -L 8100:localhost:8100 vhagar`.
- Web app up: `curl -s -o /dev/null -w '%{http_code}\n' localhost:3001/` should print 200.
- No active goals: `curl -s 'localhost:8100/api/goals?active=true'` should print `[]`.
- Close every other viewer of the Isaac cameras: other browser tabs on /watch, /live or /fleet, and the Python harness.
  Each one keeps render products active on the single Isaac loop.

## 1. Apply the hi-fi env and restart ONLY picky-sim (2 to 4 min)
```bash
ssh vhagar 'bash /data/advaiv2/picky-rec/restart_sim_hifi.sh apply' < /dev/null
```
This command:
- appends the `export` lines of `sim-hifi.env` between `# >>> hifi` and `# <<< hifi` markers in
  `/data/advaiv2/picky/sim-judges.env`, after backing the file up to `.bak-hifi-<ts>`. Anything the white-rover agent
  changed in that file is kept.
- patches one line of `repo-judges/arms/sim/app.py` (backup `.bak-hifi-<ts>`): `JPEG_QUALITY = 75` becomes
  `int(os.environ.get("PICKY_SIM_JPEG_QUALITY") or 75)`. `PATCH_JPEG=0` skips the patch.
- kills tmux `picky-sim` and the `arms.sim.app` process whose cwd is `repo-judges` (nothing else), relaunches
  `run_sim_judges.sh` in tmux `picky-sim`, then waits until `/api/sim/status` lists Isaac robots.

When it finishes, it prints the camera table. Expect `rover-ave-*.director 1920x1080 source=isaac`.
The gateway (`picky-gw`) is NOT restarted.

## 2. Verify the new fidelity (30 s)
```bash
ssh vhagar 'cd /data/advaiv2/picky-rec && REC_NOWAIT=1 REC_ROBOT=rover-ave-1 REC_MAX_S=15 REC_OUT=/data/advaiv2/picky-rec/verify uv run --no-project --with pillow --with numpy --with "imageio[ffmpeg]" python rec_hifi.py 2>&1 | tail -3' < /dev/null
```
- Pass: `rover-ave-1.director` reports `native_sizes_seen {"1920x1080": ...}` and `real_fps` of 12 or more.
- If the director's real fps is under 8, or the trip feels sluggish: edit `/data/advaiv2/picky-rec/sim-hifi.env` to
  `PICKY_SIM_DIRECTOR_SIZE=1280x720`, run `restart_sim_hifi.sh rollback`, then run `restart_sim_hifi.sh apply` again.
  Rollback first because `apply` will not add the block twice.

## 3. Start the recorder on vhagar (instant; it waits for the order)
```bash
ssh vhagar 'tmux new -d -s picky-rec "cd /data/advaiv2/picky-rec && REC_OUT=/data/advaiv2/picky-rec/final-run REC_MAX_S=1500 uv run --no-project --with pillow --with numpy --with imageio[ffmpeg] python rec_hifi.py > /data/advaiv2/picky-rec/final-run.log 2>&1"; sleep 5; cat /data/advaiv2/picky-rec/final-run.log' < /dev/null
```
Expect `baseline: N goals ...; waiting for a NEW goal on an Isaac robot`. Options:
- `REC_ROBOT=rover-ave-1` pins the recorder to one robot.
- `REC_STOP_ON=trip` keeps recording until the trip reaches paid. The default stops 10 s after the goal completes.
- `REC_JPEG=1` also keeps every source JPEG.

## 4. Run the web capture on the Mac (this PLACES THE ORDER; 4 to 10 min)
```bash
cd $H && APP_URL=http://localhost:3001 node webcap.mjs 2>&1 | tee webcap-run.log
```
- Window: 1920x1080, deviceScaleFactor 1. Flow: Home cards, then Order these, then /watch, where it switches to
  Bird's-eye 1 at +25 s for 10 s, Bird's-eye 2 at +75 s for 8 s and Bird's-eye 1 at +140 s for 8 s (`WEBCAP_VIEWS`).
- It pauses `WEBCAP_READ_S=2.5` s before each answer. Photo checks get "Looks right", detours and extras are declined,
  and checkout gets Approve and pay. It ends 11 s after the "Paid" receipt.
- Output: `$H/webcap-<ts>/webcap_hq_1080p30.mp4`, built from the CDP screencast (JPEG q92, H.264 crf 16), plus
  `events.json`, `_video/*.webm` (the recordVideo fallback) and `receipt.png`.
- `HEADED=1` shows the browser.

While it runs, you can watch the recorder: `ssh vhagar 'tail -2 /data/advaiv2/picky-rec/final-run.log' < /dev/null`.
The `late_dups` value should stay small and `qcomp` should stay near 0.

## 5. Wait for the recorder to stop (it stops 10 s after the goal completes)
```bash
ssh vhagar 'until [ -f /data/advaiv2/picky-rec/final-run/stats.json ]; do sleep 5; done; cat /data/advaiv2/picky-rec/final-run/stats.json' < /dev/null
```
If it has to be stopped by hand, send `tmux send-keys -t picky-rec C-c`. The files are closed cleanly.

## 6. Copy to the Mac (about 1 min; roughly 0.3 to 1.5 GB)
```bash
scp -r vhagar:/data/advaiv2/picky-rec/final-run $H/
```

## 7. Edit (2 to 4 min)
```bash
cd $H && W=$(ls -dt webcap-2* | head -1) && uv run --no-project --with pillow --with numpy --with "imageio[ffmpeg]" python edit_final.py \
  --web $W/webcap_hq_1080p30.mp4 --web-events $W/events.json \
  --composite final-run/composite_1080p30.mp4 --comp-events final-run/events.jsonl --raw-dir final-run --out-dir final
```
This writes `$H/final/final_1080p30.mp4` and `$H/final/highlight_1080p30.mp4` (75 s by default; set it with
`--highlight-s 60..90`).
- For a shorter full cut, add `--isaac-speed 2`.
- `--isaac-start/--isaac-end` trim the Isaac part.
- The raw per-camera files are `final-run/<camera>.mp4`, all on one timeline with the composite, for manual inserts.

## 8. Rollback (2 to 4 min; do this after the recording, or any time something looks wrong)
```bash
ssh vhagar 'bash /data/advaiv2/picky-rec/restart_sim_hifi.sh rollback' < /dev/null
```
This removes the marker block, restores `JPEG_QUALITY = 75`, and restarts picky-sim the same way.
`restart_sim_hifi.sh status` shows the current state at any time.

## Risks
- **Isaac's render loop is the fps limit, not the settings.** Measured at 10:27-10:29 during a real trip (goal
  goal_3vxv29y7 on rover-ave-2, 960x540 director): every Isaac stream ran at only about 4.6 to 4.9 unique fps, with
  gaps of up to 1.5 s. When idle, the director runs at 9.9 fps and the other cameras at 5.0. The worker had 10 render
  products (6 to 9 active, both rovers' directors watched) at 25 to 39 ms per render. A 1080p director adds render
  cost to that same loop, so expect fewer unique fps during motion, not more.
  - The recorder still writes 30 fps CFR by duplicating frames, so motion is steppy rather than smooth.
  - Mitigations: (a) no other camera viewers (step 0); (b) `REC_CEILING=none` (one render product fewer); (c)
    `PICKY_SIM_DIRECTOR_SIZE=1280x720`.
  - Watch the recorder's 5-s log during the first minute of the trip. If the director drops under about 3 fps,
    1280x720 is the better trade.
- GPU: only one Isaac process fits on the 32 GB card. 9 render products went OOM before, when a second Isaac process
  was running. The script waits for the old worker to exit before relaunching. Never start a second Isaac process.
- If Isaac does not come back, the gateway keeps every robot on the 2D fake, so the web app still works. Run rollback
  and check `/data/advaiv2/picky/logs/judges-sim.log`.
