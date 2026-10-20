import sys
import tempfile
from pathlib import Path
import time
import unittest

from minecraft_doudou.bridge import ConsoleUnavailable, InvalidCommand
from minecraft_doudou.pty_bridge import PtyConsole


class PtyConsoleTests(unittest.TestCase):
    def test_interactive_tty_round_trip_and_shutdown(self):
        # This tests a real Linux PTY, without starting Minecraft or Java.
        command = [
            sys.executable, "-u", "-c",
            'import sys; print("ISATTY", sys.stdin.isatty(), '
            'sys.stdout.isatty(), flush=True); '
            '[(print("RECV:" + line.strip(), flush=True)) for line in sys.stdin]'
        ]
        console = PtyConsole(command)
        try:
            console.start()
            self.wait_for(console, "ISATTY True True")
            console.send("msg hello")
            self.wait_for(console, "RECV:msg hello")
            # Python->PTY->Python round trip: isolates console transport from
            # downstream Java/JLine/Minecraft chat behavior.
            console.send("msg 你好，豆豆")
            self.wait_for(console, "RECV:msg 你好，豆豆")
            with self.assertRaises(InvalidCommand):
                console.send("msg hello\nexit")
        finally:
            console.stop()
        self.assertFalse(console.available())
        with self.assertRaises(ConsoleUnavailable):
            console.send("msg hello")

    def test_partial_prompt_visible_without_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = PtyConsole([
                sys.executable, "-u", "-c",
                'import sys,time; sys.stdout.write("HMC> "); sys.stdout.flush(); time.sleep(2)',
            ], diagnostic_path=Path(tmp) / "console.log")
            try:
                console.start()
                self.wait_for_partial(console, "HMC> ")
                self.assertIn("HMC> ", (Path(tmp) / "console.log").read_text())
            finally:
                console.stop()

    def test_exit_code_and_last_console_text_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.log"
            console = PtyConsole([
                sys.executable, "-u", "-c",
                'import sys; sys.stderr.write("SIMULATED ERROR\\n"); sys.exit(23)',
            ], diagnostic_path=path)
            try:
                console.start()
                deadline = time.monotonic() + 5
                while console.available() and time.monotonic() < deadline:
                    time.sleep(0.03)
                self.assertEqual(console.recent_output()["exit_code"], 23)
                self.wait_for(console, "SIMULATED ERROR")
                self.wait_for(console, "returncode=23")
            finally:
                console.stop()
            self.assertIn("SIMULATED ERROR", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def wait_for_partial(self, console, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if expected in console.recent_output().get("partial", ""):
                return
            time.sleep(0.02)
        self.fail("PTY incomplete prompt not visible: %r" % console.recent_output())

    def wait_for(self, console, exact):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if any(exact in text for _, text in console.recent_output()["lines"]):
                return
            time.sleep(0.02)
        self.fail("Didn't see %s in %s" % (exact, console.recent_output()["lines"]))


if __name__ == "__main__":
    unittest.main()
