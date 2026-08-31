"""Tests for imp-auth.

imp-auth is the tool that deliberately leaves a real, long-lived credential at
rest on the sprite, so its failure modes matter more per line than imp's. It is
bash, and it embeds two python programs that shellcheck cannot see inside --
both are extracted and exercised here against a temp HOME.

Stdlib only. Run: python3 -m unittest discover -s tests
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMP_AUTH = os.path.join(ROOT, "imp-auth")
with open(IMP_AUTH) as _f:
    SOURCE = _f.read()


def extract_heredoc(tag):
    """The merge program, shipped as a quoted heredoc."""
    m = re.search(r"<<'%s'\n(.*?)\n%s\n" % (tag, tag), SOURCE, re.S)
    if not m:
        raise AssertionError("heredoc %s not found in imp-auth" % tag)
    return m.group(1)


def extract_inline_python(marker):
    """A `python3 -c '...'` program, identified by a string it contains."""
    for m in re.finditer(r"python3 -c '\n(.*?)'", SOURCE, re.S):
        if marker in m.group(1):
            return m.group(1)
    raise AssertionError("inline python containing %r not found" % marker)


MERGE_SRC = extract_heredoc("PY_EOF")
REMOVE_SRC = extract_inline_python("token removed")
STATUS_SRC = extract_inline_python("no token installed")


class RemoteHomeCase(unittest.TestCase):
    """These programs run on the sprite; a temp HOME stands in for it."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(self.claude)
        self.settings = os.path.join(self.claude, "settings.json")
        self.stage = os.path.join(self.claude, ".max-token.stage")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_src(self, src):
        return subprocess.run([sys.executable, "-u", "-c", src],
                              capture_output=True, text=True,
                              env=dict(os.environ, HOME=self.home))

    def write_settings(self, obj):
        with open(self.settings, "w") as f:
            json.dump(obj, f)

    def read_settings(self):
        with open(self.settings) as f:
            return json.load(f)

    def stage_token(self, token="sk-ant-oat01-TESTTOKEN"):
        with open(self.stage, "w") as f:
            f.write(token + "\n")
        return token

    def leftovers(self):
        return sorted(f for f in os.listdir(self.claude) if f != "settings.json")


class TestMergeProgram(RemoteHomeCase):
    def test_installs_the_token(self):
        tok = self.stage_token()
        r = self.run_src(MERGE_SRC)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.read_settings()["env"]["CLAUDE_CODE_OAUTH_TOKEN"], tok)

    def test_consumes_the_staged_token(self):
        """The staged copy is the one that outlives a crash, so it must go."""
        self.stage_token()
        self.run_src(MERGE_SRC)
        self.assertFalse(os.path.exists(self.stage))

    def test_settings_are_not_world_readable(self):
        self.stage_token()
        self.run_src(MERGE_SRC)
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o600)

    def test_no_temp_file_is_left_behind(self):
        self.stage_token()
        self.run_src(MERGE_SRC)
        self.assertEqual(self.leftovers(), [])

    def test_unrelated_settings_survive(self):
        self.write_settings({"theme": "dark", "env": {"FOO": "bar"}})
        tok = self.stage_token()
        self.run_src(MERGE_SRC)
        data = self.read_settings()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"]["FOO"], "bar")
        self.assertEqual(data["env"]["CLAUDE_CODE_OAUTH_TOKEN"], tok)

    def test_refuses_to_overwrite_invalid_json(self):
        with open(self.settings, "w") as f:
            f.write("{not json")
        self.stage_token()
        r = self.run_src(MERGE_SRC)
        self.assertNotEqual(r.returncode, 0)
        with open(self.settings) as f:
            self.assertEqual(f.read(), "{not json")

    def test_empty_staged_token_is_refused(self):
        with open(self.stage, "w") as f:
            f.write("   \n")
        r = self.run_src(MERGE_SRC)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.settings))

    def test_works_with_no_existing_settings(self):
        tok = self.stage_token()
        r = self.run_src(MERGE_SRC)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.read_settings()["env"]["CLAUDE_CODE_OAUTH_TOKEN"], tok)


