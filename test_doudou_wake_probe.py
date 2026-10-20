import tempfile
from pathlib import Path
import threading
import time
import unittest

from minecraft_doudou.wake_probe import wait_for_first_chat


class OneShotWakeProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "latest.log"
        self.log.write_text("[CHAT] <Huhabubu> 旧消息\n", encoding="utf-8")

    def append(self, value):
        with self.log.open("a", encoding="utf-8") as file:
            file.write(value)
            file.flush()

    def test_single_new_owner_message_completes_without_three(self):
        result = []
        runner = threading.Thread(target=lambda: result.append(
            wait_for_first_chat(self.log, "Huhabubu", timeout=2, interval=.01)
        ))
        runner.start()
        try:
            time.sleep(.10)
            self.append("[CHAT] <SekaiSaionji> 其他人的消息\n")
            time.sleep(.10)
            self.assertTrue(runner.is_alive())
            self.append("[CHAT] <Huhabubu> 豆豆，请回答我\n")
            runner.join(timeout=2)
            self.assertFalse(runner.is_alive(), "A single owner chat must end this process")
            self.assertEqual(result[0]["text"], "豆豆，请回答我")
        finally:
            runner.join(timeout=3)

    def test_timeout_returns_none_without_app_wakeup(self):
        self.assertIsNone(wait_for_first_chat(self.log, "Huhabubu",
                                               timeout=.1, interval=.01))

    def test_invalid_player_is_rejected(self):
        with self.assertRaises(ValueError):
            wait_for_first_chat(self.log, "Huhabubu;quit")


if __name__ == "__main__":
    unittest.main()
