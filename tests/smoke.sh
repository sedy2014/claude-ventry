#!/usr/bin/env bash
# Minimal smoke test: runs ventry against an empty config dir; must exit 0 and write claude-code-inventory.md.
set -e
# On Windows, python3 can be a Microsoft Store stub that fails; use the first interpreter that runs.
PY=""
for c in python3 python; do
  if "$c" -c "import sys" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "smoke FAILED: no working python3/python on PATH"; exit 1; }
T="$(mktemp -d)"
CLAUDE_CONFIG_DIR="$T/.claude" HOME="$T" USERPROFILE="$T" "$PY" "$(dirname "$0")/../scripts/ventry.py" --out "$T/out" --cwd "$T" >/dev/null
test -f "$T/out/claude-code-inventory.md" && echo "smoke ok"
