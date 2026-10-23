"""Regressions for main-agent chat ownership and durable child-task handoffs.

Only fixture SQLite + real production modules; no Minecraft, network or model.
"""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import threading
import unittest

from minecraft_doudou.chat_replies import ReplyError, ReplyQueue, main as reply_main
from minecraft_doudou.event_receiver import (
    CHAT_TYPES, NONCHAT_TYPES, EventInbox, arm,
)
from minecraft_doudou.task_handoffs import (
    TaskError, TaskQueue, main as task_main,
)


class MainAgentTaskHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "inbox.sqlite")
        self.inbox = EventInbox(self.db)
        self.addCleanup(self.inbox.close)
        self.reply = ReplyQueue(self.db)
        self.addCleanup(self.reply.close)
        self.tasks = TaskQueue(self.db)
        self.addCleanup(self.tasks.close)

    def add(self, eid, text, sender="Huhabubu"):
        result = self.inbox.accept(eid, {
            "type": "chat", "sender": sender, "text": text
        })
        self.assertEqual(result, "accepted")
        return self.inbox.pending()[-1]["seq"]

    def test_mixed_chat_and_task_batch_is_atomic_and_not_silent(self):
        self.inbox.accept("death1", {"type": "death"})
        self.add("hi", "你好")
        seq = self.add("mine", "去挖20块铁")
        routes = [
            {"event_id": "hi", "kind": "chat"},
            {"event_id": "mine", "kind": "task", "lane": "game"},
        ]
        self.assertEqual(self.reply.prepare(
            "mine", seq, "收到，正在安排采矿任务", routes=routes,
            authorized_sender="Huhabubu"), "queued")
        self.assertEqual([e["id"] for e in self.inbox.pending()], ["death1"])
        pending = self.tasks.list_tasks()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_id"], "mine")
        self.assertEqual(pending[0]["instruction"], "去挖20块铁")
        self.assertEqual(pending[0]["state"], "queued")
        self.assertEqual(self.reply.stats()["queued"], 1)

    def test_newer_chat_missing_or_reordered_cannot_be_swept(self):
        self.add("a", "你好")
        seq = self.add("b", "去找我")
        for bad in (
            [{"event_id": "b", "kind": "task"}],
            [{"event_id": "b", "kind": "task"},
             {"event_id": "a", "kind": "chat"}],
            [{"event_id": "a", "kind": "chat"},
             {"event_id": "a", "kind": "task"}],
        ):
            with self.assertRaisesRegex(ReplyError, "routes_must_cover"):
                self.reply.prepare("b", seq, "收到", routes=bad,
                                   authorized_sender="Huhabubu")
        self.assertEqual(len(self.inbox.pending()), 2)
        self.assertEqual(self.tasks.list_tasks(), [])
        self.assertEqual(self.reply.stats(), {})

    def test_sender_and_invalid_kind_fail_closed_without_ack(self):
        self.add("a", "hi")
        seq = self.add("b", "攻击玩家", sender="OtherPerson")
        with self.assertRaisesRegex(ReplyError, "unauthorized_chat"):
            self.reply.prepare("b", seq, "收到", routes=[
                {"event_id": "a", "kind": "chat"},
                {"event_id": "b", "kind": "task"},
            ], authorized_sender="Huhabubu")
        with self.assertRaisesRegex(ReplyError, "invalid_chat_route"):
            self.reply.prepare("b", seq, "收到", routes=[
                {"event_id": "a", "kind": "execute_now"},
                {"event_id": "b", "kind": "chat"},
            ], authorized_sender="Huhabubu")
        self.assertEqual(len(self.inbox.pending()), 2)
        self.assertEqual(self.tasks.list_tasks(), [])

    def test_legacy_prepare_no_longer_acks_nonchat_events(self):
        self.inbox.accept("death", {"type": "death"})
        seq = self.add("hello", "早上好")
        self.assertEqual(self.reply.prepare("hello", seq, "早上好"), "queued")
        self.assertEqual([e["id"] for e in self.inbox.pending()], ["death"])

    def test_route_replay_does_not_double_enqueue_or_send(self):
        seq = self.add("build", "帮我建房子")
        routes = [{"event_id": "build", "kind": "task"}]
        self.assertEqual(self.reply.prepare(
            "build", seq, "收到", routes=routes,
            authorized_sender="Huhabubu"), "queued")
        self.assertEqual(self.reply.prepare(
            "build", seq, "收到", routes=routes,
            authorized_sender="Huhabubu"), "duplicate")
        self.assertEqual(self.reply.prepare(
            "build", seq, "另一条回复", routes=routes,
            authorized_sender="Huhabubu"), "conflict")
        self.assertEqual(len(self.tasks.list_tasks()), 1)
        self.assertEqual(self.reply.stats()["queued"], 1)

    def test_legacy_reply_cannot_be_reinterpreted_as_routed_task(self):
        seq = self.add("legacy", "去挖矿")
        self.assertEqual(self.reply.prepare("legacy", seq, "收到"), "queued")
        with self.assertRaisesRegex(ReplyError, "duplicate_without_matching_handoff"):
            self.reply.prepare("legacy", seq, "收到", routes=[
                {"event_id": "legacy", "kind": "task"}
            ], authorized_sender="Huhabubu")
        self.assertEqual(self.tasks.list_tasks(), [])
        self.assertEqual(self.reply.stats()["queued"], 1)

    def test_game_single_owner_research_can_run_in_parallel(self):
        for eid, text, lane in (
            ("g1", "采矿", "game"),
            ("r1", "查资料", "research"),
            ("g2", "建房", "game"),
            ("r2", "分析路线", "research"),
        ):
            seq = self.add(eid, text)
            self.reply.prepare(eid, seq, "收到", routes=[
                {"event_id": eid, "kind": "task", "lane": lane}
            ], authorized_sender="Huhabubu")
        a = self.tasks.claim("game-worker")
        self.assertEqual(a["event_id"], "g1")
        b = self.tasks.claim("research-worker")
        self.assertEqual(b["event_id"], "r1")
        c = self.tasks.claim("research-worker-2")
        self.assertEqual(c["event_id"], "r2")
        self.assertIsNone(self.tasks.claim("another-game-worker"))
        self.assertEqual(self.tasks.finish("g1", "game-worker",
                                           "completed", "已经交付"), "completed")
        self.assertEqual(self.tasks.claim("next-game-worker")["event_id"], "g2")

    def test_claim_race_can_only_claim_one_game_task(self):
        seq = self.add("g1", "砍树")
        self.reply.prepare("g1", seq, "收到", routes=[
            {"event_id": "g1", "kind": "task"}
        ], authorized_sender="Huhabubu")
        barrier = threading.Barrier(3)
        results = []

        def worker(i):
            q = TaskQueue(self.db)
            try:
                barrier.wait(timeout=3)
                results.append(q.claim("worker-" + str(i)))
            finally:
                q.close()

        a = threading.Thread(target=worker, args=(1,))
        b = threading.Thread(target=worker, args=(2,))
        a.start()
        b.start()
        barrier.wait(timeout=3)
        a.join(timeout=3)
        b.join(timeout=3)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(sum(t is not None for t in results), 1)

    def test_claim_survives_restart_without_reexecution(self):
        seq = self.add("go", "找我")
        self.reply.prepare("go", seq, "收到", routes=[
            {"event_id": "go", "kind": "task"}
        ], authorized_sender="Huhabubu")
        self.tasks.claim("worker-A")
        restarted = TaskQueue(self.db)
        try:
            self.assertIsNone(restarted.claim("worker-B"))
            self.assertEqual(restarted.list_tasks()[0]["state"], "claimed")
            with self.assertRaisesRegex(TaskError, "task_not_claimed"):
                restarted.finish("go", "worker-B", "completed")
            restarted.finish("go", "worker-A", "failed", "游戏断线，待人工确认")
            self.assertIsNone(restarted.claim("worker-C"))
        finally:
            restarted.close()

    def test_urgent_task_precedes_normal_unclaimed(self):
        for eid, priority in (("regular", "normal"), ("stop", "urgent")):
            seq = self.add(eid, "停止当前任务" if eid == "stop" else "去挖铁")
            self.reply.prepare(eid, seq, "收到", routes=[{
                "event_id": eid, "kind": "task", "priority": priority
            }], authorized_sender="Huhabubu")
        self.assertEqual(self.tasks.claim("main")["event_id"], "stop")

    def test_arm_chat_and_nonchat_filter(self):
        self.add("chat", "早上好")
        self.assertEqual(arm(self.db, timeout=0.03, poll_seconds=0.02,
                             types=CHAT_TYPES), 0)
        self.assertEqual(arm(self.db, timeout=0.03, poll_seconds=0.02,
                             types=NONCHAT_TYPES), 124)
        self.inbox.accept("danger", {"type": "death"})
        self.assertEqual(arm(self.db, timeout=0.03, poll_seconds=0.02,
                             types=NONCHAT_TYPES), 0)

    def test_cli_routed_reply_and_task_claim(self):
        seq = self.add("g1", "找床睡觉")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(reply_main([
                "prepare-routed", "--db", self.db, "--event-id", "g1",
                "--through-seq", str(seq), "--text", "收到",
                "--routes-json", json.dumps([{
                    "event_id": "g1", "kind": "task"
                }]),
            ]), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "queued")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(task_main([
                "claim", "--db", self.db, "--worker", "mine-agent"
            ]), 0)
        self.assertEqual(json.loads(out.getvalue())["task"]["event_id"], "g1")


if __name__ == "__main__":
    unittest.main()
