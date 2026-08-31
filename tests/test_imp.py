"""Tests for `imp`, the tmux wrapper.

`imp` used to be described as "a launcher with nothing at stake; shellcheck
covers it". It was not. It re-implemented argparse's grammar in bash to work
out which console to open, and got it wrong for every spelling except the one
the author happened to type: -sNAME, --sprite=NAME and --spr NAME all reached
imp-proxy intact and funded one machine while imp opened a console on
another. That is what these tests are for. The wrapper now asks imp-proxy
what the arguments meant (--print-console) instead of guessing, and the first
class of test below is that the guess is gone for good.

The rest is tmux wiring, which is only honestly testable against a real tmux:
the window layout, `claude` starting no earlier than the proxy's readiness,
and the two directions a session can end. Those run against a private tmux
server and are skipped where tmux is missing.

Stdlib only, matching the tools. Run: python3 -m unittest discover tests
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMP = os.path.join(ROOT, "imp")
IMP_PROXY = os.path.join(ROOT, "imp-proxy")
TMUX = shutil.which("tmux")


def write_exec(path, body):
    with open(path, "w") as f:
        f.write(textwrap.dedent(body).lstrip())
    os.chmod(path, 0o755)


class Harness(object):
    """A throwaway checkout of `imp` with everything around it stubbed.

    The stub imp-proxy hands --print-console straight to the real one, so the
    argument contract under test is the real contract and not a second
    implementation of it that could drift the same way the bash one did.
    """

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="imp-test.")
        # A tmux socket is a unix socket, and those cap out around 104 bytes
        # of path -- shorter than the temp directories CI hands out. Hence a
        # deliberately short second directory just for the socket.
        self.sockdir = tempfile.mkdtemp(prefix="impt.", dir="/tmp")
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        shutil.copy(IMP, os.path.join(self.dir, "imp"))

        write_exec(os.path.join(self.dir, "imp-proxy"), """
            #!/bin/bash
            if [ "${1:-}" = --print-console ]; then
              shift; exec %s --print-console "$@"
            fi
            ready=""; prev=""
            for a in "$@"; do
              [ "$prev" = --ready-file ] && ready="$a"; prev="$a"
            done
            for a in "$@"; do
              [ "$a" = --clear ] && { echo "PROXY CLEARED"; exit 0; }
            done
            echo "PROXY UP $*"
            trap 'echo PROXY DOWN; [ -n "$ready" ] && rm -f "$ready"; exit 0' \\
                 INT TERM HUP
            # The delay is the point: it is the window in which `claude` must
            # not have started yet.
            sleep "${IMP_TEST_READY_DELAY:-1}"
            echo "PROXY READY"
            [ -n "$ready" ] && printf 'ready\\n' > "$ready"
            # The no-tmux path runs the proxy in the foreground, so that test
            # needs one that finishes.
            [ -n "${IMP_TEST_PROXY_EXITS:-}" ] && exit 0
            while :; do sleep 0.2; done
        """ % IMP_PROXY)

        for name in ("sprite", "ssh"):
            write_exec(os.path.join(self.bin, name), """
                #!/bin/bash
                echo "CONSOLE %s $*"
                exec bash --norc --noprofile
            """ % name)
        write_exec(os.path.join(self.bin, "claude"), """
            #!/bin/bash
            echo CLAUDE RUNNING
            while :; do sleep 0.2; done
        """)

        self.env = dict(os.environ)
        self.env["PATH"] = self.bin + os.pathsep + self.env.get("PATH", "")
        self.env["TMUX_TMPDIR"] = self.sockdir
        # imp puts its readiness file under TMPDIR. A private one makes
        # "did it clean up after itself" a question about this run only.
        self.scratch = os.path.join(self.dir, "tmp")
        os.mkdir(self.scratch)
        self.env["TMPDIR"] = self.scratch
        self.env.pop("TMUX", None)
        self.env["IMP_TEST_READY_DELAY"] = "1"

    # -- tmux ------------------------------------------------------------
    def tmux(self, *args):
        return subprocess.run([TMUX] + list(args), env=self.env, cwd=self.dir,
                              capture_output=True, text=True)

    def prime(self):
        """Start the server ourselves, on our terms.

        Two reasons this cannot be left to `imp`. A tmux pane's environment
        comes from the server, not from whoever typed the command, so without
        seeding it the stubs are invisible and the real `sprite` runs
        instead. And `-f /dev/null` keeps the developer's ~/.tmux.conf out of
        it -- base-index alone would otherwise renumber every window these
        tests look at, differently on different machines.
        """
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "primer",
                  "sleep 300")
        self.tmux("set-environment", "-g", "PATH", self.env["PATH"])
        # tmux runs a pane's command through $SHELL, and a zsh that sets PATH
        # in ~/.zshenv -- a common thing to do, since it is the one file
        # always read -- puts the developer's real `sprite` back in front of
        # the stub. /bin/sh reads no such file.
        self.tmux("set-option", "-g", "default-shell", "/bin/sh")

    def windows(self, session="imp-foo"):
        r = self.tmux("list-windows", "-t", session, "-F", "#{window_name}")
        return r.stdout.split() if r.returncode == 0 else []

    def window_ids(self, name, session="imp-foo"):
        """Window ids by name. Ids because indices depend on base-index and
        on which window closed last; a name is what imp actually promises."""
        r = self.tmux("list-windows", "-t", session, "-F",
                      "#{window_id}\t#{window_name}")
        if r.returncode != 0:
            return []
        return [l.split("\t")[0] for l in r.stdout.splitlines()
                if l.endswith("\t" + name)]

    def consoles(self, label="foo", session="imp-foo"):
        return self.window_ids("claude:" + label, session)

    def proxy_window(self, label="foo", session="imp-foo"):
        ids = self.window_ids("imp:" + label, session)
        return ids[0] if ids else None

    def pane_text(self, target):
        r = self.tmux("capture-pane", "-p", "-t", target)
        return r.stdout if r.returncode == 0 else ""

    def start_commands(self, session="imp-foo"):
        """(window name, command) per pane.

        tmux reports pane_start_command wrapped in double quotes; they are
        tmux's, not part of the command, so they come off here.
        """
        r = self.tmux("list-panes", "-s", "-t", session, "-F",
                      "#{window_name}\t#{pane_start_command}")
        out = []
        for line in r.stdout.splitlines():
            if "\t" not in line:
                continue
            name, cmd = line.split("\t", 1)
            cmd = cmd.strip().strip('"')
            # imp prefixes every pane command with `exec` so the pane's
            # process is the real one; that is plumbing, not part of what the
            # arguments asked for, so it comes off here.
            if cmd.startswith("exec "):
                cmd = cmd[len("exec "):]
            out.append((name, cmd))
        return out

    # -- imp -------------------------------------------------------------
    def imp(self, *args, **kw):
        """Run imp. Detaching fails without a tty, which is expected and
        harmless -- the session is built before the attach is attempted."""
        return subprocess.run([os.path.join(self.dir, "imp")] + list(args),
                              env=self.env, cwd=kw.get("cwd", self.dir),
                              capture_output=True, text=True,
                              timeout=kw.get("timeout", 30))

    def wait(self, pred, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.1)
        return False

    def close(self):
        if TMUX:
            self.tmux("kill-server")
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.sockdir, ignore_errors=True)


# --------------------------------------------------------------------------
# what the arguments meant -- the bug that started all this. No tmux needed.
# --------------------------------------------------------------------------

class TestArgumentSpellings(unittest.TestCase):
    """Every spelling argparse accepts has to reach the same console.

    Run against imp-proxy directly: this is the contract `imp` consumes, and
    a break here is a break in the wrapper whether or not tmux is installed.
    """

    def console(self, *args):
        r = subprocess.run([IMP_PROXY, "--print-console"] + list(args),
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        return out

    def test_every_sprite_spelling_opens_the_same_console(self):
        want = "sprite console -s foo"
        for argv in (["-s", "foo"], ["-sfoo"], ["--sprite=foo"],
                     ["--sprite", "foo"], ["--spr", "foo"]):
            got = self.console(*argv)
            self.assertEqual(got["console"], want, argv)
            self.assertEqual(got["label"], "foo", argv)

    def test_every_host_spelling_opens_an_ssh_session(self):
        want = "ssh -t box"
        for argv in (["-H", "box"], ["-Hbox"], ["--host=box"],
                     ["--host", "box"]):
            got = self.console(*argv)
            self.assertEqual(got["console"], want, argv)
            self.assertEqual(got["label"], "box", argv)

    def test_a_host_never_resolves_to_a_sprite_console(self):
        # The original failure, stated as a test: --host=box funded `box`
        # over ssh and opened a console on whatever .sprite said.
        self.assertNotIn("sprite", self.console("--host=box")["console"])

    def test_clear_is_not_a_session(self):
        self.assertEqual(self.console("--clear", "-s", "foo")["mode"],
                         "oneshot")
        self.assertEqual(self.console("-s", "foo")["mode"], "session")

    def test_console_count_defaults_to_three_and_is_settable(self):
        self.assertEqual(self.console("-s", "foo")["consoles"], "3")
        self.assertEqual(self.console("-s", "foo", "-n", "5")["consoles"], "5")

    def test_a_name_needing_quotes_survives_the_round_trip(self):
        got = self.console("-s", "we ird")
        self.assertEqual(got["console"], "sprite console -s 'we ird'")
        self.assertEqual(got["label"], "we-ird")   # tmux window name

    def test_sprite_and_host_together_is_refused_before_anything_happens(self):
        r = subprocess.run([IMP_PROXY, "--print-console", "-s", "a", "-H", "b"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_a_sprite_file_without_a_trailing_newline_still_works(self):
        # `read -r sprite < .sprite` returned 1 at EOF-without-newline, and
        # under `set -e` that ended imp with no output and status 1.
        for content in ("boxy", "boxy\n", ""):
            d = tempfile.mkdtemp(prefix="imp-dotsprite.")
            try:
                with open(os.path.join(d, ".sprite"), "w") as f:
                    f.write(content)
                r = subprocess.run([IMP_PROXY, "--print-console"],
                                   capture_output=True, text=True, cwd=d)
                self.assertEqual(r.returncode, 0, (content, r.stderr))
                out = dict(l.split("=", 1) for l in r.stdout.splitlines())
                self.assertEqual(out["console"], "sprite console")
                self.assertEqual(out["label"], "boxy" if content.strip()
                                 else "sprite")
            finally:
                shutil.rmtree(d, ignore_errors=True)


# Everything `imp`, the stub proxy and the real imp-proxy reach for. The
# no-tmux path needs a PATH that has all of it and no tmux, and tmux usually
# lives in the same directory as half of this -- so the directory is built
# out of symlinks rather than by subtracting from PATH.
NEEDED = ("bash", "sh", "env", "python3", "dirname", "sed", "grep", "mktemp",
          "rm", "sleep", "cat", "seq")


class TestHelpAndErrors(unittest.TestCase):
    """imp's own usage, imp-proxy's usage, and imp-proxy's errors."""

    def imp(self, *args):
        return subprocess.run([IMP] + list(args), capture_output=True,
                              text=True, cwd=ROOT, timeout=20)

    def test_bare_help_is_imps_own(self):
        r = self.imp("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage: imp ", r.stderr)
        self.assertIn("Ctrl-B C-n", r.stderr)

    def test_help_alongside_arguments_is_the_proxys(self):
        # argparse prints and exits before --print-console is looked at, so
        # what comes back is not a console spec. It gets handed on as-is
        # rather than turned into an error about the protocol.
        r = self.imp("-s", "foo", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage: imp-proxy", r.stdout)

    def test_an_unknown_option_is_the_proxys_error_and_status(self):
        r = self.imp("-s", "foo", "--nonsense")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--nonsense", r.stderr)

    def test_a_sprite_and_a_host_together_stops_before_any_window(self):
        r = self.imp("-s", "a", "-H", "b")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pick one", r.stderr)


class TestNoTmux(unittest.TestCase):
    """Without tmux, imp is exactly imp-proxy."""

    def test_it_runs_the_proxy_and_says_where_to_get_a_console(self):
        h = Harness()
        try:
            shim = os.path.join(h.dir, "notmux")
            os.mkdir(shim)
            for name in NEEDED:
                found = shutil.which(name)
                if found is None:
                    self.skipTest("%s is not on PATH" % name)
                os.symlink(found, os.path.join(shim, name))
            h.env["PATH"] = h.bin + os.pathsep + shim
            h.env["IMP_TEST_PROXY_EXITS"] = "1"
            self.assertIsNone(shutil.which("tmux", path=h.env["PATH"]))

            r = subprocess.run([os.path.join(h.dir, "imp"), "-s", "foo"],
                               env=h.env, cwd=h.dir, capture_output=True,
                               text=True, timeout=20)
            self.assertIn("no tmux", r.stderr)
            self.assertIn("sprite console -s foo", r.stderr)
            self.assertIn("PROXY UP", r.stdout)
        finally:
            h.close()


# --------------------------------------------------------------------------
# the tmux wiring
# --------------------------------------------------------------------------

@unittest.skipUnless(TMUX, "tmux is not installed")
class TmuxCase(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.prime()

    def tearDown(self):
        self.h.close()


class TestLayout(TmuxCase):
    def test_proxy_window_then_three_consoles(self):
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        self.assertEqual(self.h.windows(),
                         ["imp:foo", "claude:foo", "claude:foo", "claude:foo"])

    def test_console_count_is_settable(self):
        self.h.imp("-s", "foo", "-n", "5")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 6))
        self.assertEqual(self.h.windows().count("claude:foo"), 5)

    def test_the_consoles_are_what_the_arguments_asked_for(self):
        self.h.imp("--sprite=foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        for name, cmd in self.h.start_commands():
            if name == "claude:foo":
                self.assertEqual(cmd, "sprite console -s foo")

    def test_a_host_gets_ssh_consoles_not_sprite_ones(self):
        self.h.imp("--host=box")
        self.assertTrue(self.h.wait(
            lambda: len(self.h.windows("imp-box")) == 4))
        cmds = [c for n, c in self.h.start_commands("imp-box")
                if n == "claude:box"]
        self.assertEqual(cmds, ["ssh -t box"] * 3)

    def test_it_lands_on_a_console_not_on_the_proxy(self):
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        r = self.h.tmux("display", "-p", "-t", "imp-foo", "#{window_name}")
        self.assertEqual(r.stdout.strip(), "claude:foo")

    def test_clear_opens_no_windows_at_all(self):
        r = self.h.imp("--clear", "-s", "foo")
        self.assertIn("PROXY CLEARED", r.stdout)
        self.assertEqual(self.h.windows(), [])


class TestReadyGate(TmuxCase):
    def test_claude_does_not_start_before_the_proxy_is_ready(self):
        self.h.env["IMP_TEST_READY_DELAY"] = "3"
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        # The console shells are up well inside the proxy's 3s delay; if the
        # gate were missing, `claude` would already be in them.
        first = self.h.consoles()[0]
        self.assertTrue(self.h.wait(
            lambda: "CONSOLE sprite" in self.h.pane_text(first)))
        self.assertNotIn("CLAUDE RUNNING", self.h.pane_text(first))

    def test_claude_starts_in_every_console_once_it_is(self):
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        for wid in self.h.consoles():
            self.assertTrue(
                self.h.wait(lambda w=wid: "CLAUDE RUNNING"
                            in self.h.pane_text(w)),
                "no claude in window %s" % wid)

    def test_the_readiness_file_is_cleaned_up(self):
        self.h.imp("-s", "foo")
        first = self.h.consoles()[0]
        self.assertTrue(self.h.wait(
            lambda: "CLAUDE RUNNING" in self.h.pane_text(first)))
        # The waiter removes its directory once it has typed `claude`
        # everywhere; nothing of imp's should outlive the session.
        self.assertTrue(
            self.h.wait(lambda: os.listdir(self.h.scratch) == []),
            "left behind: %r" % (os.listdir(self.h.scratch),))


class TestEndingASession(TmuxCase):
    def setUp(self):
        TmuxCase.setUp(self)
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))

    def test_closing_the_last_console_takes_the_proxy_with_it(self):
        for wid in self.h.consoles():
            self.h.tmux("kill-window", "-t", wid)
            time.sleep(0.3)
        self.assertTrue(self.h.wait(lambda: self.h.windows() == []),
                        "session outlived its last console: %r"
                        % (self.h.windows(),))

    def test_closing_one_console_leaves_the_others_and_the_proxy(self):
        self.h.tmux("kill-window", "-t", self.h.consoles()[0])
        time.sleep(0.6)
        self.assertEqual(sorted(self.h.windows()),
                         ["claude:foo", "claude:foo", "imp:foo"])

    def test_the_proxy_exiting_leaves_the_consoles_running(self):
        # Ctrl-C in the proxy window is a clean exit: revoke, keep working.
        self.h.tmux("send-keys", "-t", self.h.proxy_window(), "C-c")
        self.assertTrue(self.h.wait(
            lambda: "imp:foo" not in self.h.windows()))
        self.assertEqual(self.h.windows(), ["claude:foo"] * 3)


class TestKeyBindings(TmuxCase):
    def test_it_binds_a_key_that_skips_the_proxy_window(self):
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        keys = self.h.tmux("list-keys", "-T", "prefix").stdout
        self.assertIn("C-n", keys)
        self.assertIn("C-p", keys)
        self.assertIn("imp:*", keys)

    def test_stepping_never_lands_on_the_proxy(self):
        """The bound command chain, exercised directly.

        A key binding only fires for a real client's keystrokes, so what is
        checked here is the pair of commands the binding runs.
        """
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        self.h.tmux("select-window", "-t", self.h.proxy_window())
        seen = []
        for _ in range(6):
            self.h.tmux("select-window", "-t", "imp-foo:+")
            self.h.tmux("if-shell", "-F", "-t", "imp-foo",
                        "#{m:imp:*,#{window_name}}",
                        "select-window -t imp-foo:+")
            seen.append(self.h.tmux("display", "-p", "-t", "imp-foo",
                                    "#{window_name}").stdout.strip())
        self.assertEqual(set(seen), {"claude:foo"},
                         "stepped onto the proxy window: %r" % seen)


if __name__ == "__main__":
    unittest.main()
