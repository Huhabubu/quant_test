import tempfile
from pathlib import Path
import threading
import time
import unittest

from minecraft_doudou.events import MinecraftChatEvents, parse_chat


class ChatEventTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "latest.log"

    def append(self, line):
        with self.path.open("a", encoding="utf-8") as out:
            out.write(line)
            out.flush()

    def await_events(self, watcher, after=0):
        result = watcher.get(after=after, wait_ms=1500)
        self.assertTrue(result["events"], result)
        return result

    def test_parser_filters_owner_and_preserves_chinese(self):
        self.assertEqual(
            parse_chat("[16:30:01] [Render thread/INFO] [CHAT] <Huhabubu> 跟着我\n",
                       "Huhabubu")["text"],
            "跟着我"
        )
        self.assertIsNone(parse_chat("[CHAT] <SekaiSaionji> hello", "Huhabubu"))
        self.assertIsNone(parse_chat("[CHAT] DouDou joined the game", "Huhabubu"))

    def test_follows_appended_lines_and_never_replays_history(self):
        self.path.write_text("[CHAT] <Huhabubu> 旧的消息\n", encoding="utf-8")
        watcher = MinecraftChatEvents(self.path, interval=0.02)
        watcher.start()
        try:
            time.sleep(0.09)  # initial seek to EOF
            self.assertEqual(watcher.get()["events"], [])
            self.append("[CHAT] <SekaiSaionji> 不应转发\n")
            self.append("[CHAT] <Huhabubu> 豆豆，跟着我\n")
            result = self.await_events(watcher)
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(result["events"][0]["text"], "豆豆，跟着我")
            seq = result["next_cursor"]
            self.assertEqual(watcher.get(after=seq)["events"], [])
            self.append("[CHAT] <Huhabubu> 等一下\n")
            next_result = self.await_events(watcher, after=seq)
            self.assertEqual(next_result["events"][0]["text"], "等一下")
            self.assertEqual(next_result["next_cursor"], seq + 1)
        finally:
            watcher.stop()

    def test_partial_multibyte_line_waits_and_rollover_recovers(self):
        self.path.write_text("", encoding="utf-8")
        watcher = MinecraftChatEvents(self.path, interval=0.02)
        watcher.start()
        try:
            time.sleep(0.08)
            self.append("[CHAT] <Huhabubu> 我说中文")
            time.sleep(0.10)
            self.assertEqual(watcher.get()["events"], [])
            self.append("啦\n")
            self.assertEqual(self.await_events(watcher)["events"][0]["text"], "我说中文啦")
            old_seq = watcher.last_sequence
            # Replace the log atomically (Minecraft restart / log rollover).
            replacement = self.path.with_name("new_latest.log")
            replacement.write_text("[CHAT] <Huhabubu> 新游戏\n", encoding="utf-8")
            replacement.replace(self.path)
            self.assertEqual(self.await_events(watcher, after=old_seq)["events"][0]["text"],
                             "新游戏")
        finally:
            watcher.stop()

    def test_longpoll_notifies_without_periodic_worker(self):
        watcher = MinecraftChatEvents(self.path, interval=0.02)
        got = []
        thread = threading.Thread(
            target=lambda: got.append(watcher.get(after=0, wait_ms=1000))
        )
        thread.start()
        try:
            time.sleep(0.05)
            watcher.publish_line("[CHAT] <Huhabubu> 我在这里\n")
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(got[0]["events"][0]["text"], "我在这里")
        finally:
            watcher.stop()
            thread.join(timeout=1)

    def test_cursor_overflow_signaled(self):
        watcher = MinecraftChatEvents(self.path, maxlen=2)
        for n in range(4):
            watcher.publish_line("[CHAT] <Huhabubu> message%d\n" % n)
        result = watcher.get(after=0)
        self.assertEqual(len(result["events"]), 2)
        self.assertTrue(result["dropped"])
        self.assertEqual(result["next_cursor"], 4)


if __name__ == "__main__":
    unittest.main()
