"""Focused safety regressions for runtime recovery; no Minecraft or Muse calls."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from minecraft_doudou.chat_replies import ReplyError, ReplyQueue
from minecraft_doudou.event_lifecycle import EventReviews, ReviewError
from minecraft_doudou.event_receiver import EventInbox
from minecraft_doudou.runtime_coordinator import (
    CoordinatorError, monitor, sample,
)


class RuntimeReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = str(root / "inbox.sqlite")
        self.status = str(root / "status.json")
        self.lock = str(root / "coordinator.lock")
        self.inbox = EventInbox(self.db)
        self.addCleanup(self.inbox.close)
        self.reply = ReplyQueue(self.db)
        self.addCleanup(self.reply.close)
        self.reviews = EventReviews(self.db)
        self.addCleanup(self.reviews.close)
        self.config = {"db": self.db, "status_path": self.status,
                       "lock_path": self.lock, "interval_seconds": 1,
                       "systemd_units": {"receiver": "doudou-receiver.service"}}

    def chat(self, event_id, text, *, age=0, source_age=0):
        now = time.time()
        event = {"type": "chat", "text": text, "sender": "Huhabubu"}
        if source_age:
            event["received_at"] = now - source_age
        self.assertEqual(self.inbox.accept(event_id, event), "accepted")
        if age:
            self.inbox.conn.execute(
                "UPDATE events SET received_at=? WHERE id=?",
                (now - age, event_id),
            )
            self.inbox.conn.commit()
        return self.inbox.conn.execute(
            "SELECT seq FROM events WHERE id=?", (event_id,)
        ).fetchone()[0]

    def test_stale_task_cannot_be_acknowledged_by_new_chat(self):
        self.chat("old_mine", "去挖20个铁矿", age=150)
        fresh_seq = self.chat("new_hello", "你在吗")
        with self.assertRaisesRegex(ReplyError, "pending_chat_requires_review"):
            self.reply.prepare(
                "new_hello", fresh_seq, "在", routes=[
                    {"event_id": "old_mine", "kind": "task"},
                    {"event_id": "new_hello", "kind": "chat"},
                ], authorized_sender="Huhabubu")
        self.assertEqual(len(self.inbox.pending()), 2)
        self.assertEqual(self.reviews.sweep(), 1)
        self.assertEqual(self.reviews.sweep(), 0)
        self.assertEqual(self.reviews.pending_count(), 1)
        stale = self.reviews.list_reviews()[0]
        self.assertEqual(stale["text"], "去挖20个铁矿")
        self.assertEqual(stale["reason"], "inbox_age_exceeded")
        self.assertEqual(self.inbox.pending()[0]["id"], "new_hello")
        self.assertEqual(self.reply.prepare(
            "new_hello", fresh_seq, "在", routes=[
                {"event_id": "new_hello", "kind": "chat"}
            ], authorized_sender="Huhabubu"), "queued")
        self.assertEqual(self.reply.stats(), {"queued": 1})
        self.assertEqual(self.reply.conn.execute(
            "SELECT count(*) FROM task_handoffs"
        ).fetchone()[0], 0)

    def test_restarted_and_source_stale_are_reviewed_not_executed(self):
        self.chat("delayed", "跟我走", source_age=300)
        self.inbox.accept("replayed", {
            "type": "chat", "text": "采集木头", "sender": "Huhabubu",
            "replayed_after_restart": True
        })
        self.assertEqual(self.reviews.sweep(), 2)
        reasons = {r["event_id"]: r["reason"]
                   for r in self.reviews.list_reviews()}
        self.assertEqual(reasons["delayed"], "source_age_exceeded")
        self.assertEqual(reasons["replayed"], "replayed_after_restart")
        self.assertEqual(self.reviews.close_review(
            "delayed", note="过时指令，需要玩家重新确认"), "reviewed")
        with self.assertRaisesRegex(ReviewError, "review_not_pending"):
            self.reviews.close_review("delayed", note="再次结束")
        self.assertEqual(self.reviews.pending_count(), 1)

    def test_nonchat_events_and_fresh_chat_stay_pending(self):
        self.inbox.accept("danger", {"type": "death"})
        self.chat("fresh", "早上好")
        self.chat("old", "昨天的事", age=121)
        self.assertEqual(self.reviews.sweep(), 1)
        self.assertEqual([x["id"] for x in self.inbox.pending()],
                         ["danger", "fresh"])

    def test_coordinator_status_has_metadata_without_player_text(self):
        self.chat("old", "私密聊天内容", age=123)
        self.chat("new", "还有新消息")
        data = sample(self.config, service_check=lambda _: "unobservable")
        self.assertEqual(data["reviews"]["newly_quarantined"], 1)
        self.assertEqual(data["reviews"]["needs_review"], 1)
        self.assertEqual(data["inbox"]["pending_chat"], 1)
        self.assertEqual(data["main_agent"]["availability"],
                         "unobservable_without_host_api")
        self.assertEqual(data["services"]["receiver"], "unobservable")
        self.assertNotIn("私密聊天内容", Path(self.status).read_text())
        self.assertNotIn("还有新消息", Path(self.status).read_text())
        self.assertEqual(sample(self.config, service_check=lambda _: "active")[
            "reviews"]["newly_quarantined"], 0)

    def test_single_coordinator_lock_denies_second_owner(self):
        Path(self.lock).touch(mode=0o600)
        with open(self.lock) as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(CoordinatorError,
                                        "coordinator_already_running"):
                monitor(self.config, once=True)
        self.assertFalse(Path(self.status).exists())


if __name__ == "__main__":
    unittest.main()
