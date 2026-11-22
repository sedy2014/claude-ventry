#!/usr/bin/env python3
"""ventry: inventory of Claude Code hooks, plugins, MCP servers, skills,
commands and agents, exported as Obsidian-friendly markdown with usage counts
mined from local session transcripts.

Stdlib only. Reads local files only; never touches the network.
In --quiet mode (SessionStart hook) it prints nothing and detaches so it
never delays or pollutes session startup.

How the script is organised (top to bottom):
  1. helpers      small utilities: find the Claude folder, read JSON safely, format text
  2. inventory    collect_* functions read config files to list what is installed
  3. usage        scan_transcripts / usage_for count how often each item was used
  4. output       build() turns everything into markdown notes in the output folder
  5. main()       command-line entry point, including the silent hook mode

Design rules that explain many choices below:
  - Never crash. Every file read tolerates missing files and wrong JSON shapes,
    because this runs at every session start.
  - Never read prompt or code text. Transcripts are scanned only for tool names.
  - SessionStart hook output goes into Claude's context, so --quiet prints nothing.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"

# In --quiet (hook) mode, do nothing if the notes were refreshed less than this
# many seconds ago. 6 hours, so opening many sessions does not rescan every time.
THROTTLE_SECONDS = 6 * 3600

# The summary note: counts per area plus the installed-but-never-used list.
SUMMARY_NOTE = "claude-code-inventory.md"


# ---------------------------------------------------------------- helpers
def claude_dir():
    """Return the folder where Claude Code keeps its user-level config.

    This is normally ~/.claude (for example C:\\Users\\<you>\\.claude). It holds
    settings.json, plugins/, skills/, commands/, agents/ and projects/ (the
    session transcripts). If the CLAUDE_CONFIG_DIR environment variable is set,
    that folder is used instead; the tests rely on this to point ventry at a
    fake, throwaway config folder.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def read_json(path):
    """Read a JSON file and return it as a dict, or None on any problem.

    Returns None if the file is missing, unreadable, not valid JSON, or is JSON
    that is not an object (for example a list). Callers write `read_json(p) or {}`
    so a bad file simply looks like an empty one instead of raising an error.
    The "utf-8-sig" encoding also accepts files that start with a byte-order mark
    (BOM), which some Windows editors add.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def frontmatter_desc(path):
    """Return the description from a SKILL.md / command / agent file."""
    # These markdown files start with a YAML block between two "---" lines, for
    # example:  ---\ndescription: Does a thing\n---.  We read only that one field.
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return ""
    # Capture everything between the opening "---" and the next "---" line.
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    # Find the line "description: ..." and strip any surrounding quotes.
    d = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
    return d.group(1).strip().strip("\"'") if d else ""


def esc(s):
    """Make a value safe inside a markdown table cell (no pipes, no newlines)."""
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def short(s, n=90):
    """Shorten text to at most n characters, ending with an ellipsis if cut."""
    s = str(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- inventory
def settings_files(cdir, cwd):
    """List the three settings.json files Claude Code reads, as (scope, path).

    user    - the settings in your Claude folder, applying to every project
    project - <project>/.claude/settings.json, shared with the project's team
    local   - <project>/.claude/settings.local.json, your private overrides
    The scope name becomes the "source" label in the notes.
    """
    return [
        ("user", cdir / "settings.json"),
        ("project", cwd / ".claude" / "settings.json"),
        ("local", cwd / ".claude" / "settings.local.json"),
    ]


def same_path(a, b):
    """True if two paths point to the same folder (case-insensitive on Windows)."""
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError):
        return False


def mapping(value):
    """Return value if it is a dict, otherwise an empty dict.

    A safety net for odd JSON: `mapping(x).get("k")` never crashes, even when x
    is None, a list or a string.
    """
    return value if isinstance(value, dict) else {}


def sequence(value):
    """Return value if it is a list, otherwise an empty list (same idea as mapping)."""
    return value if isinstance(value, list) else []


def collect_hooks(cdir, cwd, plugins):
    """List every hook from the settings files and from installed plugins.

    Returns a list of dicts: source (where it came from), event (such as
    SessionStart), matcher (which tools it applies to), type and command.
    """
    rows = []

    def add(source, event, block):
        # A hook "block" looks like {"matcher": "Bash", "hooks": [{...}, {...}]}.
        # One block can hold several hooks, so each hook becomes its own row.
        matcher = block.get("matcher", "*") if isinstance(block, dict) else "*"
        for h in sequence(mapping(block).get("hooks")):
            if not isinstance(h, dict):
                continue
            rows.append({
                "source": source,
                "event": event,
                "matcher": matcher or "*",
                "type": h.get("type", "command"),
                "command": h.get("command") or h.get("prompt") or "",
            })

    # Hooks defined in settings.json files (user, project, local).
    for scope, path in settings_files(cdir, cwd):
        data = read_json(path) or {}
        for event, blocks in mapping(data.get("hooks")).items():
            for b in sequence(blocks):
                add(scope, event, b)
    # Hooks shipped inside plugins (each plugin may have hooks/hooks.json).
    for p in plugins:
        hj = read_json(Path(p["path"]) / "hooks" / "hooks.json") if p["path"] else None
        for event, blocks in mapping((hj or {}).get("hooks")).items():
            for b in sequence(blocks):
                add("plugin:" + p["name"], event, b)
    return rows


def plugin_desc(path):
    """The "description" from a plugin's own .claude-plugin/plugin.json, or ""."""
    if not path:
        return ""
    d = mapping(read_json(Path(path) / ".claude-plugin" / "plugin.json")).get("description")
    return d if isinstance(d, str) else ""


