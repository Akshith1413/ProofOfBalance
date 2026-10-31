#!/usr/bin/env bash
# Create or update ONE comment per PR, identified by a hidden marker, so the
# gate edits its previous verdict instead of piling up comments.
#   scripts/ci/pr_comment.sh <pr-number> <body-file> <marker>
# Needs GH_TOKEN and GITHUB_REPOSITORY (both set inside GitHub Actions).
set -euo pipefail
pr="$1" file="$2" marker="<!-- $3 -->"
[[ -s "$file" ]] || { echo "pr_comment: $file is empty, nothing to post" >&2; exit 0; }

body="$(cat "$file")"$'\n\n'"$marker"
id=$(gh api "repos/$GITHUB_REPOSITORY/issues/$pr/comments" --paginate \
       --jq ".[] | select(.body | contains(\"$marker\")) | .id" | tail -n 1)
if [[ -n "$id" ]]; then
  gh api -X PATCH "repos/$GITHUB_REPOSITORY/issues/comments/$id" -f body="$body" >/dev/null
  echo "pr_comment: updated comment $id"
else
  gh api "repos/$GITHUB_REPOSITORY/issues/$pr/comments" -f body="$body" >/dev/null
  echo "pr_comment: created comment"
fi
