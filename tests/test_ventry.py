"""Unit tests for scripts/ventry.py. Standard library only (unittest).

Run from the repo root:
    python -m unittest discover tests -v

Every test builds a small fake Claude config folder in a temporary directory and
points ventry at it, so your real ~/.claude is never read or changed.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ventry.py"

# Put the repo root on the import path, then import scripts/ventry.py as
# "scripts.ventry". A plain import (rather than loading by file path) lets static
# tools such as CodeGraph link these tests to scripts/ventry.py.
sys.path.insert(0, str(ROOT))
from scripts import ventry  # noqa: E402


def write(path, text, encoding="utf-8"):
    """Create parent folders and write a text file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding, newline="\n")
    return path


def write_json(path, obj, encoding="utf-8"):
    return write(path, json.dumps(obj), encoding)


def tool_use(name, inp, ts="2026-11-18T10:00:00Z"):
    """One transcript line (as a JSON string) holding a single tool call."""
    return json.dumps({"timestamp": ts, "message": {"content": [
        {"type": "tool_use", "name": name, "input": inp}]}})


class FakeClaude(unittest.TestCase):
    """Base class: gives each test a throwaway ~/.claude, project and home."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cdir = self.root / ".claude"      # fake Claude config folder
        self.cwd = self.root / "proj"          # fake project folder
        self.cdir.mkdir()
        self.cwd.mkdir()
        # Make "home" the temp folder so collect_mcp never reads the real
        # ~/.claude.json when the fake one is missing.
        patcher = mock.patch.object(Path, "home", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)


# ------------------------------------------------------------------ helpers
class TestHelpers(unittest.TestCase):
    def test_claude_dir_uses_env_override(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/some/where"}):
            self.assertEqual(ventry.claude_dir(), Path("/some/where"))

    def test_claude_dir_default_is_home_dot_claude(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ventry.claude_dir(), Path.home() / ".claude")

    def test_short_and_esc(self):
        self.assertEqual(ventry.short("abcdef", 4), "abc…")
        self.assertEqual(ventry.short("abc", 4), "abc")
        self.assertEqual(ventry.esc("a|b\nc"), "a\\|b c")

    def test_table_empty_and_filled(self):
        self.assertEqual(ventry.table(["a"], []), "_none found_\n")
        out = ventry.table(["a", "b"], [["1", "x|y"]])
        self.assertIn("| a | b |", out)
        self.assertIn("x\\|y", out)

    def test_mapping_and_sequence_guards(self):
        self.assertEqual(ventry.mapping(None), {})
        self.assertEqual(ventry.mapping([1]), {})
        self.assertEqual(ventry.sequence("x"), [])
        self.assertEqual(ventry.sequence([1]), [1])


class TestReadJson(FakeClaude):
    def test_missing_file_is_none(self):
        self.assertIsNone(ventry.read_json(self.root / "nope.json"))

    def test_valid_object(self):
        p = write_json(self.root / "a.json", {"k": 1})
        self.assertEqual(ventry.read_json(p), {"k": 1})

    def test_bom_is_accepted(self):
        p = write_json(self.root / "bom.json", {"k": 1}, encoding="utf-8-sig")
        self.assertEqual(ventry.read_json(p), {"k": 1})

    def test_invalid_json_is_none(self):
        self.assertIsNone(ventry.read_json(write(self.root / "bad.json", "{oops")))

    def test_non_object_json_is_none(self):
        self.assertIsNone(ventry.read_json(write(self.root / "list.json", "[1, 2]")))


class TestFrontmatter(FakeClaude):
    def test_description_read_and_unquoted(self):
        p = write(self.root / "s.md", '---\nname: x\ndescription: "Does a thing"\n---\nbody')
        self.assertEqual(ventry.frontmatter_desc(p), "Does a thing")

    def test_bom_and_missing_frontmatter(self):
        p = write(self.root / "b.md", "---\ndescription: hi\n---\n", encoding="utf-8-sig")
        self.assertEqual(ventry.frontmatter_desc(p), "hi")
        self.assertEqual(ventry.frontmatter_desc(write(self.root / "n.md", "no front matter")), "")
        self.assertEqual(ventry.frontmatter_desc(self.root / "missing.md"), "")


# ---------------------------------------------------------------- inventory
class TestHooks(FakeClaude):
    def test_settings_hooks_are_listed(self):
        write_json(self.cdir / "settings.json", {"hooks": {"SessionStart": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}})
        rows = ventry.collect_hooks(self.cdir, self.cwd, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "user")
        self.assertEqual(rows[0]["event"], "SessionStart")
        self.assertEqual(rows[0]["matcher"], "Bash")
        self.assertEqual(rows[0]["command"], "echo hi")

    def test_plugin_hooks_are_labelled_with_plugin_name(self):
        pdir = self.root / "plug"
        write_json(pdir / "hooks" / "hooks.json", {"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "x"}]}]}})
        rows = ventry.collect_hooks(self.cdir, self.cwd, [{"name": "demo", "path": str(pdir)}])
        self.assertEqual([r["source"] for r in rows], ["plugin:demo"])
        self.assertEqual(rows[0]["matcher"], "*")  # default when no matcher given

    def test_malformed_entries_are_skipped(self):
        # Same shapes as tutorial step 13: strings where objects belong.
        write_json(self.cdir / "settings.json", {
            "hooks": {"SessionStart": [{"hooks": ["oops", {"type": "command", "command": "echo hi"}]}, "notastring"],
                      "Stop": "bad"}})
        rows = ventry.collect_hooks(self.cdir, self.cwd, [])
        self.assertEqual([r["command"] for r in rows], ["echo hi"])


class TestPlugins(FakeClaude):
    def test_installed_plugin_with_enabled_flag(self):
        write_json(self.cdir / "settings.json", {"enabledPlugins": {"a@m": True}})
        write_json(self.cdir / "plugins" / "installed_plugins.json", {"plugins": {
            "a@m": [{"scope": "user", "version": "1.0", "installPath": "/x/a"}]}})
        rows = ventry.collect_plugins(self.cdir, self.cwd)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["marketplace"], rows[0]["version"]), ("a", "m", "1.0"))
        self.assertIs(rows[0]["enabled"], True)
        self.assertEqual(rows[0]["path"], "/x/a")

    def test_project_install_only_shows_in_its_project(self):
        other = self.root / "other-proj"
        other.mkdir()
        write_json(self.cdir / "plugins" / "installed_plugins.json", {"plugins": {
            "mine@m": [{"scope": "project", "projectPath": str(self.cwd), "installPath": "/x/mine"}],
            "theirs@m": [{"scope": "local", "projectPath": str(other), "installPath": "/x/theirs"}],
            "everywhere@m": [{"scope": "user", "installPath": "/x/all"}]}})
        names = sorted(r["name"] for r in ventry.collect_plugins(self.cdir, self.cwd))
        self.assertEqual(names, ["everywhere", "mine"])

    def test_bare_registry_shape_is_accepted(self):
        write_json(self.cdir / "plugins" / "installed_plugins.json",
                   {"a@m": [{"installPath": "/x/a"}]})
        self.assertEqual([r["name"] for r in ventry.collect_plugins(self.cdir, self.cwd)], ["a"])

    def test_malformed_registry_entries_do_not_crash(self):
        write_json(self.cdir / "plugins" / "installed_plugins.json", {"plugins": {
            "a@b": [None, "str", {"installPath": 123}]}})
        write_json(self.cdir / "settings.json", {"enabledPlugins": ["x"]})
        rows = ventry.collect_plugins(self.cdir, self.cwd)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "")  # a number is not a usable path
        self.assertIsNone(rows[0]["enabled"])

    def test_synced_plugin_is_found_by_real_name(self):
        # plugins/synced/<bucket>/manifest.json lists an ID; the real name is
        # in that plugin folder's .claude-plugin/plugin.json.
        bucket = self.cdir / "plugins" / "synced" / "bucket1"
        write_json(bucket / "manifest.json", {"lastUpdated": 1, "plugins": [
            {"name": "abc-123", "marketplaceName": "dir", "version": "0001"}]})
        write_json(bucket / "abc-123" / ".claude-plugin" / "plugin.json",
                   {"name": "marketing", "version": "1.2.0"})
        rows = ventry.collect_plugins(self.cdir, self.cwd)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["scope"], rows[0]["version"]),
                         ("marketing", "synced", "1.2.0"))
        self.assertIsNone(rows[0]["enabled"])
        self.assertEqual(rows[0]["path"], str(bucket / "abc-123"))

    def test_plugin_description_is_read_from_its_manifest(self):
        pdir = self.root / "a"
        write_json(pdir / ".claude-plugin" / "plugin.json", {"name": "a", "description": "Does A"})
        write_json(self.cdir / "plugins" / "installed_plugins.json",
                   {"plugins": {"a@m": [{"installPath": str(pdir)}], "b@m": [{"installPath": "/nowhere"}]}})
        rows = {r["name"]: r["desc"] for r in ventry.collect_plugins(self.cdir, self.cwd)}
        self.assertEqual(rows, {"a": "Does A", "b": ""})

    def test_synced_plugin_missing_folder_is_skipped(self):
        bucket = self.cdir / "plugins" / "synced" / "bucket1"
        write_json(bucket / "manifest.json", {"plugins": [{"name": "gone"}]})
        self.assertEqual(ventry.collect_plugins(self.cdir, self.cwd), [])

    def test_synced_plugin_not_duplicated(self):
        write_json(self.cdir / "plugins" / "installed_plugins.json",
                   {"plugins": {"marketing@x": [{"installPath": "/p"}]}})
        bucket = self.cdir / "plugins" / "synced" / "b"
        write_json(bucket / "manifest.json", {"plugins": [{"name": "id1"}]})
        write_json(bucket / "id1" / ".claude-plugin" / "plugin.json", {"name": "marketing"})
        self.assertEqual(len(ventry.collect_plugins(self.cdir, self.cwd)), 1)


class TestMcp(FakeClaude):
    def setUp(self):
        super().setUp()
        # Default profile unless a test sets CLAUDE_CONFIG_DIR itself.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_custom_profile_does_not_read_default_profile(self):
        write_json(self.root / ".claude.json", {"mcpServers": {"default-srv": {"command": "x"}}})
        custom = self.root / "work-profile"
        write_json(custom / ".claude.json", {"mcpServers": {"work-srv": {"command": "y"}}})
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom)}):
            names = [r["name"] for r in ventry.collect_mcp(custom, self.cwd, [])]
        self.assertEqual(names, ["work-srv"])

    def test_user_local_project_and_plugin_sources(self):
        write_json(self.root / ".claude.json", {
            "mcpServers": {"u": {"type": "http", "url": "https://u"}},
            "projects": {str(self.cwd): {"mcpServers": {"l": {"command": "node"}}}}})
        write_json(self.cwd / ".mcp.json", {"mcpServers": {"p": {"transport": "http", "url": "https://p"}}})
        pdir = self.root / "plug"
        write_json(pdir / ".mcp.json", {"mcpServers": {"w": {"type": "sse", "url": "https://w"}}})
        rows = {r["name"]: r for r in ventry.collect_mcp(
            self.cdir, self.cwd, [{"name": "plug", "path": str(pdir)}])}
        self.assertEqual(rows["u"]["source"], "user")
        self.assertEqual(rows["u"]["transport"], "http")
        self.assertEqual(rows["l"]["source"], "local")
        self.assertEqual(rows["l"]["transport"], "stdio")   # has a "command"
        self.assertEqual(rows["p"]["source"], "project")
        self.assertEqual(rows["p"]["transport"], "?")       # non-standard "transport" key
        self.assertEqual(rows["w"]["source"], "plugin:plug")

    def test_plugin_mcp_json_without_wrapper(self):
        pdir = self.root / "plug"
        write_json(pdir / ".mcp.json", {"bare": {"type": "http", "url": "https://b"}})
        rows = ventry.collect_mcp(self.cdir, self.cwd, [{"name": "plug", "path": str(pdir)}])
        self.assertEqual([r["name"] for r in rows], ["bare"])

    def test_nothing_configured_returns_empty(self):
        self.assertEqual(ventry.collect_mcp(self.cdir, self.cwd, []), [])


class TestMdItems(FakeClaude):
    def test_skills_are_named_after_their_folder(self):
        write(self.cdir / "skills" / "demo" / "SKILL.md", "---\ndescription: d\n---\n")
        write(self.cdir / "skills" / "synced" / "other.txt", "not a skill")
        rows = ventry.collect_md_items([("user", self.cdir / "skills")], "SKILL.md")
        self.assertEqual([(r["name"], r["desc"]) for r in rows], [("demo", "d")])

    def test_commands_use_path_without_extension(self):
        write(self.cdir / "commands" / "team" / "review.md", "---\ndescription: r\n---\n")
        write(self.cdir / "commands" / "hello.md", "hi")
        rows = ventry.collect_md_items([("user", self.cdir / "commands")])
        self.assertEqual(sorted(r["name"] for r in rows), ["hello", "team/review"])

    def test_missing_root_is_ignored(self):
        self.assertEqual(ventry.collect_md_items([("user", self.root / "nope")], "SKILL.md"), [])


# -------------------------------------------------------------------- usage
class TestScanTranscripts(FakeClaude):
    def transcript(self, lines, name="t.jsonl", encoding="utf-8"):
        return write(self.cdir / "projects" / "p1" / name, "\n".join(lines) + "\n", encoding)

    def scan(self, out=None):
        return ventry.scan_transcripts(self.cdir, out_dir=out or self.root / "out")

    def test_counts_skill_mcp_agent_and_commands(self):
        self.transcript([
            tool_use("Skill", {"skill": "demo"}, "2026-11-18T10:00:00Z"),
            tool_use("Skill", {"skill": "demo"}, "2026-11-19T10:00:00Z"),
            tool_use("mcp__plugin_marketing_notion__notion-search", {"query": "x"}),
            tool_use("Task", {"subagent_type": "Explore"}),
            tool_use("Agent", {}),                       # no subagent_type given
            tool_use("Bash", {"command": "ls"}),         # other tools are ignored
            # slash command typed by the user: content is a plain string
            json.dumps({"type": "user", "timestamp": "2026-11-18T11:00:00Z", "message": {
                "content": "<command-name>/hello</command-name>"}}),
            # slash command inside a list of text blocks
            json.dumps({"type": "user", "message": {"content": [
                {"type": "text", "text": "<command-name>/hello</command-name>"}]}}),
        ])
        t = self.scan()
        self.assertEqual(t["skill:demo"]["count"], 2)
        self.assertEqual(t["skill:demo"]["last"], "2026-11-19T10:00:00Z")
        self.assertEqual(t["mcp:plugin_marketing_notion"]["count"], 1)
        self.assertEqual(t["mcptool:plugin_marketing_notion/notion-search"]["count"], 1)
        self.assertEqual(t["agent:Explore"]["count"], 1)
        self.assertEqual(t["agent:general-purpose"]["count"], 1)
        self.assertEqual(t["cmd:hello"]["count"], 2)
        self.assertNotIn("Bash", " ".join(t))

    def test_marker_quoted_by_assistant_is_not_counted(self):
        self.transcript([
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "You typed <command-name>/hello</command-name>"},
                {"type": "tool_use", "name": "Skill", "input": {"skill": "demo"}}]}}),
        ])
        t = self.scan()
        self.assertNotIn("cmd:hello", t)
        self.assertEqual(t["skill:demo"]["count"], 1)  # tool calls still count

    def test_marker_quoted_by_user_is_not_counted(self):
        self.transcript([
            # A real command record starts with the tag: counted.
            json.dumps({"type": "user", "message": {"content":
                "<command-message>deploy</command-message>\n<command-name>/deploy</command-name>"}}),
            # The user asking about a command, as a plain string: not counted.
            json.dumps({"type": "user", "message": {"content":
                "what does <command-name>/deploy</command-name> mean?"}}),
            # The same question as a text block: not counted.
            json.dumps({"type": "user", "message": {"content": [
                {"type": "text", "text": "explain <command-name>/deploy</command-name>"}]}}),
        ])
        self.assertEqual(self.scan()["cmd:deploy"]["count"], 1)

    def test_command_marker_followed_by_prose(self):
        self.transcript([json.dumps({"type": "user", "message": {"content":
            "<command-name>/deploy</command-name> means what?"}})])
        self.assertEqual(self.scan(), {})

    def test_command_marker_in_arguments(self):
        content = ("<command-name>/explain</command-name><command-message>explain</command-message>"
                   "<command-args>Explain <command-name>/deploy</command-name></command-args>")
        self.transcript([json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": content}]}})])
        totals = self.scan()
        self.assertEqual(totals["cmd:explain"]["count"], 1)
        self.assertNotIn("cmd:deploy", totals)

    def test_old_cache_version_is_rebuilt(self):
        f = self.transcript([tool_use("Skill", {"skill": "demo"})])
        out = self.root / "out"
        st = f.stat()
        write_json(out / ".ventry-cache.json", {"v": 2, "files": {str(f): {
            "sig": [st.st_size, int(st.st_mtime)],
            "counts": {"skill:demo": {"count": 50, "last": ""}}}}})
        self.assertEqual(self.scan(out)["skill:demo"]["count"], 1)  # not the stale 50

    def test_broken_lines_are_skipped(self):
        good = tool_use("Skill", {"skill": "demo"})
        self.transcript([
            good,
            '{"timestamp":"2026-11-18T10:00:01Z","message":{"content":[{"type":"tool_use","name":"Ski',  # cut off
            '["tool_use"]',                                           # JSON but not an object
            '{"message":{"content":5},"x":"tool_use"}',               # content is a number
            '{"message":{"content":[{"type":"tool_use","name":"Skill","input":"notadict"}]}}',
            '{"message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":7}}]}}',
            good,
        ], encoding="utf-8-sig")  # also starts with a BOM
        self.assertEqual(self.scan()["skill:demo"]["count"], 2)

    def test_no_projects_folder_gives_empty_totals(self):
        self.assertEqual(self.scan(), {})

    def test_cache_is_written_and_reused(self):
        f = self.transcript([tool_use("Skill", {"skill": "demo"})])
        out = self.root / "out"
        first = self.scan(out)
        cache_path = out / ".ventry-cache.json"
        self.assertTrue(cache_path.is_file())
        # Change the saved counts without touching the transcript. If the second
        # scan returns the tampered number, it reused the cache instead of re-reading.
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cache["files"][str(f)]["counts"]["skill:demo"]["count"] = 99
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        self.assertEqual(self.scan(out)["skill:demo"]["count"], 99)
        self.assertEqual(first["skill:demo"]["count"], 1)

    def test_changed_file_is_rescanned(self):
        f = self.transcript([tool_use("Skill", {"skill": "demo"})])
        out = self.root / "out"
        self.scan(out)
        with open(f, "a", encoding="utf-8") as fh:   # new content changes the file size
            fh.write(tool_use("Skill", {"skill": "demo"}) + "\n")
        self.assertEqual(self.scan(out)["skill:demo"]["count"], 2)

    def test_damaged_cache_entry_is_rebuilt(self):
        f = self.transcript([tool_use("Skill", {"skill": "demo"})])
        out = self.root / "out"
        self.scan(out)
        cache_path = out / ".ventry-cache.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cache["files"][str(f)]["counts"]["skill:demo"]["count"] = "lots"   # wrong type
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        self.assertEqual(self.scan(out)["skill:demo"]["count"], 1)

    def test_corrupt_cache_file_is_ignored(self):
        self.transcript([tool_use("Skill", {"skill": "demo"})])
        out = self.root / "out"
        write(out / ".ventry-cache.json", "{not json")
        self.assertEqual(self.scan(out)["skill:demo"]["count"], 1)


class TestUsageFor(unittest.TestCase):
    TOTALS = {
        "mcp:plugin_marketing_notion": {"count": 3, "last": "2026-11-18T10:00:00Z"},
        "mcp:plain": {"count": 2, "last": "2026-11-17T10:00:00Z"},
        "skill:marketing:seo-audit": {"count": 4, "last": "2026-11-16T09:00:00Z"},
        "cmd:hello": {"count": 1, "last": ""},
    }

    def test_plugin_mcp_matches_its_full_recorded_name(self):
        self.assertEqual(ventry.usage_for(self.TOTALS, "mcp", "notion", ("plugin_marketing_notion",)),
                         (3, "2026-11-18"))
        # Without the plugin-qualified name there is no loose suffix match.
        self.assertEqual(ventry.usage_for(self.TOTALS, "mcp", "notion")[0], 0)

    def test_similar_mcp_names_do_not_share_counts(self):
        totals = {"mcp:web_search": {"count": 1, "last": "2026-11-18T10:00:00Z"}}
        self.assertEqual(ventry.usage_for(totals, "mcp", "search")[0], 0)
        self.assertEqual(ventry.usage_for(totals, "mcp", "web_search")[0], 1)

    def test_exact_mcp_name(self):
        self.assertEqual(ventry.usage_for(self.TOTALS, "mcp", "plain"), (2, "2026-11-17"))

    def test_plugin_skill_matches_only_its_qualified_name(self):
        self.assertEqual(ventry.usage_for(self.TOTALS, "skill", "marketing:seo-audit"), (4, "2026-11-16"))
        self.assertEqual(ventry.usage_for(self.TOTALS, "skill", "seo-audit")[0], 0)

    def test_two_plugins_with_same_skill_name_do_not_share_counts(self):
        totals = {"skill:alpha:review": {"count": 1, "last": "2026-11-18T10:00:00Z"}}
        alpha = {"name": "review", "source": "plugin:alpha"}
        beta = {"name": "review", "source": "plugin:beta"}
        user = {"name": "review", "source": "user"}
        counts = [ventry.usage_for(totals, "skill", ventry.recorded_name("skill", r))[0]
                  for r in (alpha, beta, user)]
        self.assertEqual(counts, [1, 0, 0])

    def test_recorded_name_forms(self):
        self.assertEqual(ventry.recorded_name("mcp", {"name": "notion", "source": "plugin:marketing"}),
                         "plugin_marketing_notion")
        self.assertEqual(ventry.recorded_name("cmd", {"name": "ventry", "source": "plugin:ventry"}),
                         "ventry:ventry")
        self.assertEqual(ventry.recorded_name("mcp", {"name": "srv", "source": "user"}), "srv")

    def test_unused_item_is_zero(self):
        self.assertEqual(ventry.usage_for(self.TOTALS, "skill", "never"), (0, ""))

    def test_kinds_do_not_mix(self):
        self.assertEqual(ventry.usage_for(self.TOTALS, "cmd", "hello")[0], 1)
        self.assertEqual(ventry.usage_for(self.TOTALS, "skill", "hello")[0], 0)


# ------------------------------------------------------------------- output
def args_for(cwd, out, no_usage=False):
    return argparse.Namespace(cwd=str(cwd), out=str(out), no_usage=no_usage,
                              quiet=False, worker=False)


class TestBuild(FakeClaude):
    def setUp(self):
        super().setUp()
        self.out = self.root / "out"
        write(self.cdir / "skills" / "demo" / "SKILL.md", "---\ndescription: demo skill\n---\n")
        write(self.cdir / "skills" / "idle" / "SKILL.md", "---\ndescription: never used\n---\n")
        write(self.cdir / "projects" / "p1" / "t.jsonl", tool_use("Skill", {"skill": "demo"}) + "\n")

    def run_build(self, no_usage=False):
        env = {"CLAUDE_CONFIG_DIR": str(self.cdir)}
        with mock.patch.dict(os.environ, env):
            return ventry.build(args_for(self.cwd, self.out, no_usage))

    def test_writes_all_notes_with_frontmatter(self):
        self.run_build()
        for name in ("claude-code-inventory", "plugins", "hooks", "mcp-servers", "skills", "commands", "agents"):
            text = (self.out / f"{name}.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\ntype: ventry-"), name)
            self.assertIn("generated:", text)
        self.assertTrue((self.out / ".last-run").is_file())

    def test_index_has_wikilinks_and_counts(self):
        self.run_build()
        index = (self.out / "claude-code-inventory.md").read_text(encoding="utf-8")
        self.assertIn("| [[skills]] | 2 |", index)
        self.assertIn("[[plugins]]", index)

    def test_usage_counts_and_unused_list(self):
        _, _, n_unused = self.run_build()
        skills = (self.out / "skills.md").read_text(encoding="utf-8")
        self.assertRegex(skills, r"\| demo \| user \| demo skill \| 1 \| 2026-11-18 \|")
        self.assertRegex(skills, r"\| idle \| user \| never used \| 0 \|")
        index = (self.out / "claude-code-inventory.md").read_text(encoding="utf-8")
        self.assertIn("| Skill | idle | user |", index)
        self.assertNotIn("| Skill | demo | user |", index)
        self.assertEqual(n_unused, 1)

    def test_no_usage_leaves_counts_blank(self):
        _, _, n_unused = self.run_build(no_usage=True)
        index = (self.out / "claude-code-inventory.md").read_text(encoding="utf-8")
        self.assertIn("usage scan skipped", index)
        self.assertEqual(n_unused, 0)
        self.assertFalse((self.out / ".ventry-cache.json").exists())

    def test_refuses_to_overwrite_a_note_it_did_not_write(self):
        write(self.out / "claude-code-inventory.md", "# My own vault index\n")
        with self.assertRaises(RuntimeError):
            self.run_build()
        self.assertEqual((self.out / "claude-code-inventory.md").read_text(encoding="utf-8"), "# My own vault index\n")
        self.assertFalse((self.out / "skills.md").exists())  # nothing half-written

    def test_rerun_overwrites_its_own_notes(self):
        self.run_build()
        self.run_build()  # second run must not refuse its own notes
        self.assertTrue((self.out / "claude-code-inventory.md").is_file())

    def test_descriptions_for_plugins_and_mcp_servers(self):
        pdir = self.root / "mkt"
        write_json(pdir / ".claude-plugin" / "plugin.json", {"name": "mkt", "description": "Marketing helpers"})
        write_json(pdir / ".mcp.json", {"mcpServers": {"slack": {"type": "http", "url": "https://s"}}})
        write_json(self.cdir / "plugins" / "installed_plugins.json",
                   {"plugins": {"mkt@m": [{"scope": "user", "installPath": str(pdir)}]}})
        write_json(self.cdir / ".claude.json", {"mcpServers": {"github": {"command": "gh-mcp"}}})
        with open(self.cdir / "projects" / "p1" / "t.jsonl", "a", encoding="utf-8") as fh:
            fh.write(tool_use("mcp__github__search_code", {}) + "\n")
            fh.write(tool_use("mcp__github__search_code", {}) + "\n")
            fh.write(tool_use("mcp__github__get_issue", {}) + "\n")
        self.run_build()
        plugins = (self.out / "plugins.md").read_text(encoding="utf-8")
        self.assertIn("| mkt | Marketing helpers |", plugins)
        mcp = (self.out / "mcp-servers.md").read_text(encoding="utf-8")
        self.assertIn("| github | user | tools used: search_code, get_issue |", mcp)   # most used first
        self.assertIn("| slack | plugin:mkt | from plugin: Marketing helpers |", mcp)  # unused: plugin's text

    def test_per_project_writes_main_notes_and_a_project_snapshot(self):
        args = args_for(self.cwd, self.out)
        args.per_project = True
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cdir)}):
            out, folders, _ = ventry.build(args)
        slug = ventry.project_slug(self.cwd)
        snap = self.out / "projects" / slug
        self.assertEqual(folders, [self.out, snap])
        for folder in folders:
            for name in ("claude-code-inventory", "plugins", "hooks", "mcp-servers", "skills", "commands", "agents"):
                self.assertTrue((folder / f"{name}.md").is_file(), (folder, name))
            self.assertTrue((folder / ".last-run").is_file())
        # Main summary keeps plain links; the snapshot links inside its own folder.
        self.assertIn("| [[skills]] | 2 |", (self.out / "claude-code-inventory.md").read_text(encoding="utf-8"))
        self.assertIn(f"| [[projects/{slug}/skills\\|skills]] | 2 |",
                      (snap / "claude-code-inventory.md").read_text(encoding="utf-8"))
        # The cache is shared at the main folder, not duplicated per project.
        self.assertTrue((self.out / ".ventry-cache.json").is_file())
        self.assertFalse((snap / ".ventry-cache.json").exists())

    def test_without_per_project_no_snapshot_is_written(self):
        self.run_build()
        self.assertFalse((self.out / "projects").exists())

    def test_notes_use_lf_line_endings(self):
        self.run_build()
        self.assertNotIn(b"\r\n", (self.out / "claude-code-inventory.md").read_bytes())

    def test_empty_config_still_builds(self):
        empty = self.root / "empty" / ".claude"
        empty.mkdir(parents=True)
        out = self.root / "out2"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(empty)}):
            ventry.build(args_for(self.cwd, out))
        self.assertTrue((out / "claude-code-inventory.md").is_file())


class TestBrokenConfigEndToEnd(FakeClaude):
    """Mirrors tutorial step 13: broken files must not crash the export."""

    def test_step_13_fixture(self):
        write_json(self.cdir / "settings.json", {
            "hooks": {"SessionStart": [{"hooks": ["oops", {"type": "command", "command": "echo hi"}]}, "notastring"],
                      "Stop": "bad"},
            "enabledPlugins": ["x"]}, encoding="utf-8-sig")
        write_json(self.cdir / "plugins" / "installed_plugins.json",
                   {"plugins": {"a@b": [None, "str", {"installPath": 123}]}}, encoding="utf-8-sig")
        write(self.cdir / "skills" / "demo" / "SKILL.md", "---\ndescription: demo skill\n---\nbody",
              encoding="utf-8-sig")
        good = tool_use("Skill", {"skill": "demo"})
        write(self.cdir / "projects" / "p1" / "t.jsonl", "\n".join([
            good,
            '{"timestamp":"2026-11-18T10:00:01Z","message":{"content":[{"type":"tool_use","name":"Ski',
            '["tool_use"]',
            '{"message":{"content":5},"x":"tool_use"}',
            '{"message":{"content":[{"type":"tool_use","name":"Skill","input":"notadict"}]}}',
            good]) + "\n", encoding="utf-8-sig")
        out = self.root / "out"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cdir)}):
            ventry.build(args_for(self.cwd, out))
        skills = (out / "skills.md").read_text(encoding="utf-8")
        self.assertRegex(skills, r"\| demo \| user \| demo skill \| 2 \|")
        hooks = (out / "hooks.md").read_text(encoding="utf-8")
        self.assertIn("`echo hi`", hooks)
        self.assertNotIn("oops", hooks)
        plugins = (out / "plugins.md").read_text(encoding="utf-8")
        self.assertIn("| a |  | b |", plugins)   # name, empty description, marketplace


# --------------------------------------------------------------- throttling
class TestThrottle(FakeClaude):
    def test_no_last_run_means_do_not_skip(self):
        self.assertFalse(ventry.should_skip(self.root))

    def test_recent_run_is_skipped(self):
        write(self.root / ".last-run", str(int(time.time())))
        self.assertTrue(ventry.should_skip(self.root))

    def test_old_run_is_not_skipped(self):
        write(self.root / ".last-run", str(int(time.time()) - ventry.THROTTLE_SECONDS - 60))
        self.assertFalse(ventry.should_skip(self.root))

    def test_project_slug_is_stable_and_keeps_same_named_projects_apart(self):
        a = self.root / "one" / "app"
        b = self.root / "two" / "app"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        self.assertEqual(ventry.project_slug(a), ventry.project_slug(a))
        self.assertNotEqual(ventry.project_slug(a), ventry.project_slug(b))
        self.assertTrue(ventry.project_slug(a).startswith("app-"))
        self.assertTrue(ventry.project_slug(self.root / "My Project!").startswith("My-Project-"))

    def test_throttle_is_per_project_with_per_project(self):
        out = self.root / "out"
        args = argparse.Namespace(out=str(out), cwd=str(self.cwd), per_project=True)
        self.assertEqual(ventry.throttle_dir(args), out / "projects" / ventry.project_slug(self.cwd))
        args.per_project = False
        self.assertEqual(ventry.throttle_dir(args), out)

    def test_garbage_last_run_is_not_skipped(self):
        write(self.root / ".last-run", "not a number")
        self.assertFalse(ventry.should_skip(self.root))


class TestQuietMode(FakeClaude):
    """Runs the real script as a subprocess, the way the SessionStart hook does."""

    def run_quiet(self, out):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.cdir), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--quiet", "--no-usage", "--out", str(out), "--cwd", str(self.cwd)],
            capture_output=True, text=True, env=env, timeout=30)

    def wait_for(self, path, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            if path.is_file():
                return True
            time.sleep(0.2)
        return False

    def test_quiet_prints_nothing_then_worker_writes_notes_then_throttles(self):
        out = self.root / "out dir"  # a space in the path, as in tutorial step 12
        first = self.run_quiet(out)
        self.assertEqual((first.returncode, first.stdout, first.stderr), (0, "", ""))
        # The background worker writes .last-run last, so wait for it.
        self.assertTrue(self.wait_for(out / ".last-run"), "worker did not finish")
        self.assertTrue((out / "claude-code-inventory.md").is_file())
        stamp = (out / ".last-run").read_text(encoding="utf-8")
        # Straight after, the 6 hour throttle makes a second run do nothing.
        time.sleep(1.2)
        second = self.run_quiet(out)
        self.assertEqual((second.returncode, second.stdout, second.stderr), (0, "", ""))
        time.sleep(2)
        self.assertEqual((out / ".last-run").read_text(encoding="utf-8"), stamp)


if __name__ == "__main__":
    unittest.main()