def mcp_tools(totals, recorded):
    """Tools used on one MCP server, most used first, from mcptool: totals."""
    prefix = "mcptool:" + recorded + "/"
    used = [(v["count"], k[len(prefix):]) for k, v in totals.items() if k.startswith(prefix)]
    return [t for _, t in sorted(used, key=lambda x: (-x[0], x[1]))]


def collect_plugins(cdir, cwd):
    """List installed plugins, from two places.

    1. plugins/installed_plugins.json - plugins installed with /plugin install.
    2. plugins/synced/<bucket>/manifest.json - plugins synced from your account.
       These are not in installed_plugins.json, so they need their own reader.

    Each result has: name, marketplace, version, scope, enabled (True, False or
    None when unknown) and path (the plugin's folder, used later to find its
    hooks, skills and MCP servers).
    """
    # Which plugins are switched on, from the "enabledPlugins" setting.
    enabled = {}
    for _, path in settings_files(cdir, cwd):
        for k, v in mapping((read_json(path) or {}).get("enabledPlugins")).items():
            enabled[k] = bool(v)
    reg = read_json(cdir / "plugins" / "installed_plugins.json") or {}
    # Newer files wrap the list as {"version": .., "plugins": {...}}; older ones
    # are the bare dict. Accept both.
    plugins = reg.get("plugins", reg)
    out = []
    if isinstance(plugins, dict):
        # Keys look like "name@marketplace"; each value is a list of installs.
        for key, val in plugins.items():
            entries = val if isinstance(val, list) else [val]
            for e in entries:
                if not isinstance(e, dict):
                    continue
                # A project or local install belongs to one project. Skip it
                # when exporting a different project. User installs apply
                # everywhere. If projectPath is missing, keep the entry.
                proj_path = e.get("projectPath")
                if (e.get("scope") in ("project", "local") and isinstance(proj_path, str)
                        and not same_path(proj_path, cwd)):
                    continue
                path = e.get("installPath") or e.get("path") or ""
                name = key.split("@")[0]
                market = key.split("@")[1] if "@" in key else ""
                out.append({
                    "name": name,
                    "marketplace": market,
                    "version": e.get("version", ""),
                    "scope": e.get("scope", ""),
                    "enabled": enabled.get(key, None),
                    "path": path if isinstance(path, str) and "\x00" not in path else "",
                })
                out[-1]["desc"] = plugin_desc(out[-1]["path"])
    # Account-synced plugins live in plugins/synced/<bucket>/<manifest name>/
    # and are not listed in installed_plugins.json.
    try:
        manifests = sorted((cdir / "plugins" / "synced").glob("*/manifest.json"))
    except Exception:
        manifests = []
    seen = {o["name"] for o in out}  # avoid listing the same plugin twice
    for mf in manifests:
        for e in sequence(mapping(read_json(mf)).get("plugins")):
            if not isinstance(e, dict) or not isinstance(e.get("name"), str):
                continue
            # In the manifest, "name" is an ID; it is also the plugin's folder name.
            pdir = mf.parent / e["name"]
            if not pdir.is_dir():
                continue
            # The human-readable name (for example "marketing") is in the plugin's
            # own .claude-plugin/plugin.json.
            meta = mapping(read_json(pdir / ".claude-plugin" / "plugin.json"))
            name = meta.get("name") if isinstance(meta.get("name"), str) else e["name"]
            if name in seen:
                continue
            seen.add(name)
            market = e.get("marketplaceName")
            version = meta.get("version", e.get("version", ""))
            out.append({
                "name": name,
                "marketplace": market if isinstance(market, str) else "synced",
                "version": version if isinstance(version, str) else "",
                "scope": "synced",
                "enabled": None,  # synced plugins have no enabledPlugins entry
                "path": str(pdir),
                "desc": meta.get("description") if isinstance(meta.get("description"), str) else "",
            })
    return out


