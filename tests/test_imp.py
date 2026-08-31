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
import re
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

        # `sprite exec` is what the meter samples through, and the two
        # /proc/stat lines below are a machine that was busy half the time
        # between them: 200 ticks passed, 100 of them idle.
        self.calls = os.path.join(self.dir, "calls")
        for name in ("sprite", "ssh"):
            write_exec(os.path.join(self.bin, name), """
                #!/bin/bash
                echo "%s $*" >> %s
                if [ "${1:-}" = exec ] || [ "${1:-}" = -o ]; then
                  echo "cpu  100 0 100 800 0 0 0 0 0 0"
                  echo "cpu  150 0 150 900 0 0 0 0 0 0"
                  echo "${IMP_TEST_LOADAVG:-0.10} 0.10 0.10 1/200 999"
                  echo "8"
                  exit 0
                fi
                echo "CONSOLE %s $*"
                exec bash --norc --noprofile
            """ % (name, self.calls, name))
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
        # The meter's cache lives under XDG_STATE_HOME, beside what
        # imp-proxy writes down; a test run must not read or write the real
        # one.
        self.env["XDG_STATE_HOME"] = os.path.join(self.dir, "state")
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

    def console_panes(self, label="foo", session="imp-foo"):
        """Console panes in session order -- which reads the same tiled or
        not, and is how "the same three consoles, still in order" is said."""
        r = self.tmux("list-panes", "-s", "-t", session, "-F",
                      "#{pane_id}\t#{window_name}")
        if r.returncode != 0:
            return []
        return [l.split("\t")[0] for l in r.stdout.splitlines()
                if l.endswith("\tclaude:" + label)]

    def panes(self, target):
        r = self.tmux("list-panes", "-t", target, "-F", "#{pane_id}")
        return r.stdout.split() if r.returncode == 0 else []

    def proxy_window(self, label="foo", session="imp-foo"):
        ids = self.window_ids("imp:" + label, session)
        return ids[0] if ids else None

    def revoke_proxy(self, label="foo", session="imp-foo"):
        """Ctrl-C in the proxy window, the way a session loses it. Empty
        string if the window went, and why it did not otherwise.

        Waited for and retried, because a keystroke is not a function call.
        The proxy installs its INT handler a moment after it starts, and a
        C-c that lands before that kills the pane by signal instead -- which
        `remain-on-exit failed` rightly keeps on screen, leaving a window
        that never closes.

        The reason it gives back is the pane's own state, because a window
        that outlives five Ctrl-Cs has already stopped being about the
        keystroke, and "False is not true" from a CI runner an ocean away is
        not enough to say what it is about instead.
        """
        win = self.proxy_window(label, session)
        if not win:
            return "no proxy window in %s: %r" % (session, self.windows(session))
        if not self.wait(lambda: "PROXY READY" in self.pane_text(win)):
            return "the proxy never reported ready: %r" % self.pane_state(win)
        for _ in range(5):
            self.tmux("send-keys", "-t", win, "C-c")
            if self.wait(lambda: ("imp:" + label) not in self.windows(session),
                         3.0):
                return ""
            # Or the proxy stopped and tmux kept the corpse. `remain-on-exit
            # failed` keeps a pane it cannot prove succeeded, and on tmux 3.4
            # a pane can arrive dead with neither an exit status nor a signal
            # recorded -- seen on ubuntu runners, where the pane read
            # `dead=1 status= sig=` after printing PROXY DOWN and exiting 0.
            # The proxy is stopped either way, which is what revoking means;
            # the window staying is that tmux's cosmetics, not imp's doing.
            if "PROXY DOWN" in self.pane_text(win) and self.pane_dead(win):
                return ""
        return ("the proxy window outlived five Ctrl-Cs: windows=%r pane=%r "
                "text=%r" % (self.windows(session), self.pane_state(win),
                             self.pane_text(win).strip().splitlines()[-3:]))

    def pane_dead(self, target):
        r = self.tmux("list-panes", "-t", target, "-F", "#{pane_dead}")
        return r.stdout.strip().startswith("1")

    def pane_state(self, target):
        """What tmux believes about a pane -- alive or dead, how it died, and
        whether the option that decides if its window stays actually took."""
        r = self.tmux("list-panes", "-t", target, "-F",
                      "dead=#{pane_dead} status=#{pane_dead_status} "
                      "sig=#{pane_dead_signal} pid=#{pane_pid} "
                      "cmd=#{pane_current_command} "
                      "remain=#{?pane_dead,-,#{window_visible_layout}} "
                      "opt=#{remain-on-exit}")
        return r.stdout.strip() or ("no pane: %r" % r.stderr.strip())

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

    def test_reattach_is_reported_in_the_console_spec(self):
        # `imp` does not parse argv; every question it asks about the
        # arguments is answered here, and "is this a session to build or one
        # to rejoin" is one of them.
        self.assertEqual(self.console("-s", "foo")["reattach"], "0")
        self.assertEqual(self.console("-s", "foo", "--reattach")["reattach"],
                         "1")

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
        why = self.h.revoke_proxy()
        if why:
            self.fail(why)
        # The consoles, all three, whatever became of the proxy's window.
        self.assertEqual([w for w in self.h.windows() if w != "imp:foo"],
                         ["claude:foo"] * 3)


