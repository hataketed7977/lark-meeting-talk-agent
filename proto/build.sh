#!/usr/bin/env bash
# Generate Python protobuf stubs for lark_meeting_voice.
# Requires: protoc (>= 3.20). Outputs to ../lark_meeting_voice/_pb/
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../lark_meeting_voice/_pb"
mkdir -p "$OUT"
touch "$OUT/__init__.py"

protoc \
  --proto_path="$HERE" \
  --python_out="$OUT" \
  "$HERE/frontier.proto" \
  "$HERE/meeting_realtime.proto"

# protoc generates absolute package-rooted imports; rewrite to local package.
# Nothing to fix here because both files are in the same _pb dir.
echo "Generated:"
ls -1 "$OUT"