def collect_mcp(cdir, cwd, plugins):
    """List configured MCP servers from four places, each labelled by source.

    user          - "mcpServers" in ~/.claude.json (applies everywhere)
    local         - the same file, under projects[<this project>] (private to you)
    project       - .mcp.json in the project folder (shared with the team)
    plugin:<name> - .mcp.json inside each installed plugin
    Servers that are built in or added through claude.ai are not in any of
    these files, so they do not appear here.
    """
    rows = []

    def add(source, servers):
        for name, cfg in mapping(servers).items():
            cfg = cfg if isinstance(cfg, dict) else {}
            rows.append({
                "name": name,
                "source": source,
                # "type" is http/sse/stdio; a server with a "command" is stdio;
                # anything else shows "?".
                "transport": cfg.get("type") or ("stdio" if "command" in cfg else "?"),
                "target": cfg.get("url") or cfg.get("command") or "",
            })

    # Normally ~/.claude.json sits NEXT TO the .claude folder, not inside it.
    # With CLAUDE_CONFIG_DIR set, read only that profile's own .claude.json and
    # never fall back to the default profile's file.
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        top = read_json(cdir / ".claude.json") or {}
    else:
        top = read_json(Path.home() / ".claude.json") or {}
    add("user", top.get("mcpServers"))
    # Per-project servers are stored under the project's path; try both slash styles.
    proj = mapping(top.get("projects")).get(str(cwd)) or mapping(top.get("projects")).get(cwd.as_posix()) or {}
    add("local", mapping(proj).get("mcpServers"))
    add("project", (read_json(cwd / ".mcp.json") or {}).get("mcpServers"))
    for p in plugins:
        if p["path"]:
            mj = read_json(Path(p["path"]) / ".mcp.json") or {}
            # Some plugins wrap servers in {"mcpServers": {...}}, others list
            # them at the top level; accept both.
            add("plugin:" + p["name"], mj.get("mcpServers", mj))
    return rows


def collect_md_items(roots, filename=None):
    """roots: list of (source, dir). Finds SKILL.md dirs or *.md files.

    With filename="SKILL.md" (skills): every <root>/<skill-name>/SKILL.md is one
    skill, named after its folder. Without filename (commands and agents): every
    .md file under the root is one item, named by its path without ".md"
    (so commands/team/review.md becomes "team/review").
    Each row has: name, source, desc (from the file's frontmatter) and path.
    """
    rows = []
    for source, root in roots:
        if not root.is_dir():
            continue
        if filename:
            for f in sorted(root.glob("*/" + filename)):
                rows.append({"name": f.parent.name, "source": source,
                             "desc": frontmatter_desc(f), "path": str(f)})
        else:
            for f in sorted(root.rglob("*.md")):
                rel = f.relative_to(root).with_suffix("")
                rows.append({"name": rel.as_posix(), "source": source,
                             "desc": frontmatter_desc(f), "path": str(f)})
    return rows


# ---------------------------------------------------------------- usage
# Claude Code emits the message tag on either side of the name.
COMMAND_MESSAGE_RE = re.compile(r"<command-message>[^<]*</command-message>")
COMMAND_NAME_RE = re.compile(r"<command-name>/?([^<>\s]+)</command-name>")