class TestReattach(TmuxCase):
    """Putting the proxy back beside consoles that outlived it.

    The expensive half of a session is the consoles -- three claudes with
    three conversations in them -- so what is checked here is mostly what
    --reattach does *not* do: it opens no console, types no `claude`, and
    leaves the ones that are running exactly where they were.
    """

    def revoke(self):
        """Leave the session without a proxy window.

        Killed rather than Ctrl-C'd. What these tests are about is what
        --reattach does with a session that has lost its proxy, and reaching
        that state through a keystroke made seven of them depend on the
        timing of one -- see TestEndingASession, where the keystroke is the
        subject and belongs. Killing the window is the same loss from the
        session's side, and it exercises the reaper on the way: the hook
        fires, counts three consoles, and leaves them alone.
        """
        self.h.tmux("kill-window", "-t", self.h.proxy_window())
        self.assertTrue(self.h.wait(lambda: "imp:foo" not in self.h.windows()),
                        "the proxy window survived kill-window: %r"
                        % (self.h.windows(),))

    def setUp(self):
        TmuxCase.setUp(self)
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        first = self.h.consoles()[0]
        self.assertTrue(self.h.wait(
            lambda: "CLAUDE RUNNING" in self.h.pane_text(first)))
        self.before = self.h.consoles()

    def test_the_proxy_comes_back_in_front_of_the_consoles_it_left(self):
        self.revoke()
        self.h.imp("-s", "foo", "--reattach")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        self.assertEqual(self.h.windows(),
                         ["imp:foo", "claude:foo", "claude:foo", "claude:foo"])

    def test_the_consoles_are_the_same_ones(self):
        # Not "three consoles again" -- the same three windows, still holding
        # whatever was in them. Ids, because that is the difference.
        self.revoke()
        self.h.imp("-s", "foo", "--reattach")
        self.assertTrue(self.h.wait(lambda: self.h.proxy_window()))
        self.assertEqual(self.h.consoles(), self.before)

    def test_the_proxy_is_told_to_reattach_and_waits_for_nothing(self):
        self.revoke()
        self.h.imp("-s", "foo", "--reattach")
        self.assertTrue(self.h.wait(lambda: self.h.proxy_window()))
        cmds = [c for n, c in self.h.start_commands() if n == "imp:foo"]
        self.assertEqual(len(cmds), 1)
        self.assertIn("--reattach", cmds[0])
        # No readiness gate: nothing is waiting to type `claude` anywhere.
        self.assertNotIn("--ready-file", cmds[0])

    def test_claude_is_not_typed_into_a_console_again(self):
        self.revoke()
        # A console at its shell prompt is the shape a stray `claude` would
        # arrive in; this one is running the stub, so a second line would be
        # a second CLAUDE RUNNING.
        self.h.imp("-s", "foo", "--reattach")
        self.assertTrue(self.h.wait(lambda: self.h.proxy_window()))
        self.assertTrue(self.h.wait(
            lambda: "PROXY UP" in self.h.pane_text(self.h.proxy_window())))
        for wid in self.before:
            self.assertEqual(self.h.pane_text(wid).count("CLAUDE RUNNING"), 1,
                             "claude was started twice in %s" % wid)

    def test_the_reaper_is_armed_again(self):
        # A reattached session that had lost its hook would sit there funding
        # nothing after its last console closed.
        self.revoke()
        self.h.imp("-s", "foo", "--reattach")
        self.assertTrue(self.h.wait(lambda: self.h.proxy_window()))
        for wid in self.h.consoles():
            self.h.tmux("kill-window", "-t", wid)
            time.sleep(0.3)
        self.assertTrue(self.h.wait(lambda: self.h.windows() == []),
                        "session outlived its last console: %r"
                        % (self.h.windows(),))

    def test_reattaching_to_a_session_that_still_has_its_proxy_says_so(self):
        r = self.h.imp("-s", "foo", "--reattach")
        self.assertIn("already has a proxy window", r.stderr)
        self.assertEqual(self.h.windows().count("imp:foo"), 1)

    def test_reattaching_to_nothing_names_the_command_that_builds_one(self):
        r = self.h.imp("-s", "bar", "--reattach")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no session 'imp-bar'", r.stderr)
        self.assertIn("imp -s bar", r.stderr)
        self.assertEqual(self.h.windows("imp-bar"), [])


