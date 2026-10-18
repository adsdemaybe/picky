<!-- Title: `<arm>: <imperative summary>`  (e.g. `routing: add OR-Tools orienteering with skip penalties`)
     Branch: arm/<arm> (whole arm) · feat/<arm>-<slug> · fix/<arm>-<slug> · contracts/<slug> · utils/<slug> · chore/<slug>
     See CLAUDE.md §5 (how to contribute) and §6 (definition of demo-ready). -->

## Summary

**Arm:** <!-- catalog | perception | sim | robot | routing | agent | recommender | trust | messaging | frontend | gateway | contracts | utils | chore -->
**Type:** <!-- arm (first merge) | feat | fix | contracts | utils | chore -->

Closes # <!-- bug/idea issue, if any (arms are tracked on their draft PR, not an issue) -->

<!-- 1–3 sentences: what this adds to the pipeline (CLAUDE.md §2) and which stage it plugs into. -->

## Spec items completed (CLAUDE.md §3)

<!-- Copy the relevant lines from the arm's draft-PR checklist (= the scope in arms/<arm>/README.md) and tick what this
     PR delivers. [x] = works in `make demo` with the REAL impl; [x] ... (fake) = fake only (CLAUDE.md §5 Rules). -->
- [ ]
- [ ]

## Demo evidence

<!-- Screenshot / GIF / log excerpt of a full trip through the pipeline with this arm REAL. -->

- `PICKY_REAL_ARMS` used: `...`
- Evidence:

## Fallback

- Switch back to the fake with: `PICKY_REAL_ARMS=<list without this arm>` (no code change)
- Other fallback flag / degraded mode (if any):

## Known limitations

<!-- What does NOT work yet, edge cases skipped on purpose. Also recorded in arms/<arm>/README.md. -->
-

## Checklist

**Contracts**
- [ ] `contracts/` is unchanged (arm/feat/fix PRs must not change contracts)
- [ ] OR this is a `contracts/<slug>` PR: `CONTRACTS_VERSION` bumped, affected arms listed below, their fakes updated here

**Demo-ready (CLAUDE.md §6)**
- [ ] Implements its `contracts` Protocol fully; no `NotImplementedError` on demo paths
- [ ] `PICKY_REAL_ARMS=<arm> make demo` completes a trip
- [ ] Predictable failures degrade + log + emit an `error` event (empty search, OOS, timeout, bad LLM output, sim disconnect, payment decline)
- [ ] Can be switched back to its fake by env flag alone
- [ ] Core-logic tests added and passing
- [ ] No secrets committed; new env vars added to `.env.example` (and `utils/config.py`)
- [ ] `arms/<arm>/README.md` updated: what works, known limitations, how to run, fallback flag

**Pre-PR checks (CLAUDE.md §5 step 6)**
- [ ] `make check` passes (lint, format, rules sync, tests; `make fmt` fixes formatting)
- [ ] `make demo` passes (all fakes)
- [ ] `make rules` run and `.cursor/rules/` committed (if `CLAUDE.md` or any arm README changed)
- [ ] Merged latest `main` (`git fetch && git merge origin/main`, no rebase/force-push); conflicts resolved on this branch

Affected arms (contracts PRs only):
