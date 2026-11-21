---
description: Export an inventory of your hooks, plugins, MCP servers and skills (with usage counts) to markdown
allowed-tools: Bash(python3:*), Bash(python:*)
argument-hint: "[--out DIR] [--no-usage]"
---

Run the ventry export and summarise the result for me.

!`python3 -c "" >/dev/null 2>&1 && python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ventry.py" $ARGUMENTS 2>&1 || python "${CLAUDE_PLUGIN_ROOT}/scripts/ventry.py" $ARGUMENTS 2>&1`

In your summary: say where the notes were written, list anything installed but never used, and point out any hook or MCP server that looks like a candidate to disable. Keep it short.