# Version 3 counts only the command record's own name.
CACHE_VERSION = 4  # 4 adds per-tool MCP counts (mcptool:<server>/<tool>)


def command_names(text):
    """Read one command header, treating any arguments as opaque text."""
    header, has_args, args = text.strip().partition("<command-args>")
    if has_args and not args.endswith("</command-args>"):
        return []
    header = header.strip()
    message = COMMAND_MESSAGE_RE.match(header)
    if message:
        header = header[message.end():].lstrip()
    name = COMMAND_NAME_RE.match(header)
    if not name:
        return []
    tail = header[name.end():].strip()
    if tail and (message or not COMMAND_MESSAGE_RE.fullmatch(tail)):
        return []
    return [name.group(1)]


def scan_transcripts(cdir, use_cache=True, out_dir=None):
    """Return counts + last-used timestamps keyed by kind:name.

    Reads every session transcript (*.jsonl, one JSON object per line) under
    <claude dir>/projects/ and counts, by name only:
      mcp:<server>   MCP tool calls           (tool name mcp__<server>__<tool>)
      skill:<name>   Skill tool calls
      agent:<type>   Task/Agent tool calls    (the subagent type)
      cmd:<name>     slash commands you typed (<command-name> markers)
    The result looks like {"skill:brag": {"count": 3, "last": "2026-09-15T..."}}.

    Speed: transcripts are large (over 100 MB here), so per-file counts are
    cached in .ventry-cache.json in the output folder. A file is re-read only if
    its size or modified time changed since the last run.
    """
    projects = cdir / "projects"
    cache_path = (out_dir or cdir / "ventry") / ".ventry-cache.json"
    cache = (read_json(cache_path) or {}) if use_cache else {}
    # Ignore a cache written by a different format version. Bump CACHE_VERSION
    # whenever counting rules change, so old counts are rebuilt.
    files_cache = mapping(cache.get("files")) if cache.get("v") == CACHE_VERSION else {}
    new_cache = {}
    totals = {}

    def bump(key, ts):
        c = totals.setdefault(key, {"count": 0, "last": ""})
        c["count"] += 1
        if ts and ts > c["last"]:
            c["last"] = ts

    if projects.is_dir():
        for f in projects.rglob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            # "Signature" of the file: size and modified time. If both match the
            # cache, the file has not changed and its saved counts are reused.
            sig = [st.st_size, int(st.st_mtime)]
            key = str(f)
            ent = mapping(files_cache.get(key))
            saved = ent.get("counts")
            # Reuse the cached counts only if the signature matches AND the saved
            # data has the expected shape (a damaged cache entry is rebuilt).
            if (ent.get("sig") == sig and isinstance(saved, dict)
                    and all(isinstance(v, dict) and type(v.get("count")) is int
                            and isinstance(v.get("last"), str) for v in saved.values())):
                counts = saved
            else:
                counts = {}
                try:
                    with open(f, "r", encoding="utf-8-sig", errors="replace") as fh:
                        for line in fh:
                            # Cheap pre-filter: skip lines that cannot hold a
                            # tool call or slash command, before parsing JSON.
                            if "tool_use" not in line and "command-name" not in line:
                                continue
                            try:
                                obj = json.loads(line)
                            except Exception:
                                continue  # truncated or corrupt line: skip it
                            if not isinstance(obj, dict):
                                continue
                            ts = obj.get("timestamp")
                            ts = ts if isinstance(ts, str) else ""
                            msg = mapping(obj.get("message"))
                            content = msg.get("content")
                            # Slash commands count only in messages YOU sent. A
                            # marker quoted in Claude's reply is not a use.
                            is_user = obj.get("type") == "user" or msg.get("role") == "user"
                            if isinstance(content, list):
                                # Assistant messages: a list of blocks. A block of
                                # type "tool_use" has a "name" and an "input".
                                for blk in content:
                                    if not isinstance(blk, dict):
                                        continue
                                    if blk.get("type") == "tool_use":
                                        extra_keys = []
                                        n = blk.get("name", "")
                                        if not isinstance(n, str):
                                            continue
                                        inp = blk.get("input", {})
                                        if not isinstance(inp, dict):
                                            continue
                                        if n.startswith("mcp__"):
                                            # mcp__<server>__<tool>: the server is
                                            # the part between the first two "__".
                                            # Plugin servers appear as
                                            # plugin_<plugin>_<server> (see usage_for).
                                            parts = n.split("__")
                                            k = "mcp:" + parts[1]
                                            # Also count the tool itself, so the
                                            # notes can say what a server is used for.
                                            tool = "__".join(parts[2:])
                                            if tool:
                                                extra_keys.append("mcptool:" + parts[1] + "/" + tool)
                                        elif n == "Skill":
                                            # The Skill tool's input names the skill.
                                            skill = inp.get("skill", inp.get("command", "?"))
                                            if not isinstance(skill, str):
                                                continue
                                            k = "skill:" + skill
                                        elif n in ("Task", "Agent"):
                                            # The subagent tool; input.subagent_type
                                            # says which kind of agent was started.
                                            agent = inp.get("subagent_type", "general-purpose")
                                            if not isinstance(agent, str):
                                                continue
                                            k = "agent:" + agent
                                        else:
                                            continue  # any other tool: not counted
                                        for kk in [k] + extra_keys:
                                            c = counts.setdefault(kk, {"count": 0, "last": ""})
                                            c["count"] += 1
                                            if ts > c["last"]:
                                                c["last"] = ts
                                    elif is_user and blk.get("type") == "text" and isinstance(blk.get("text"), str):
                                        for m in command_names(blk["text"]):
                                            c = counts.setdefault("cmd:" + m, {"count": 0, "last": ""})
                                            c["count"] += 1
                                            if ts > c["last"]:
                                                c["last"] = ts
                            elif is_user and isinstance(content, str):
                                # User messages: slash commands are stored as a
                                # plain string, not a list of blocks.
                                for m in command_names(content):
                                    c = counts.setdefault("cmd:" + m, {"count": 0, "last": ""})
                                    c["count"] += 1
                                    if ts > c["last"]:
                                        c["last"] = ts
                except OSError:
                    continue
            new_cache[key] = {"sig": sig, "counts": counts}
            # Add this file's counts into the grand totals.
            for k, v in counts.items():
                c = totals.setdefault(k, {"count": 0, "last": ""})
                c["count"] += v["count"]
                if v["last"] > c["last"]:
                    c["last"] = v["last"]
    # Save the cache for next time. Failing to save is harmless.
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"v": CACHE_VERSION, "files": new_cache}), encoding="utf-8")
    except OSError:
        pass
    return totals