class TestTiling(TmuxCase):
    """Ctrl-B T: three windows to watch one console in, one window to watch
    all three. The binding hands `imp --tile` the window the key was pressed
    in, so that is what these call.
    """

    def setUp(self):
        TmuxCase.setUp(self)
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        self.before = self.h.console_panes()
        self.assertEqual(len(self.before), 3)

    def tile(self, window=None):
        r = self.h.imp("--tile", window or self.h.consoles()[0])
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_the_consoles_become_one_tiled_window(self):
        self.tile()
        self.assertEqual(self.h.windows(), ["imp:foo", "claude:foo"])
        self.assertEqual(len(self.h.panes("imp-foo:1")), 3)

    def test_the_proxy_is_not_one_of_the_panes(self):
        # It owns the reaper hook, and Ctrl-C in it means revoke. Neither
        # belongs in a window full of claudes.
        proxy = self.h.proxy_window()
        self.tile()
        self.assertEqual(self.h.proxy_window(), proxy)
        self.assertEqual(len(self.h.panes(proxy)), 1)

    def test_the_consoles_are_the_same_ones_in_the_same_order(self):
        self.tile()
        self.assertEqual(self.h.console_panes(), self.before)

    def test_it_toggles_back_to_a_window_each(self):
        self.tile()
        self.tile()
        self.assertEqual(self.h.windows(),
                         ["imp:foo", "claude:foo", "claude:foo", "claude:foo"])
        self.assertEqual(self.h.console_panes(), self.before)
        for wid in self.h.consoles():
            self.assertEqual(len(self.h.panes(wid)), 1)

    def test_pressing_it_in_the_proxy_window_tiles_the_consoles(self):
        # The label comes off a window name, and `imp:foo` carries it too.
        self.tile(self.h.proxy_window())
        self.assertEqual(self.h.windows(), ["imp:foo", "claude:foo"])

    def test_tiling_does_not_look_like_the_last_console_closing(self):
        # Three windows are unlinked on the way in, and the reaper watches
        # exactly that. It counts by name, and the merged window still has
        # the name -- so the proxy stays.
        self.tile()
        time.sleep(0.8)
        self.assertIsNotNone(self.h.proxy_window())
        self.assertIn("PROXY UP", self.h.pane_text(self.h.proxy_window()))

    def test_closing_the_tiled_window_still_takes_the_proxy_down(self):
        self.tile()
        self.h.tmux("kill-window", "-t", "imp-foo:1")
        self.assertTrue(self.h.wait(lambda: self.h.windows() == []),
                        "session outlived its consoles: %r"
                        % (self.h.windows(),))

    def test_a_session_with_no_consoles_is_left_alone(self):
        self.h.tmux("new-session", "-d", "-s", "plain", "-n", "shell",
                    "sleep 300")
        wid = self.h.tmux("display", "-p", "-t", "plain",
                          "#{window_id}").stdout.strip()
        r = self.h.imp("--tile", wid)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.h.windows("plain"), ["shell"])
        self.assertEqual(len(self.h.windows()), 4)


