#!/usr/bin/env bash
set -euo pipefail

repo="${TOKEN_HEATMAP_REPO:-/opt/token-heatmap}"
data_repo="${TOKEN_HEATMAP_DATA_REPO:-/var/lib/token-heatmap/data-branch}"
main_branch="${TOKEN_HEATMAP_GITHUB_BRANCH:-master}"
data_branch="${TOKEN_HEATMAP_DATA_BRANCH:-token-data}"

cd "$repo"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "refusing to update a dirty source checkout" >&2
  exit 1
fi
git fetch origin "$main_branch"
if git merge-base --is-ancestor HEAD "origin/$main_branch"; then
  git merge --ff-only "origin/$main_branch"
elif ! git merge-base --is-ancestor "origin/$main_branch" HEAD; then
  git rebase "origin/$main_branch"
fi

cd "$data_repo"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "refusing to overwrite a dirty data checkout" >&2
  exit 1
fi
git fetch origin "$data_branch"
if git merge-base --is-ancestor HEAD "origin/$data_branch"; then
  git merge --ff-only "origin/$data_branch"
elif ! git merge-base --is-ancestor "origin/$data_branch" HEAD; then
  git rebase "origin/$data_branch"
fi

PYTHONPATH="$repo/src" /usr/bin/python3 -m token_heatmap.data export \
  --database "${TOKEN_HEATMAP_DB_PATH:-/var/lib/token-heatmap/token_usage.sqlite}" \
  --cpa-database "${TOKEN_HEATMAP_CPA_DB_PATH:-/var/lib/cpa-manager-plus/usage.sqlite}" \
  --timezone "${TOKEN_HEATMAP_TIMEZONE:-Asia/Shanghai}" \
  --output "$data_repo/daily_usage.json"

git add -- daily_usage.json
if ! git diff --cached --quiet -- daily_usage.json; then
  git -c user.name="Token Heatmap Bot" \
      -c user.email="token-heatmap@users.noreply.github.com" \
      commit --only -m "chore: update token usage data" -- daily_usage.json
fi

if [ "$(git rev-list --count "origin/$data_branch..HEAD")" -gt 0 ]; then
  git push origin "HEAD:$data_branch"
fi