def usage_for(totals, kind, name, extra_names=()):
    """Sum usage for one item. Returns (call count, last-used date YYYY-MM-DD).

    Looks through the totals from scan_transcripts for entries of the given kind
    ("mcp", "skill", "cmd" or "agent") whose recorded name equals `name` or one
    of `extra_names` EXACTLY. The caller passes the name as transcripts record
    it (see recorded_name), so two plugins' "review" skills never share counts
    and "web_search" is never counted for "search".
    """
    cnt, last = 0, ""
    for k, v in totals.items():
        if not k.startswith(kind + ":"):
            continue
        n = k.split(":", 1)[1]
        if n == name or n in extra_names:
            cnt += v["count"]
            last = max(last, v["last"])
    return cnt, last[:10]


def recorded_name(kind, row):
    """The name transcripts use for an inventory row.

    Items from your own or the project's folders are recorded by their plain
    name. Plugin items carry the plugin's name: MCP servers as
    plugin_<plugin>_<server>, and skills, commands and agents as <plugin>:<name>.
    """
    src = row["source"]
    if not src.startswith("plugin:"):
        return row["name"]
    plugin = src[len("plugin:"):]
    if kind == "mcp":
        return "plugin_" + plugin + "_" + row["name"]
    return plugin + ":" + row["name"]


# ---------------------------------------------------------------- output
def table(headers, rows):
    """Render a markdown table, or "_none found_" if there are no rows."""
    if not rows:
        return "_none found_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def note(title, ntype, generated, body, extra_fm=None):
    """Wrap a body in an Obsidian note: YAML frontmatter, then a heading.

    The frontmatter (between the "---" lines) becomes Obsidian's Properties
    panel: a type, a generated timestamp, tags, and any extra fields given.
    """
    fm = ["---", "type: ventry-" + ntype, "generated: " + generated, "tags: [ventry, claude-code]"]
    for k, v in (extra_fm or {}).items():
        fm.append(f"{k}: {v}")
    fm.append("---")
    return "\n".join(fm) + f"\n\n# {title}\n\n{body}\n"


