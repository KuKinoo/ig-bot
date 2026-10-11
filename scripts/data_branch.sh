#!/usr/bin/env bash
# Keeps the bot's state (state.json) and draft images on a dedicated `data` branch of this repository.
#
#   data_branch.sh pull            download the branch into ./data (empty folder if it does not exist yet)
#   data_branch.sh push "message"  publish ./data as a fresh single-commit snapshot (history never grows)
#
# Needs GH_TOKEN (the workflow's GITHUB_TOKEN) and GITHUB_REPOSITORY. REMOTE_URL overrides the remote (tests).
set -euo pipefail

if [ -n "${REMOTE_URL:-}" ]; then
  REMOTE="$REMOTE_URL"
else
  : "${GH_TOKEN:?GH_TOKEN is required}" "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
  REMOTE="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
fi

case "${1:-}" in
  pull)
    rm -rf data
    if git ls-remote --exit-code --heads "$REMOTE" data >/dev/null 2>&1; then
      git clone --quiet --depth 1 --branch data "$REMOTE" data
      rm -rf data/.git
    else
      mkdir -p data
    fi
    mkdir -p data/media
    ;;
  push)
    mkdir -p data/media
    # Keep empty folders out of the way, but make sure the branch is never empty.
    [ -e data/.gitkeep ] || : > data/.gitkeep
    cd data
    rm -rf .git
    git init -q -b data
    git config user.name "ig-bot"
    git config user.email "ig-bot@users.noreply.github.com"
    git add -A
    git commit -q -m "${2:-update}"
    git push -q --force "$REMOTE" HEAD:data
    rm -rf .git
    ;;
  *)
    echo "usage: $0 pull | push \"message\"" >&2
    exit 2
    ;;
esac
