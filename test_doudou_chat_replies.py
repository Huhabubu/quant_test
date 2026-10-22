import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from minecraft_doudou.bridge import Controller, make_handler
from minecraft_doudou.chat_replies import (
    ReplyError, ReplyQueue, run_sender, submit_chat, validate_bridge_url,
)
from minecraft_doudou.event_receiver import EventInbox


class FakeConsole:
    def __init__(self):
        self.commands = []

    def send(self, command):
        self.commands.append(command)

    def available(self):
        return True


class ChatRepliesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "inbox.sqlite")
        self.inbox = EventInbox(self.db)
        self.addCleanup(self.inbox.close)
        self.q = ReplyQueue(self.db)
        self.addCleanup(self.q.close)

    def accept(self, event_id="e1", event=None):
        event = event or {"type": "chat", "sender": "Player", "text": "你好"}
        self.assertEqual(self.inbox.accept(event_id, event), "accepted")
        return self.inbox.pending()[-1]["seq"]

    def test_model_reply_persists_and_ack_is_atomic(self):
        seq = self.accept()
        self.assertEqual(self.q.prepare("e1", seq, "在，我正在建房。"), "queued")
        self.assertEqual(self.inbox.counts()["pending"], 0)
        self.assertEqual(self.q.prepare("e1", seq, "在，我正在建房。"), "duplicate")
        self.assertEqual(self.q.prepare("e1", seq, "另一个回复"), "conflict")
        self.assertEqual(self.q.stats(), {"queued": 1})
        path = Path(self.temp.name) / "mock-replies.jsonl"
        result = run_sender(self.db, mock_log=str(path), once=True)
        self.assertEqual(result["state"], "mocked")
        self.assertFalse(result["verified"])
        self.assertEqual(self.q.stats(), {"mocked": 1})
        self.assertEqual(len(path.read_text().splitlines()), 1)
        self.assertIn("在，我正在建房。", path.read_text())
        self.assertIsNone(run_sender(self.db, mock_log=str(path), once=True))
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_no_reply_for_old_or_unrelated_or_acknowledged_event(self):
        ts = self.accept("old")
        row = self.inbox.conn.execute(
            "SELECT received_at FROM events WHERE id='old'"
        ).fetchone()[0]
        with self.assertRaisesRegex(ReplyError, "stale"):
            self.q.prepare("old", ts, "过时", now=row + 130)
        self.assertEqual(self.inbox.counts()["pending"], 1)
        self.assertEqual(self.inbox.accept("status", {"type": "player_nearby"}), "accepted")
        with self.assertRaisesRegex(ReplyError, "pending_chat"):
            self.q.prepare("status", 2, "不许")
        self.inbox.ack_through(2)
        with self.assertRaisesRegex(ReplyError, "pending_chat"):
            self.q.prepare("old", 2, "已经处理")

    def test_no_chat_command_injection(self):
        seq = self.accept()
        for bad in ["", "hello\nquit", "\x00", "a" * 201, 1]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ReplyError, "chat_text"):
                self.q.prepare("e1", seq, bad)
        self.assertEqual(self.q.stats(), {})
        self.assertEqual(self.q.prepare("e1", seq, "正常回复"), "queued")

    def test_avoid_acknowledging_newer_chat_unseen(self):
        self.accept("e1")
        newest = self.accept("e2")
        with self.assertRaisesRegex(ReplyError, "newer_chat"):
            self.q.prepare("e1", newest, "只回答第一条")
        self.assertEqual(self.inbox.counts()["pending"], 2)
        self.assertEqual(self.q.prepare("e2", newest, "这是合并后的回答"), "queued")
        self.assertEqual(self.inbox.counts()["pending"], 0)

    def test_two_concurrent_dispatchers_cannot_double_send(self):
        seq = self.accept()
        self.q.prepare("e1", seq, "一次发送")
        seen = []
        barrier = threading.Barrier(3)
        def dispatch():
            sender = ReplyQueue(self.db)
            try:
                barrier.wait(timeout=2)
                return sender.send_once(
                    lambda text: (seen.append(text), time.sleep(.025),
                                  {"accepted": True})[-1])
            finally:
                sender.close()
        results = []
        def work():
            results.append(dispatch())
        t1 = threading.Thread(target=work)
        t2 = threading.Thread(target=work)
        t1.start(); t2.start()
        barrier.wait(timeout=2)
        t1.join(2); t2.join(2)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(seen, ["一次发送"])
        self.assertEqual(len([r for r in results if r is not None]), 1)
        self.assertEqual(self.q.stats(), {"accepted_unverified": 1})

    def test_network_unknown_is_not_retried(self):
        seq = self.accept()
        self.q.prepare("e1", seq, "只投一次")
        attempted = []
        def failed(text):
            attempted.append(text)
            raise TimeoutError("the bridge could have sent it")
        self.assertEqual(self.q.send_once(failed)["state"], "uncertain")
        self.assertIsNone(self.q.send_once(failed))
        self.assertEqual(len(attempted), 1)
        self.assertEqual(self.q.stats(), {"uncertain": 1})

    def test_process_restart_in_sending_state_requires_review(self):
        seq = self.accept()
        self.q.prepare("e1", seq, "留痕")
        with self.q.conn:
            self.q.conn.execute(
                "UPDATE chat_replies SET state='sending' WHERE event_id='e1'"
            )
        fresh = ReplyQueue(self.db)
        try:
            self.assertIsNone(fresh.send_once(lambda text: self.fail("repeated")))
            self.assertEqual(fresh.stats()["sending"], 1)
        finally:
            fresh.close()

    def test_bridge_is_explicit_chat_only_loopback(self):
        for invalid in [
            "https://public.example/v1/chat",
            "http://127.0.0.1:8765/v1/move",
            "http://127.0.0.1:8765/v1/chat?x=1",
            "http://localhost/v1/chat",
            "http://bad@127.0.0.1:8765/v1/chat"
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ReplyError):
                validate_bridge_url(invalid)

    def test_sends_through_existing_bridge_chat_not_arbitrary_commands(self):
        console = FakeConsole()
        bridge = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(
                Controller(console, "example.org", 25565), "x" * 32))
        t = threading.Thread(target=bridge.serve_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (bridge.shutdown(), t.join(timeout=2),
                                 bridge.server_close()))
        seq = self.accept()
        self.q.prepare("e1", seq, '你好 "朋友"')
        url = f"http://127.0.0.1:{bridge.server_port}/v1/chat"
        tokenfile = Path(self.temp.name) / "token"
        tokenfile.write_text("x" * 32)
        tokenfile.chmod(0o600)
        result = run_sender(self.db, bridge_url=url,
                            token_file=str(tokenfile), once=True)
        self.assertEqual(result["state"], "accepted_unverified")
        self.assertEqual(console.commands, ['msg "你好 \\"朋友\\""'])
        self.assertEqual(self.q.stats(), {"accepted_unverified": 1})
        with self.assertRaises(ReplyError):
            run_sender(self.db, bridge_url=url.replace("chat", "move"),
                       token_file=str(tokenfile), once=True)


if __name__ == "__main__":
    unittest.main()
