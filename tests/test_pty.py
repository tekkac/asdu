"""Small real-PTY checks; fictional data only, no terminal emulator required."""

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.resize(24, 100)
        self.original_modes = termios.tcgetattr(self.slave)
        env = dict(os.environ, TERM="xterm-256color", LC_ALL="C.UTF-8")
        for key in (
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ):
            env[key] = self.directory.name
        fixtures = ROOT / "tests" / "fixtures" / "asdu"
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "asdu.py"),
                "--all",
                "--no-progress",
                "--codex-root",
                str(fixtures),
                "--claude-root",
                self.directory.name,
            ],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            env=env,
        )
        self.addCleanup(self.stop)
        self.output = b""
        self.wait_for(b"help")

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=3)

    def resize(self, rows, columns):
        fcntl.ioctl(
            self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
        )

    def read_for(self, seconds):
        result = b""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [self.master], [], [], max(0, deadline - time.monotonic())
            )
            if ready:
                result += os.read(self.master, 65536)
        self.output += result
        return result

    def wait_for(self, text):
        deadline = time.monotonic() + 5
        start = len(self.output)
        while text not in self.output[start:] and time.monotonic() < deadline:
            self.read_for(0.05)
            if self.process.poll() is not None:
                break
        self.assertIn(text, self.output[start:])

    def finish(self, status):
        deadline = time.monotonic() + 3
        while self.process.poll() is None and time.monotonic() < deadline:
            self.read_for(0.05)
        self.assertEqual(
            self.process.poll(), status, self.output.decode(errors="replace")
        )
        self.read_for(0.05)
        self.assertNotIn(b"Traceback", self.output)
        self.assertIn(b"\x1b[2J", self.output.split(b"\x1b[?1049l")[-1])
        modes = termios.tcgetattr(self.slave)
        # BSD sets PENDIN when cooked input is restored; it is a transient flag.
        modes[3] &= ~getattr(termios, "PENDIN", 0)
        original = self.original_modes.copy()
        original[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(modes, original)

    def test_startup_resize_and_quit(self):
        for rows, columns in ((8, 40), (35, 180)):
            self.resize(rows, columns)
            self.process.send_signal(signal.SIGWINCH)
            self.wait_for(b"asdu")
        os.write(self.master, b"q")
        self.finish(0)

    def test_ctrl_c_restores_terminal(self):
        self.process.send_signal(signal.SIGINT)
        self.finish(130)
        self.assertIn(b"\x1b[2J", self.output)

    def test_arrow_bursts_and_reversals(self):
        for prefix in (b"\x1bO", b"\x1b["):
            os.write(self.master, (prefix + b"A") * 40 + (prefix + b"B") * 40)
            self.read_for(0.1)
            self.assertIsNone(self.process.poll(), self.output.decode(errors="replace"))
        os.write(self.master, b"q")
        self.finish(0)

    def test_delayed_and_fragmented_theme_reply_is_not_a_command(self):
        self.read_for(0.1)
        os.write(self.master, b"\x1b]11;rgb:")
        self.assertEqual(self.read_for(0.1), b"")
        os.write(self.master, b"ffff/ffff/ffff\x1b\\")
        self.assertEqual(self.read_for(0.1), b"")
        self.assertIsNone(self.process.poll())
        os.write(self.master, b"q")
        self.finish(0)
