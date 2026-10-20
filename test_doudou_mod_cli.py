import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from minecraft_doudou.mod_cli import main


class ModCliTests(unittest.TestCase):
    def test_manual_state_and_eat_without_bridge(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            (root / "commands").mkdir()
            (root / "results").mkdir()
            (root / "state.json").write_text(json.dumps({
                "schema": 1,
                "session_id": "c" * 32,
                "captured_at_ms": int(time.time() * 1000),
                "in_world": True, "dead": False,
                "hunger": 6,
                "inventory": [{"slot": 4, "id": "minecraft:golden_carrot", "count": 6}]
            }))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(["--root", dirname, "state"]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["hunger"], 6)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(["--root", dirname, "eat", "--slot", "4"]), 0)
            info = json.loads(stdout.getvalue())
            queued = root / "commands" / (info["id"] + ".json")
            self.assertTrue(queued.is_file())
            self.assertEqual(json.loads(queued.read_text())["slot"], 4)

    def test_fails_closed_without_game(self):
        with tempfile.TemporaryDirectory() as dirname:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--root", dirname, "eat"]), 2)


if __name__ == "__main__":
    unittest.main()
