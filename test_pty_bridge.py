import sys
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
            with self.assertRaises(InvalidCommand):
                console.send("msg hello\nexit")
        finally:
            console.stop()
        self.assertFalse(console.available())
        with self.assertRaises(ConsoleUnavailable):
            console.send("msg hello")

    def wait_for(self, console, exact):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if any(exact in text for _, text in console.recent_output()["lines"]):
                return
            time.sleep(0.02)
        self.fail("Didn't see %s in %s" % (exact, console.recent_output()["lines"]))


if __name__ == "__main__":
    unittest.main()
