#!/usr/bin/env bash
# Check whether upstream has updates (read-only; does not change the pin).
# Usage: ./scripts/sync-upstream.sh
set -euo pipefail
cd "$(dirname "$0")/.."

SUB=third_party/rocq-mcp
PIN=$(git -C "$SUB" rev-parse HEAD)

echo "current pin: $PIN"
echo "fetching latest upstream..."
git -C "$SUB" fetch origin

NEW_MAIN=$(git -C "$SUB" rev-parse origin/main)
echo "upstream latest main: $NEW_MAIN"

if [ "$PIN" = "$NEW_MAIN" ]; then
  echo "Already up to date."
  exit 0
fi

echo
echo "Upstream commits since pin:"
git -C "$SUB" log --oneline "$PIN..origin/main"

echo
echo "To upgrade: check out the new commit in third_party/rocq-mcp, git add it"
echo "from the repo root, then update the pin record in README and NOTICE."
