---
name: Bug
about: Something broke in the pipeline, an arm, CI, or the demo.
title: "<arm>: <what broke>"
labels: ["bug"]
assignees: []
---

**Arm / area:** <!-- catalog, routing, gateway, CI, demo, ... (also add label arm:<arm>) -->
**Demo-blocking?** <!-- yes / no -->

## What happened
<!-- Observed vs expected. Paste the error / event / log line. -->

## Repro
```bash
# e.g. PICKY_REAL_ARMS=routing make demo
```
- Branch / commit:
- `PICKY_REAL_ARMS`:
- Store / user: <!-- store_ave / maya -->

## Workaround / fallback
<!-- Does switching the arm back to its fake avoid it? -->
