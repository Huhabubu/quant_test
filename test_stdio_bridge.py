import sys
import time
import unittest

from minecraft_doudou.bridge import ConsoleUnavailable, InvalidCommand
from minecraft_doudou.stdio_bridge import StdioConsole


class StdioConsoleTests(unittest.TestCase):
    def setUp(self):
        self.console = StdioConsole([
            sys.executable, "-u", "-c",
            'import sys; print("READY", flush=True); '
            '[(print("RECV:" + line.rstrip("\\n"), flush=True)) for line in sys.stdin]'
        ])
        self.console.start()

    def tearDown(self):
        self.console.stop()

    def wait_for(self, value):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            lines = self.console.recent_output()["lines"]
            if any(text == value for _, text in lines):
                return
            time.sleep(0.01)
        self.fail("Missing output: " + value)

    def test_round_trip_and_output(self):
        self.assertTrue(self.console.available())
        self.wait_for("READY")
        self.console.send('msg "你好"')
        self.wait_for('RECV:msg "你好"')
        self.assertGreater(self.console.recent_output()["last_sequence"], 1)

    def test_reject_invalid_console_input(self):
        for bad in ("quit\nexit", "quit\r", "x\x00", 42):
            with self.subTest(bad=bad), self.assertRaises(InvalidCommand):
                self.console.send(bad)

    def test_stopped_session_rejects_commands(self):
        self.console.stop()
        self.assertFalse(self.console.available())
        with self.assertRaises(ConsoleUnavailable):
            self.console.send('msg "hi"')


if __name__ == "__main__":
    unittest.main()