def build(args):
    """Collect everything, count usage, and write the markdown notes.

    Writes plugins.md, hooks.md, mcp-servers.md, skills.md, commands.md,
    agents.md and the summary note (SUMMARY_NOTE) into the output folder, plus a .last-run timestamp
    used by the --quiet throttle. Returns (output folder, note names, number of
    installed-but-unused items).
    """
    cdir = claude_dir()
    cwd = Path(args.cwd).resolve()
    out_dir = out_root(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Where to write: always the main folder (latest overall view). With
    # --per-project, also a snapshot folder for this project, whose links point
    # inside that folder so they never open the main folder's notes.
    targets = [(out_dir, "")]
    if getattr(args, "per_project", False):
        slug = project_slug(cwd)
        targets.append((out_dir / "projects" / slug, "projects/" + slug + "/"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- 1. inventory: what is installed ---
    plugins = collect_plugins(cdir, cwd)
    hooks = collect_hooks(cdir, cwd, plugins)
    mcp = collect_mcp(cdir, cwd, plugins)
    # Folders to search for skills, commands and agents: your user folder, the
    # project's .claude folder, and each plugin's folders.
    skill_roots = [("user", cdir / "skills"), ("project", cwd / ".claude" / "skills")]
    cmd_roots = [("user", cdir / "commands"), ("project", cwd / ".claude" / "commands")]
    agent_roots = [("user", cdir / "agents"), ("project", cwd / ".claude" / "agents")]
    for p in plugins:
        if p["path"]:
            pp = Path(p["path"])
            skill_roots.append(("plugin:" + p["name"], pp / "skills"))
            cmd_roots.append(("plugin:" + p["name"], pp / "commands"))
            agent_roots.append(("plugin:" + p["name"], pp / "agents"))
    skills = collect_md_items(skill_roots, "SKILL.md")
    commands = collect_md_items(cmd_roots)
    agents = collect_md_items(agent_roots)

    # --- 2. usage: how often each was used (skipped with --no-usage) ---
    totals = scan_transcripts(cdir, out_dir=out_dir) if not args.no_usage else {}
    have_usage = bool(totals) or not args.no_usage

    unused = []  # items installed but never seen in any transcript

    def u(kind, row):
        # With --no-usage the counts are blank rather than a misleading 0.
        if args.no_usage:
            return "", ""
        return usage_for(totals, kind, recorded_name(kind, row))

    # --- 3. build the table rows for each note ---
    # plugins
    prow = []
    for p in plugins:
        prow.append([p["name"], short(p.get("desc", "")), p["marketplace"], p["version"], p["scope"],
                     {True: "yes", False: "no", None: "?"}[p["enabled"]]])
    plugin_descs = {p["name"]: p.get("desc", "") for p in plugins}
    # hooks (fire counts are not in transcripts; list only)
    hrow = [[h["source"], h["event"], h["matcher"], h["type"], "`" + short(h["command"], 80) + "`"] for h in hooks]
    # mcp
    mrow = []
    for m in mcp:
        c, last = u("mcp", m)
        # What it is for: the tools you actually called on it (from your
        # transcripts), else the description of the plugin that provides it.
        tools = mcp_tools(totals, recorded_name("mcp", m))
        if tools:
            desc = "tools used: " + ", ".join(tools[:4]) + (" …" if len(tools) > 4 else "")
        elif m["source"].startswith("plugin:") and plugin_descs.get(m["source"][len("plugin:"):]):
            desc = "from plugin: " + plugin_descs[m["source"][len("plugin:"):]]
        else:
            desc = ""
        mrow.append([m["name"], m["source"], short(desc), m["transport"], short(m["target"], 50), c, last])
        if not args.no_usage and c == 0:
            unused.append(("MCP server", m["name"], m["source"]))
    # skills
    srow = []
    for s in skills:
        c, last = u("skill", s)
        srow.append([s["name"], s["source"], short(s["desc"]), c, last])
        if not args.no_usage and c == 0:
            unused.append(("Skill", s["name"], s["source"]))
    crow = []
    for s in commands:
        c, last = u("cmd", s)
        # Show the name you type: plugin commands are /<plugin>:<command>.
        typed = "/" + recorded_name("cmd", s)
        crow.append([typed, s["source"], short(s["desc"]), c, last])
        if not args.no_usage and c == 0:
            unused.append(("Command", typed, s["source"]))
    arow = []
    for s in agents:
        c, last = u("agent", s)
        arow.append([s["name"], s["source"], short(s["desc"]), c, last])
        if not args.no_usage and c == 0:
            unused.append(("Agent", s["name"], s["source"]))

    # --- 4. assemble the note files ---
    ug = "Usage = calls found in local transcripts under `projects/`.\n\n"
    hug = "Hook firings are not recorded in transcripts, so hooks are listed without counts. Use `claude --debug` to watch them fire.\n\n"
    files = {
        "plugins.md": note("Plugins", "plugins", now, table(["plugin", "description", "marketplace", "version", "scope", "enabled"], prow), {"count": len(prow)}),
        "hooks.md": note("Hooks", "hooks", now, hug + table(["source", "event", "matcher", "type", "command"], hrow), {"count": len(hrow)}),
        "mcp-servers.md": note("MCP servers", "mcp", now, ug + table(["server", "source", "description", "transport", "target", "calls", "last used"], mrow), {"count": len(mrow)}),
        "skills.md": note("Skills", "skills", now, ug + table(["skill", "source", "description", "calls", "last used"], srow), {"count": len(srow)}),
        "commands.md": note("Slash commands", "commands", now, ug + table(["command", "source", "description", "uses", "last used"], crow), {"count": len(crow)}),
        "agents.md": note("Subagents", "agents", now, ug + table(["agent", "source", "description", "calls", "last used"], arow), {"count": len(arow)}),
    }
    if args.no_usage:
        unused_md = "_usage scan skipped (--no-usage)_\n"
    elif unused:
        unused_md = table(["kind", "name", "source"], unused)
    else:
        unused_md = "_nothing installed-but-unused found_\n"
    counts = [("plugins", len(prow)), ("hooks", len(hrow)), ("mcp-servers", len(mrow)),
              ("skills", len(srow)), ("commands", len(crow)), ("agents", len(arow))]

    def summary(prefix):
        # The summary note links to the others with [[wikilinks]] (Obsidian
        # syntax). A project snapshot uses path links, [[projects/<slug>/hooks\|hooks]]
        # (the backslash keeps the alias pipe from splitting the table cell).
        def link(n):
            return f"[[{prefix}{n}\\|{n}]]" if prefix else f"[[{n}]]"
        rows = "".join(f"| {link(n)} | {c} |\n" for n, c in counts)
        idx = (
            "Setup snapshot for `" + str(cwd) + "`.\n\n"
            "| area | count |\n|---|---|\n" + rows + "\n"
            "## Installed but never used\n\n" + unused_md +
            "\n_Prune candidates only. Unused here may just mean not used in the transcripts that still exist on disk._\n"
        )
        return note("Claude Code inventory", "index", now, idx, {"ventry_version": VERSION})

    # Never overwrite a file ventry did not write (for example your own note
    # in an Obsidian vault). Check every target first, so nothing is half-written.
    for folder, _ in targets:
        for name in list(files) + [SUMMARY_NOTE]:
            target = folder / name
            if target.exists() and not is_ventry_note(target):
                raise RuntimeError(f"refusing to overwrite {target}: it is not a ventry note")
    for folder, prefix in targets:
        folder.mkdir(parents=True, exist_ok=True)
        # newline="\n" keeps line endings the same on Windows and Linux. open() is
        # used instead of Path.write_text(newline=...), which needs Python 3.10.
        for name, text in list(files.items()) + [(SUMMARY_NOTE, summary(prefix))]:
            with open(folder / name, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        # Record when we last ran; should_skip() reads this for the --quiet throttle.
        (folder / ".last-run").write_text(str(int(time.time())), encoding="utf-8")
    return out_dir, [folder for folder, _ in targets], len(unused)


def out_root(args):
    """Main output folder: --out, else $VENTRY_OUT, else <claude dir>/ventry."""
    if args.out:
        return Path(args.out).expanduser()
    return Path(os.environ.get("VENTRY_OUT", claude_dir() / "ventry")).expanduser()


def project_slug(cwd):
    """Folder name for a project snapshot: <folder name>-<6-char hash of its path>.

    The hash keeps two projects with the same folder name (for example two
    folders called "app") apart. Same project, same slug, on every run.
    """
    # Resolve first, so ".", "..", relative paths and symlinks give the same
    # slug for the build (resolved cwd) and the throttle check (raw --cwd).
    p = Path(cwd).resolve()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", p.name).strip("-.") or "project"
    digest = hashlib.sha1(os.path.normcase(str(p)).encode("utf-8")).hexdigest()[:6]
    return f"{name}-{digest}"


def throttle_dir(args):
    """Folder whose .last-run decides the --quiet throttle.

    With --per-project each project has its own 6 hour timer, so opening a new
    project refreshes its snapshot straight away.
    """
    root = out_root(args)
    if getattr(args, "per_project", False):
        return root / "projects" / project_slug(Path(args.cwd))
    return root


def is_ventry_note(path):
    """True if the file starts with ventry's frontmatter ("---" then "type: ventry-")."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            head = fh.read(64)
    except OSError:
        return False
    return head.replace("\r\n", "\n").startswith("---\ntype: ventry-")


def should_skip(out_dir):
    """True if the notes in out_dir were refreshed less than THROTTLE_SECONDS ago.

    Reads the .last-run timestamp. If it is missing or unreadable we do NOT skip,
    so the first run always happens.
    """
    try:
        last = int((out_dir / ".last-run").read_text())
        return time.time() - last < THROTTLE_SECONDS
    except Exception:
        return False


def main():
    """Command-line entry point. Returns the process exit code.

    Two ways to run:
      normal   `python ventry.py [--out DIR] [--no-usage]` builds the notes now
               and prints a short summary.
      --quiet  used by the SessionStart hook. It prints NOTHING (hook output is
               injected into Claude's context) and must not slow startup. So it
               checks the 6 hour throttle, then starts a second, detached copy of
               this script (--worker) that does the real work in the background,
               and returns immediately.
    """
    ap = argparse.ArgumentParser(prog="ventry", description=__doc__)
    ap.add_argument("--out", help="output folder (default: <claude dir>/ventry or $VENTRY_OUT)")
    ap.add_argument("--cwd", default=os.getcwd(), help="project dir to inspect")
    ap.add_argument("--no-usage", action="store_true", help="skip transcript scan")
    ap.add_argument("--quiet", action="store_true", help="hook mode: silent, throttled, detached")
    ap.add_argument("--per-project", action="store_true",
                    help="also write a snapshot for this project to <out>/projects/<project>-<id>/")
    # Internal flag: marks the background copy so it does the work itself
    # instead of launching yet another copy. Hidden from --help.
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.quiet and not args.worker:
        if should_skip(throttle_dir(args)):
            return 0  # refreshed recently: do nothing
        # Relaunch this same script as a background worker, passing the options on.
        cmd = [sys.executable, os.path.abspath(__file__), "--worker", "--quiet", "--cwd", args.cwd]
        if args.out:
            cmd += ["--out", args.out]
        if args.no_usage:
            cmd.append("--no-usage")
        if args.per_project:
            cmd.append("--per-project")
        # Detach from the console: no input, and any output is thrown away.
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            # Windows: run without a console window, in its own process group, so
            # it keeps running after this process exits.
            kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # macOS/Linux: start a new session so the worker outlives this process.
            kw["start_new_session"] = True
        try:
            subprocess.Popen(cmd, **kw)
        except Exception:
            pass  # never break session startup
        return 0

    # Normal run, or the background worker: do the real work.
    try:
        out_dir, folders, n_unused = build(args)
    except Exception as e:
        if not args.quiet:
            print(f"ventry: failed: {e}", file=sys.stderr)
        # In quiet mode a failure is silent and exits 0, so the hook never errors.
        return 0 if args.quiet else 1
    if not args.quiet:
        print(f"ventry: wrote notes to {out_dir}")
        for extra in folders[1:]:
            print(f"ventry: project snapshot in {extra}")
        if args.no_usage:
            print("ventry: usage scan skipped (--no-usage)")
        else:
            print(f"ventry: {n_unused} installed item(s) with no recorded use (see {SUMMARY_NOTE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
