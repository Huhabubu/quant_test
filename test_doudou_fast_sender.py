"""Focused regressions for fast M1 sender; no real game/network contact."""
from datetime import datetime
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from minecraft_doudou.chat_replies import ReplyQueue
from minecraft_doudou.event_receiver import EventInbox
from minecraft_doudou.fast_sender import LogWriteOff
from minecraft_doudou.m1_chat import submit_m1_chat_fast


class FakeSocket:
    def __init__(self):
        self.parts = [b"Connected to Machine One\n<<END\n"]
        self.sent = []
        self.closed = False
    def recv(self, n):
        return self.parts.pop(0) if self.parts else b""
    def sendall(self, data):
        self.sent.append(data)
    def settimeout(self, value):
        self.timeout = value
    def close(self):
        self.closed = True


class FastSenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.db = str(self.path / "inbox.sqlite")
        self.log = self.path / "latest.log"
        self.log.write_bytes(b"[12:00:00] [CHAT] <DouDou> OLD\n")
        inbox = EventInbox(self.db)
        self.addCleanup(inbox.close)
        self.inbox = inbox
        self.q = ReplyQueue(self.db)
        self.addCleanup(self.q.close)
        self.writeoff = LogWriteOff(self.q.conn, str(self.log))

    def enqueue(self, eid, text):
        self.inbox.accept(eid, {"type": "chat", "sender": "Huhabubu",
                                "text": "hi " + eid})
        seq = self.inbox.conn.execute(
            "SELECT seq FROM events WHERE id=?", (eid,)).fetchone()[0]
        self.assertEqual(self.q.prepare(eid, seq, text), "queued")

    def put_echo(self, text, *, sender="DouDou", stamp=None):
        clock = datetime.fromtimestamp(time.time()).strftime("%H:%M:%S")
        line = "[%s] [Render thread/INFO] [CHAT] <%s> %s\n" % (
            stamp or clock, sender, text)
        with self.log.open("ab") as stream:
            stream.write(line.encode("utf-8"))

    def test_fast_tcp_write_does_not_wait_for_say_receipt(self):
        fake = FakeSocket()
        result = submit_m1_chat_fast("你好", connect=lambda *_: fake)
        self.assertEqual(fake.sent, ["say 你好\n".encode()])
        self.assertTrue(fake.closed)
        self.assertEqual(result["awaiting_game_log"], True)
        self.assertFalse(result["verified"])
        self.assertLessEqual(fake.timeout, 3.0)

    def test_two_identical_replies_need_two_independent_log_lines(self):
        self.enqueue("e1", "一样的回复")
        self.enqueue("e2", "一样的回复")
        calls = []
        def send(text):
            calls.append(text)
            return {"accepted": True, "transport": "m1",
                    "awaiting_game_log": True}
        self.assertEqual(self.q.send_once(send, on_claim=self.writeoff.claim)[
            "state"], "sent_unverified")
        self.assertEqual(self.q.send_once(send, on_claim=self.writeoff.claim)[
            "state"], "sent_unverified")
        self.assertEqual(len(calls), 2)
        self.put_echo("一样的回复")
        self.assertEqual(self.writeoff.scan()["confirmed"], 1)
        self.assertEqual(self.q.stats(), {"game_log_confirmed": 1,
                                         "sent_unverified": 1})
        self.assertEqual(self.writeoff.scan()["confirmed"], 0)
        self.put_echo("一样的回复")
        self.assertEqual(self.writeoff.scan()["confirmed"], 1)
        self.assertEqual(self.q.stats(), {"game_log_confirmed": 2})
        self.assertIsNone(self.q.send_once(send, on_claim=self.writeoff.claim))
        self.assertEqual(len(calls), 2)

    def test_wrong_sender_or_pre_send_history_cannot_confirm(self):
        self.enqueue("e1", "OLD")
        self.q.send_once(lambda _: {"accepted": True, "transport": "m1",
                                     "awaiting_game_log": True},
                         on_claim=self.writeoff.claim)
        self.put_echo("OLD", sender="Huhabubu")
        self.assertEqual(self.writeoff.scan()["confirmed"], 0)
        self.assertEqual(self.q.stats(), {"sent_unverified": 1})
        self.put_echo("OLD")
        self.assertEqual(self.writeoff.scan()["confirmed"], 1)

    def test_log_rotation_never_confirms_old_attempt(self):
        self.enqueue("e1", "待复核")
        self.q.send_once(lambda _: {"accepted": True, "transport": "m1",
                                     "awaiting_game_log": True},
                         on_claim=self.writeoff.claim)
        self.log.rename(self.path / "archived.log")
        self.log.write_bytes(b"[12:00:00] [CHAT] <DouDou> 待复核\n")
        self.assertEqual(self.writeoff.scan()["rotated"], 1)
        self.assertEqual(self.q.stats(), {"uncertain": 1})

    def test_uncertain_wire_result_is_never_retried(self):
        self.enqueue("e1", "一次性发送")
        attempted = []
        def broken(text):
            attempted.append(text)
            raise TimeoutError("possibly delivered")
        self.assertEqual(self.q.send_once(broken, on_claim=self.writeoff.claim)[
            "state"], "uncertain")
        self.assertIsNone(self.q.send_once(broken, on_claim=self.writeoff.claim))
        self.assertEqual(attempted, ["一次性发送"])


if __name__ == "__main__":
    unittest.main()
