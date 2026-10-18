# SO-101 runs

Logs, frames (`scene.jpg`, `after.jpg`), `episode.json` files, replayable recipes (`recipes/`) and the learned
Q-policy (`q_policy.json`) from the physical SO-101 pick runs on 2026-10-18.

The five run videos (`gum_success1.mp4`, `gum_success2.mp4`, `oreo_success1.mp4`, `oreo_success2.mp4`,
`20261018-021122/video.mp4`, 7-14 MB each) are not kept in `main`, to keep large binaries out of the repo. They are in
commit `0dbc9fd` on `arm/robot` (PR #6): `git show 0dbc9fd:runs/20261018-023731/oreo_success2.mp4 > oreo_success2.mp4`.
`.gitignore` ignores `runs/**/*.mp4` so new recordings stay local.
