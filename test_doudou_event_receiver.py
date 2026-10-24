import http.client
import json
from multiprocessing import Process
from pathlib import Path
import socket
import tempfile
import time
import unittest

from minecraft_doudou.event_receiver import (
    EventInbox, MAX_BODY, arm, serve, validate_request,
)
from minecraft_doudou.event_relay import EventOutbox, deliver_pending


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "inbox.sqlite")

    def test_pending_ack_and_same_id_dedupe(self):
        inbox = EventInbox(self.db)
        self.addCleanup(inbox.close)
        self.assertEqual(inbox.accept("a1", {"type": "player_nearby"}), "accepted")
        self.assertIsNone(inbox.actionable())
        self.assertEqual(arm(self.db, timeout=0.03, poll_seconds=0.02), 124)
        self.assertEqual(inbox.accept("a2", {"type": "chat", "text": "跟我来"}), "accepted")
        self.assertEqual(inbox.accept("a2", {"type": "chat", "text": "跟我来"}), "duplicate")
        self.assertEqual(inbox.accept("a2", {"type": "chat", "text": "不同内容"}), "conflict")
        self.assertEqual(inbox.counts()["pending"], 2)
        self.assertEqual(inbox.actionable()[1], "a2")
        self.assertEqual(arm(self.db, timeout=0.03, poll_seconds=0.02), 0)
        batch = inbox.pending(limit=1)
        self.assertEqual(batch[0]["id"], "a1")
        self.assertEqual(len(inbox.pending(batch[-1]["seq"], limit=1000)), 1)
        self.assertEqual(inbox.ack_through(2), 2)
        self.assertEqual(inbox.counts()["pending"], 0)
        inbox.close()
        again = EventInbox(self.db)
        self.assertEqual(again.pending(), [])
        self.assertEqual(again.accept("a2", {"type": "chat", "text": "跟我来"}), "duplicate")
        again.close()

    def test_bad_json_and_event_ids(self):
        self.assertIsNone(validate_request({}, b"[]"))
        self.assertIsNone(validate_request({}, b"{}"))
        self.assertIsNone(validate_request({}, b"{broken"))
        payload = json.dumps({"id": "abc", "event": {"type": "chat", "text": "x"}}).encode()
        self.assertIsNotNone(validate_request({"X-DouDou-Event-ID": "abc"}, payload))
        self.assertIsNone(validate_request({"X-DouDou-Event-ID": "wrong"}, payload))
        self.assertIsNone(validate_request(
            {}, json.dumps({"id": "", "event": {"type": "chat"}}).encode()))

    def test_fast_bulk_drain_without_one_message_per_wake(self):
        outbox = EventOutbox(Path(self.temp.name) / "outbox.sqlite")
        for i in range(100):
            outbox.put({"type": "chat", "text": f"line{i}"})
        sent = []
        n = deliver_pending(outbox, "http://127.0.0.1:18765/events", "x" * 32,
                            deliver=lambda url, token, eid, event: sent.append(eid),
                            max_seconds=2)
        self.assertEqual(n, 100)
        self.assertEqual(len(set(sent)), 100)
        self.assertIsNone(outbox.next())
        outbox.close()

    def test_http_rejects_bad_inputs_without_unarming(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        token = "x" * 48
        process = Process(target=serve, args=(self.db, port, token), daemon=True)
        process.start()
        self.addCleanup(lambda: (process.terminate(), process.join(timeout=2)))
        def post(data, auth=token, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            h = {"Authorization": "Bearer " + auth, "Content-Type": "application/json"}
            h.update(headers or {})
            try:
                conn.request("POST", "/events", body=data, headers=h)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        for _ in range(40):
            if not process.is_alive():
                self.fail("receiver crashed before test")
            try:
                post(b"{}", auth="bad")
                break
            except OSError:
                time.sleep(0.025)
        else:
            self.fail("receiver not ready")
        self.assertEqual(post(b"{}", auth="bad")[0], 401)
        self.assertEqual(post(b"[]")[0], 400)
        self.assertEqual(post(b'{"id":"","event":{"type":"chat"}}')[0], 400)
        self.assertEqual(post(b"x" * (MAX_BODY + 1))[0], 413)
        body = json.dumps({"id": "unique_1",
                           "event": {"type": "chat", "sender": "Huhabubu", "text": "你好"}}).encode()
        self.assertEqual(post(body)[1]["status"], "accepted")
        self.assertEqual(post(body)[1]["status"], "duplicate")
        self.assertEqual(post(body.replace(b"unique_1", b"unique_2"))[0], 200)
        inbox = EventInbox(self.db)
        self.assertEqual(inbox.counts()["pending"], 2)
        inbox.close()
        self.assertTrue(process.is_alive())


if __name__ == "__main__":
    unittest.main()