class TestMeter(TmuxCase):
    """The status-line segment: how busy the far side is, and how busy this
    machine is.

    The far side's number is the one that costs a round trip, so what is
    tested is mostly the caching around it -- that nothing blocks on it, that
    it is not asked for again while a reading is fresh, and that a reading is
    never shown without saying how old it is.
    """

    def setUp(self):
        TmuxCase.setUp(self)
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))

    def meter(self, session="imp-foo"):
        r = self.h.imp("--meter", session)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def samples(self):
        try:
            with open(self.h.calls) as f:
                return [l for l in f if " exec " in l]
        except IOError:
            return []

    def test_the_local_number_is_there_from_the_first_draw(self):
        # It costs a fork, not a round trip, so it never has to be waited for.
        self.assertRegex(self.meter(), r"mac\s+\d+%")

    def test_the_far_sides_number_is_not_waited_for(self):
        # Nothing has been sampled yet, and the first draw still returns.
        self.assertRegex(self.meter(), r"spr\s+\.\.%")

    def test_a_sample_lands_and_is_shown(self):
        self.meter()
        self.assertTrue(self.h.wait(lambda: re.search(r"spr\s+50%", self.meter())),
                        "no reading arrived: %r" % self.meter())

    def test_a_fresh_reading_is_not_asked_for_again(self):
        self.meter()
        self.assertTrue(self.h.wait(lambda: re.search(r"spr\s+50%", self.meter())))
        before = len(self.samples())
        for _ in range(4):
            self.meter()
        self.assertEqual(len(self.samples()), before,
                         "the meter went back to the sprite while its "
                         "reading was still fresh")

    def test_a_run_queue_past_the_core_count_is_shown(self):
        # A percentage saturates at 100 and stops answering the question the
        # meter is there for: one test suite or three?
        self.h.env["IMP_TEST_LOADAVG"] = "20.0"
        self.meter()
        self.assertTrue(self.h.wait(lambda: re.search(r"spr\s+x2\.5", self.meter())),
                        "no over-subscription figure: %r" % self.meter())
        # And it takes the percentage's place rather than widening the bar.
        self.assertNotIn("50%", self.meter())

    def test_a_badly_oversubscribed_far_side_still_fits_its_column(self):
        # x12.5 is one character wider than x2.4, and one character is all it
        # takes to move `mac` about.
        path = os.path.join(self.h.env["XDG_STATE_HOME"], "imp", "meter-foo")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        widths = set()
        for line in ("50", "50 2.5", "50 13", "50 12.5", "50 1000.25"):
            with open(path, "w") as f:
                f.write("%d %s\n" % (time.time(), line))
            widths.add(len(self.meter()))
        self.assertEqual(len(widths), 1, sorted(widths))

    def test_a_run_queue_of_ten_cores_loses_the_decimal(self):
        self.h.env["IMP_TEST_LOADAVG"] = "104.0"
        self.meter()
        self.assertTrue(self.h.wait(lambda: re.search(r"x13\b", self.meter())),
                        "expected x13 for 104 over 8 cores: %r" % self.meter())

    def test_a_quiet_run_queue_is_not_shown_at_all(self):
        self.meter()
        self.assertTrue(self.h.wait(lambda: re.search(r"spr\s+50%", self.meter())))
        self.assertNotIn("x", self.meter().replace("mac", ""))

    def test_the_columns_hold_still_when_a_number_narrows(self):
        # The whole reason for the padding: 10% dropping to 9% must not drag
        # everything beside it one character to the right.
        self.meter()
        self.assertTrue(self.h.wait(lambda: re.search(r"spr\s+50%", self.meter())))
        path = os.path.join(self.h.env["XDG_STATE_HOME"], "imp", "meter-foo")
        widths = set()
        for pct in ("9", "10", "99", "100"):
            with open(path, "w") as f:
                f.write("%d %s\n" % (time.time(), pct))
            widths.add(len(self.meter()))
        self.assertEqual(len(widths), 1,
                         "the segment changed width with the number: %r"
                         % (sorted(widths),))

    def test_the_run_queue_keeps_its_column_when_it_is_not_there(self):
        path = os.path.join(self.h.env["XDG_STATE_HOME"], "imp", "meter-foo")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        widths = set()
        for line in ("50", "50 2.5"):
            with open(path, "w") as f:
                f.write("%d %s\n" % (time.time(), line))
            widths.add(len(self.meter()))
        self.assertEqual(len(widths), 1,
                         "the run queue moved `mac` when it appeared: %r"
                         % (sorted(widths),))

    def test_an_old_reading_says_that_it_is_old(self):
        # A number with nothing said about its age is the one thing a meter
        # must not show.
        self.meter()
        self.assertTrue(self.h.wait(lambda: re.search(r"spr\s+50%", self.meter())))
        path = os.path.join(self.h.env["XDG_STATE_HOME"], "imp", "meter-foo")
        with open(path, "w") as f:
            f.write("%d 50\n" % (time.time() - 3600))
        self.assertRegex(self.meter(), r"spr\s+~50%")

    def local_cache(self):
        return os.path.join(self.h.env["XDG_STATE_HOME"], "imp", "meter.local")

    def test_the_local_number_holds_still_between_draws(self):
        # It costs a fork rather than a round trip, so this is not about
        # cost: a number recomputed on every draw moves on every draw, and a
        # bar that never sits still is one you keep reading.
        #
        # The local field only. The sprite's is allowed to change between
        # draws and does -- the first sample lands somewhere in here, taking
        # `spr ..%` to `spr 50%`, which is the meter working rather than
        # moving about.
        def mac():
            return re.search(r"mac\s+\d+%", self.meter()).group(0)

        first = mac()
        self.assertEqual([mac() for _ in range(3)], [first] * 3)

    def test_a_written_down_local_number_is_the_one_shown(self):
        path = self.local_cache()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("%d 42\n" % time.time())
        self.assertIn("mac  42%", self.meter())

    def test_an_old_local_number_is_measured_again(self):
        path = self.local_cache()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("%d 42\n" % (time.time() - 3600))
        self.meter()
        with open(path) as f:
            at, _ = f.read().split()
        self.assertGreater(int(at), time.time() - 60)

    def test_it_can_be_told_to_measure_every_draw(self):
        self.h.env["IMP_METER_LOCAL_INTERVAL"] = "0"
        path = self.local_cache()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("%d 42\n" % time.time())
        self.assertNotIn("mac  42%", self.meter())

    def test_a_session_that_ended_is_not_an_error(self):
        # The status line asks for a session that was there when it redrew;
        # by the time the job answers it may not be. That costs the far
        # side's number, not the segment.
        out = self.meter("imp-gone")
        self.assertNotIn("spr", out)
        self.assertRegex(out, r"mac\s+\d+%")

    def test_a_session_that_is_not_imps_has_no_far_side(self):
        self.h.tmux("new-session", "-d", "-s", "plain", "-n", "shell",
                    "sleep 300")
        out = self.meter("plain")
        self.assertNotIn("spr", out)
        self.assertRegex(out, r"mac\s+\d+%")
        self.assertEqual(self.samples(), [])

    def test_it_styles_the_far_side_when_it_is_hot(self):
        self.h.env["IMP_METER_HOT"] = "#[fg=red]"
        self.h.env["IMP_METER_HOT_AT"] = "40"
        self.meter()
        self.assertTrue(self.h.wait(lambda: re.search(r"spr\s+50%", self.meter())))
        self.assertRegex(self.meter(), r"#\[fg=red\]spr\s+50%")

    def test_it_is_quiet_about_a_far_side_it_cannot_reach(self):
        # Only once the consoles have actually reached the stub: replacing it
        # while a pane is still on its way into it kills the console, and
        # three dead consoles are the reaper's business, not the meter's.
        for wid in self.h.consoles():
            self.assertTrue(self.h.wait(
                lambda w=wid: "CONSOLE sprite" in self.h.pane_text(w)))
        write_exec(os.path.join(self.h.bin, "sprite"), """
            #!/bin/bash
            exit 1
        """)
        self.meter()
        time.sleep(1.5)
        out = self.meter()
        self.assertRegex(out, r"spr\s+\.\.%")
        self.assertRegex(out, r"mac\s+\d+%")


class TestKeyBindings(TmuxCase):
    def test_it_binds_a_key_that_skips_the_proxy_window(self):
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        keys = self.h.tmux("list-keys", "-T", "prefix").stdout
        self.assertIn("C-n", keys)
        self.assertIn("C-p", keys)
        self.assertIn("imp:*", keys)

    def test_it_binds_a_key_for_the_tiled_view(self):
        self.h.imp("-s", "foo")
        self.assertTrue(self.h.wait(lambda: len(self.h.windows()) == 4))
        keys = self.h.tmux("list-keys", "-T", "prefix").stdout
        self.assertIn("--tile", keys)
        # The window the key was pressed in, expanded by tmux rather than
        # guessed by the script.
        self.assertIn("#{window_id}", keys)

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
