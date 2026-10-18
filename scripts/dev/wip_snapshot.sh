#!/usr/bin/env bash
# Snapshot uncommitted work in every git worktree of the picky repo (patch of tracked changes + tar of untracked files)
# into $PICKY_WIP_BACKUPS (default: ../picky-wip-backups next to the repo). Loop: while true; do scripts/dev/wip_snapshot.sh; sleep 600; done Read-only on git (--no-optional-locks), so it never blocks agents working in the worktrees.
set -uo pipefail
REPO="${PICKY_REPO:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}"
BACKUPS="${PICKY_WIP_BACKUPS:-$REPO/../picky-wip-backups}"
OUT=$BACKUPS/$(date +%Y%m%d-%H%M)
mkdir -p "$OUT"
git -C "$REPO" --no-optional-locks worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
  [ -d "$wt" ] || continue
  name=$(basename "$wt"); br=$(git -C "$wt" --no-optional-locks rev-parse --abbrev-ref HEAD 2>/dev/null)
  head=$(git -C "$wt" --no-optional-locks rev-parse HEAD 2>/dev/null)
  git -C "$wt" --no-optional-locks diff HEAD > "$OUT/$name.patch" 2>/dev/null
  git -C "$wt" --no-optional-locks ls-files --others --exclude-standard -z 2>/dev/null > "$OUT/$name.untracked"
  if [ -s "$OUT/$name.untracked" ]; then
    (cd "$wt" && xargs -0 tar -czf "$OUT/$name.untracked.tgz" < "$OUT/$name.untracked" 2>/dev/null)
  fi
  [ -s "$OUT/$name.patch" ] || [ -s "$OUT/$name.untracked" ] || { rm -f "$OUT/$name.patch" "$OUT/$name.untracked"; continue; }
  echo "$name $br $head" >> "$OUT/INDEX.txt"
done
[ -s "$OUT/INDEX.txt" ] || rmdir "$OUT" 2>/dev/null
ls -1dt "$BACKUPS"/2* 2>/dev/null | tail -n +25 | xargs rm -rf 2>/dev/null   # keep the last 24 snapshots