class TestRemoveProgram(RemoteHomeCase):
    def test_removes_the_token(self):
        self.write_settings({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}})
        r = self.run_src(REMOVE_SRC)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("token removed", r.stdout)
        self.assertNotIn("env", self.read_settings())

    def test_keeps_unrelated_settings(self):
        self.write_settings({"theme": "dark",
                             "env": {"FOO": "bar",
                                     "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}})
        self.run_src(REMOVE_SRC)
        data = self.read_settings()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"], {"FOO": "bar"})

    def test_reports_when_there_is_nothing_to_remove(self):
        self.write_settings({"theme": "dark"})
        r = self.run_src(REMOVE_SRC)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no token present", r.stdout)
        self.assertEqual(self.read_settings(), {"theme": "dark"})

    def test_leaves_no_temp_file(self):
        self.write_settings({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}})
        self.run_src(REMOVE_SRC)
        self.assertEqual(self.leftovers(), [])

    def test_settings_are_replaced_not_rewritten_in_place(self):
        """Atomicity comes from rename, and rename changes the inode.

        A truncate-in-place rewrite keeps the same inode and is observable
        empty in between; os.replace is never observable at all.
        """
        self.write_settings({"theme": "dark",
                             "env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}})
        before = os.stat(self.settings).st_ino
        r = self.run_src(REMOVE_SRC)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(before, os.stat(self.settings).st_ino,
                            "settings.json was rewritten in place")
        self.assertEqual(self.read_settings(), {"theme": "dark"})


class TestStatusProgram(RemoteHomeCase):
    def test_reports_an_installed_token(self):
        self.write_settings({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abc"}})
        os.chmod(self.settings, 0o600)
        r = self.run_src(STATUS_SRC)
        self.assertIn("token installed", r.stdout)
        self.assertIn("600", r.stdout)

    def test_reports_no_token(self):
        self.write_settings({"theme": "dark"})
        r = self.run_src(STATUS_SRC)
        self.assertIn("no token installed", r.stdout)

    def test_survives_a_missing_settings_file(self):
        r = self.run_src(STATUS_SRC)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no readable settings.json", r.stdout)


class TestEmbeddedProgramsCompile(unittest.TestCase):
    """shellcheck cannot see inside these, and a syntax error would only
    surface against a live sprite."""

    def test_all_three_compile(self):
        for name, src in (("merge", MERGE_SRC), ("remove", REMOVE_SRC),
                          ("status", STATUS_SRC)):
            try:
                compile(src, "<%s>" % name, "exec")
            except SyntaxError as e:
                self.fail("%s program does not compile: %s" % (name, e))


class TestErrexitDiscipline(unittest.TestCase):
    """for_each calls these as `"$fn" "$t" || rc=1`, and bash disables errexit
    for the whole of a function whose status is tested. Every fallible command
    in push_one must therefore be checked by hand."""

    def test_bash_really_does_suppress_errexit_there(self):
        # If this ever stops being true, the hand-checking below is redundant
        # rather than load-bearing -- and we should know.
        script = ('set -euo pipefail\n'
                  'inner() { false; echo REACHED; }\n'
                  'outer() { inner || rc=1; }\n'
                  'outer\n')
        r = subprocess.run(["/bin/bash", "-c", script],
                           capture_output=True, text=True)
        self.assertIn("REACHED", r.stdout)

    def push_one_body(self):
        m = re.search(r"\npush_one\(\) \{\n(.*?)\n\}\n", SOURCE, re.S)
        self.assertIsNotNone(m, "push_one not found")
        return m.group(1)

    def test_every_sprite_call_in_push_one_is_checked(self):
        body = self.push_one_body()
        unchecked = []
        lines = body.split("\n")
        for i, line in enumerate(lines):
            st = line.strip()
            if not st.startswith(("sprite ", "security ", "chmod ", "mktemp")):
                continue
            # Checked if this line or the next carries || / { ... }, or the
            # line is a continuation into one.
            window = " ".join(lines[i:i + 2])
            if "||" in window or st.endswith("\\"):
                continue
            unchecked.append(st)
        self.assertEqual(unchecked, [],
                         "unchecked fallible commands in push_one: %r" % unchecked)

    def test_signal_traps_exit(self):
        """A trap that only cleans up lets a signal delete the temp files and
        then carry on running without them."""
        body = self.push_one_body()
        self.assertIn("trap 'exit 130' INT", body)
        self.assertIn("trap 'exit 143' TERM", body)


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([IMP_AUTH] + list(args),
                              capture_output=True, text=True)

    def test_help_exits_zero(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("imp-auth <command>", r.stdout + r.stderr)

    def test_no_args_shows_usage(self):
        r = self.run_cli()
        self.assertIn("imp-auth <command>", r.stdout + r.stderr)

    def test_unknown_command_is_an_error(self):
        r = self.run_cli("wat")
        self.assertNotEqual(r.returncode, 0)

    def test_parses_under_system_bash(self):
        """macOS ships /bin/bash 3.2.57; that is what has to parse this."""
        if not os.path.exists("/bin/bash"):
            self.skipTest("no /bin/bash")
        r = subprocess.run(["/bin/bash", "-n", IMP_AUTH],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
