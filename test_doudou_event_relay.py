import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from minecraft_doudou.event_relay import (
    EventOutbox, StateChangeTracker, http_deliver, pump_once, validate_receiver
)


class EventRelayTests(unittest.TestCase):
    def test_reject_remote_and_unauthenticated_receiver_paths(self):
        for url in (
            "http://evil.test:80/hook", "https://evil.test/hook",
            "http://127.0.0.1/hook", "http://127.0.0.1:8888",
            "http://x@127.0.0.1:8888/hook", "http://127.0.0.1:8888/hook?secret=1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_receiver(url)
        self.assertEqual(validate_receiver("http://127.0.0.1:9876/events"),
                         "http://127.0.0.1:9876/events")

    def test_sqlite_pending_survives_restart_and_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private.sqlite"
            q = EventOutbox(path)
            q.put({"type": "chat", "text": "你好"})
            event_id, payload = q.next()
            self.assertEqual(payload["text"], "你好")
            q.close()
            q = EventOutbox(path)
            self.assertEqual(q.next()[0], event_id)
            q.ack(event_id)
            self.assertIsNone(q.next())
            q.close()

    def test_push_includes_idempotency_and_token(self):
        seen = []
        class Response:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *args): return None
        def opener(request, timeout):
            seen.append((request, timeout))
            return Response()
        http_deliver("http://localhost:8080/notify", "a" * 32, "same-id",
                     {"type": "chat", "text": "你好"}, opener=opener)
        req, timeout = seen[0]
        self.assertEqual(json.loads(req.data)["id"], "same-id")
        self.assertEqual(json.loads(req.data)["event"]["text"], "你好")
        self.assertEqual(req.get_header("X-doudou-event-id"), "same-id")
        self.assertEqual(timeout, 3)

    def test_state_changes_ignores_baseline_and_repeated_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "state.json"
            stamp = int(time.time() * 1000)
            base = {"schema": 1, "session_id": "x" * 32, "captured_at_ms": stamp,
                    "in_world": True, "dead": False, "health": 20, "hunger": 10,
                    "inventory": [{"item_id": "minecraft:oak_log", "count": 5}],
                    "nearby_entities": []}
            file.write_text(json.dumps(base))
            tracker = StateChangeTracker(file)
            self.assertEqual(tracker.poll(), [])
            self.assertEqual(tracker.poll(), [])
            base.update(captured_at_ms=stamp+1, health=15, hunger=6, dead=True,
                        inventory=[{"item_id": "minecraft:oak_log", "count": 3}])
            file.write_text(json.dumps(base))
            ev = tracker.poll()
            self.assertEqual({x["type"] for x in ev},
                             {"health_drop", "low_hunger", "death", "inventory_changed"})
            self.assertEqual(tracker.poll(), [])
            base.update(session_id="y"*32, captured_at_ms=stamp+2)
            file.write_text(json.dumps(base))
            self.assertEqual(tracker.poll(), [])

    def test_relay_retries_without_ack_or_duplicate_insertion(self):
        class Source:
            def __init__(self): self.calls = 0
            def get(self, **args):
                self.calls += 1
                if self.calls == 1:
                    return {"events": [{"type": "chat", "text": "hi", "sequence": 1}],
                            "next_cursor": 1, "dropped": False}
                return {"events": [], "next_cursor": 1, "dropped": False}
        with tempfile.TemporaryDirectory() as temp:
            outbox = EventOutbox(Path(temp) / "out.sqlite")
            src = Source()
            def fail(*args): raise OSError("temporarily down")
            cursor = pump_once(src, 0, None, outbox, "http://localhost:8080/notify", "x"*32,
                               deliver=fail)
            self.assertEqual(cursor, 1)
            event_id, _ = outbox.next()
            sent = []
            cursor = pump_once(src, cursor, None, outbox, "http://localhost:8080/notify", "x"*32,
                               deliver=lambda url, tok, eid, event: sent.append((eid, event)))
            self.assertEqual(sent[0][0], event_id)
            self.assertEqual(cursor, 1)
            self.assertIsNone(outbox.next())
            outbox.close()


if __name__ == "__main__":
    unittest.main()
