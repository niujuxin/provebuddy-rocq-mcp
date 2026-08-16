#!/usr/bin/env bash
# Initialize this repository: fetch the submodule and verify it is pinned
# to the expected commit.
set -euo pipefail
cd "$(dirname "$0")/.."

PIN=6983113d0844c0b7f987c79dab13988445109bfb

git submodule update --init --recursive

actual=$(git -C third_party/rocq-mcp rev-parse HEAD 2>/dev/null || true)
if [ "$actual" != "$PIN" ]; then
  echo "WARNING: third_party/rocq-mcp is not on the pinned commit" >&2
  echo "expected: $PIN" >&2
  echo "actual:   $actual" >&2
  exit 1
fi

echo "submodule ready: $(git -C third_party/rocq-mcp rev-parse --short HEAD)"
