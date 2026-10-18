#!/usr/bin/env bash
# Branch + PR cleanup for adsdemaybe/picky. Run AFTER the release PR #28 is merged into main.
#
# For every OPEN PR (except none):
#   - head already in main, or merging it into main would change nothing -> close it with a comment.
#   - otherwise (real unmerged work) -> left open; if its base is not main, retarget the base to main
#     first (so deleting the old base branch below does not auto-close it).
# For every remote branch except main:
#   - contained in main, or merging it into main would change nothing -> delete it (nothing is lost:
#     its commits/changes are in main).
#   - listed in ARCHIVE -> push tag archive/<branch> at its tip, then delete the branch (recoverable).
#   - anything else (unmerged work) -> kept, reported.
# A branch that is still the head of an open PR is never deleted.
#
# Usage: bash cleanup_branches.sh --dry-run   (prints the plan, changes nothing)
#        bash cleanup_branches.sh --simulate origin/release/demo-1018   (plan as if main were that rev)
#        bash cleanup_branches.sh             (does it)
set -uo pipefail

REPO="adsdemaybe/picky"
RELEASE_PR=28
ARCHIVE=(feat/demo-video feat/tmobile-store)
DRY=0
KEEP=""
SIMULATE=""  # --simulate <rev>: dry run that pretends main is <rev> (to preview the plan before #28 merges)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --simulate) DRY=1; SIMULATE=$2; shift ;;
    --keep) KEEP=$2; shift ;;  # comma-separated branches to leave alone this run (e.g. still being pushed to)
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
run() { if [[ $DRY == 1 ]]; then echo "   DRY: $*"; else "$@" < /dev/null; fi; }

cd "$(git rev-parse --show-toplevel)" || exit 2
state=$(gh pr view "$RELEASE_PR" -R "$REPO" --json state --jq .state) || exit 2
if [[ "$state" != "MERGED" && -z "$SIMULATE" ]]; then
  echo "#$RELEASE_PR is $state, not MERGED. Merge it first; nothing was changed." >&2
  exit 1
fi
git fetch -q --prune origin || exit 2
MAIN=$(git rev-parse "${SIMULATE:-origin/main}")
echo "main = ${MAIN:0:7}; $([[ $DRY == 1 ]] && echo 'DRY RUN')"

# 0 = merging <rev> into main changes nothing (already in main or no-op); 1 = it has real changes
noop_into_main() {
  local rev=$1 tree
  git merge-base --is-ancestor "$rev" "$MAIN" && return 0
  tree=$(git merge-tree --write-tree "$MAIN" "$rev" 2>/dev/null | head -1) || return 1
  [[ "$tree" == "$(git rev-parse "$MAIN^{tree}")" ]]
}
in_archive() { local b; for b in "${ARCHIVE[@]}"; do [[ "$b" == "$1" ]] && return 0; done; return 1; }

echo "== open PRs"
keep_heads=("")  # sentinel: bash 3.2 + set -u rejects empty arrays
while read -r n head base sha; do
  [[ -z "$n" ]] && continue
  git cat-file -e "$sha" 2>/dev/null || git fetch -q origin "$sha" 2>/dev/null < /dev/null
  if [[ ",$KEEP," == *",$head,"* ]]; then
    echo "#$n ($head): --keep -> left open this run"
    keep_heads+=("$head")
    if [[ "$base" != "main" ]]; then  # its base may be deleted below; GitHub closes PRs whose base disappears
      echo "   base $base -> main"
      run gh pr edit "$n" -R "$REPO" --base main
    fi
  elif noop_into_main "$sha"; then
    echo "#$n ($head): in main -> close"
    run gh pr close "$n" -R "$REPO" --comment "Landed in main via #$RELEASE_PR (cleanup: this branch adds nothing that main does not already have)."
  else
    echo "#$n ($head): has changes not in main -> keep open"
    keep_heads+=("$head")
    if [[ "$base" != "main" ]]; then
      echo "   base $base -> main"
      run gh pr edit "$n" -R "$REPO" --base main
    fi
  fi
done < <(gh pr list -R "$REPO" --state open --limit 100 --json number,headRefName,baseRefName,headRefOid \
  --jq '.[] | "\(.number) \(.headRefName) \(.baseRefName) \(.headRefOid)"')

echo "== remote branches"
for ref in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin); do
  b=${ref#origin/}
  [[ "$b" == "main" || "$b" == "HEAD" || "$b" == "origin" ]] && continue
  if [[ ",$KEEP," == *",$b,"* ]]; then
    echo "$b: --keep -> left alone this run"
    continue
  fi
  if printf '%s\n' "${keep_heads[@]}" | grep -qx "$b"; then
    echo "$b: head of an open PR with unmerged work -> keep"
    continue
  fi
  if noop_into_main "$ref"; then
    echo "$b: in main -> delete"
    run git push -q origin --delete "$b"
  elif in_archive "$b"; then
    echo "$b: superseded WIP -> tag archive/$b at $(git rev-parse --short "$ref"), delete branch"
    run git push -q origin "$(git rev-parse "$ref"):refs/tags/archive/$b"
    run git push -q origin --delete "$b"
  else
    echo "$b: has changes not in main -> keep"
  fi
done
echo "done."
