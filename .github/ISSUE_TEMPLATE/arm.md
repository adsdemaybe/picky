---
name: Arm tracking issue
about: Only for proposing a NEW arm. Existing arms are claimed and tracked on their draft PR (CLAUDE.md §5).
title: "new arm: <arm>"
labels: ["arm"]
assignees: []
---

<!-- Existing arms: do NOT open this. Claim the arm's draft PR instead:
     `gh pr list --label arm:<arm>` -> `gh pr edit <n> --add-assignee @me` (CLAUDE.md §5 step 2).
     New arm: once agreed, cut `arm/<arm>` off main, open a draft PR with this checklist as its body
     (`gh pr create --draft --base main --label arm:<arm>`), and close this issue. The draft PR is the claim. -->

**Arm:** `<arm>`
**Owner:** @
**Branch:** `arm/<arm>` (off `main`; merged by one PR when demo-ready)
**Contract:** `contracts/services.py` → `<Protocol>` · README: `arms/<arm>/README.md`
**Real deps / extra:** `uv sync --extra <arm>` · env keys:
**Fallback:** <!-- from CLAUDE.md §9 -->

## Scope

### P0 — the demo trip (must work end to end)
- [ ]
- [ ]

### P1 — strengthens the demo / rubric
- [ ]

### P2 — stretch, only after P0 is on `main`
- [ ]

## Demo-ready checklist (CLAUDE.md §6)
- [ ] Implements its `contracts` Protocol fully; no `NotImplementedError` on demo paths
- [ ] `PICKY_REAL_ARMS=<arm> make demo` completes a trip (others real-or-fake)
- [ ] Predictable failures degrade + log + emit an `error` event
- [ ] Switchable back to the fake by env flag alone
- [ ] Core-logic tests pass; CI green
- [ ] No secrets committed; new env vars in `.env.example`
- [ ] README updated: what works, known limitations, how to run, fallback flag
- [ ] Draft PR marked ready with demo evidence (`gh pr ready <n>`)

## Notes / blockers
<!-- Time-box blockers at ~45 min, then switch to the documented fallback and note it here + in the README. -->
