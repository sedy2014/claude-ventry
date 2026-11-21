# ventry: Claude Code inventory

Know what your Claude Code is running. ventry inventories your **hooks, plugins, MCP servers, skills, slash commands and subagents** and writes them as Obsidian-friendly markdown notes, with **real usage counts** mined from your local session transcripts, so you can see what's installed but never used.

> Status: v0.1 (inventory export). Session-start suggestions (status line) are planned, see Roadmap.

## What you get

A folder of notes (default `~/.claude/ventry/`, or any folder, e.g. inside an Obsidian vault):

| Note | Contents |
|---|---|
| `claude-code-inventory.md` | counts per area + **installed but never used** list |
| `plugins.md` | plugin, marketplace, version, scope, enabled |
| `hooks.md` | source, event, matcher, command (across user/project/local/plugin) |
| `mcp-servers.md` | server, source, transport, calls, last used |
| `skills.md`, `commands.md`, `agents.md` | description, calls, last used |

Every note has YAML frontmatter (`type`, `generated`, `count`, `tags`) so Dataview queries work, and `claude-code-inventory.md` links the rest with `[[wikilinks]]`.

## Install

```
/plugin marketplace add sedy2014/claude-ventry
/plugin install ventry@ventry
```

Local testing before publishing: `/plugin marketplace add C:\repos\ventry`

## Use

- **Automatic:** a `SessionStart` hook refreshes the notes in the background (throttled to once per 6 hours, prints nothing, never delays startup).
- **On demand:** `/ventry:ventry` (optionally `--out DIR`, `--no-usage`) runs the export and summarises prune candidates.
- **Standalone:** `python scripts/ventry.py --out ~/Vault/claude-ventry`

Set `VENTRY_OUT` to change the default output folder.

## Privacy

ventry only reads local files (`~/.claude/settings.json`, project `.claude/`, plugin folders, `~/.claude/projects/*.jsonl`). It extracts tool *names* and slash-command names only, never prompt or code content, and it makes **no network calls**.

Besides the notes, it writes two small files to the output folder (`<claude dir>/ventry/` by default, or wherever `--out` points): `.last-run` (the throttle timestamp) and `.ventry-cache.json` (per-transcript counts).

The notes themselves contain your project's absolute path, full hook commands, and MCP server URLs or command lines. Those can include tokens or other secrets, so keep the output folder private and do not sync it to a shared vault.

## Limits (be aware)

- Hook firings are not recorded in transcripts, so hooks have no counts. Use `claude --debug`.
- "Unused" means unused in the transcripts still on disk. Treat it as a prune *candidate*.
- Settings, plugin registry and transcript formats can change between Claude Code versions; ventry parses defensively and fails quietly.
- Requires Python 3 on PATH (`python3` or `python`). The script uses only the standard library. The unit tests need Python 3.10 or newer.

## Roadmap

1. Session-start suggestions via the status line, driven by an editable `rules/suggestions.yaml` (repo signals to suggested MCP servers / hooks). Suggest only, once per item per session, with accept/dismiss logging.
2. Optional per-item notes for richer Dataview queries.

## License

MIT
