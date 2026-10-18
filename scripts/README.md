# scripts/

Repo tooling (CLAUDE.md §4). Arm entrypoints run as `python -m arms.<arm>.<module>`, not from here.

| Path | What it does |
|---|---|
| `sync_agent_rules.py` | Generates `.cursor/rules/*.mdc` from CLAUDE.md and the arm READMEs (`make rules`). |
| `genie_to_planogram.py` | Generates the `store_genie` planogram/products/prim map from the Genie Sim supermarket scene (`gen`, `gen --check`). |
| `e2e/` | Playwright judges'-flow harness: suggestions → order → live Isaac streams → photo checks → counter unload → Visa payment agent → receipt, plus API / stream / real-robot checks. `APP_URL=http://localhost:3001 GATEWAY_URL=http://localhost:8100 ./scripts/e2e/run.sh` (`./scripts/e2e/run.sh install` once). `summ.py` summarises a trip event log. See `e2e/README.md`. |
| `demo/vhagar/` | Deploy and run the live judges' stack on the GPU host: `deploy_vhagar.sh` (rsync + deps incl. the routing extra + restart gateway/worker), `run_gw_judges.sh`, `run_sim_judges.sh`, `RUNBOOK.md`. |
| `demo/recording/` | Recording tools: `rec_streams.py` (composite MP4 of a robot's MJPEG streams + step log), `rec_hifi.py` (on-host per-camera + composite recorder), `webcap.mjs` (1080p web-app capture of the judges' flow), `edit_final.py` (final cut + highlight), `restart_sim_hifi.sh` + `sim-hifi.env` (hi-fi camera settings for the worker), `RUN.md`. |
| `release/cleanup_branches.sh` | After a release PR merges: closes PRs whose work is in main, deletes merged branches, archives superseded WIP branches as tags (`--dry-run`, `--simulate <rev>`, `--keep a,b`). |
| `dev/wip_snapshot.sh` | Snapshots uncommitted work of every git worktree (patch + untracked tar) into `$PICKY_WIP_BACKUPS` (default `../picky-wip-backups`); run it in a loop during long sessions. |

Paths like `$SCRATCH`, `<repo>` and the vhagar host/dirs in the runbooks are placeholders or env-overridable defaults.
