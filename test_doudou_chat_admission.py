"""Focused end-to-end chat admission cases: no model and no game."""
import json
from pathlib import Path
import tempfile
import unittest

from minecraft_doudou.chat_policy import admitted_chat
from minecraft_doudou.chat_replies import ReplyError, ReplyQueue
from minecraft_doudou.child_once import fetch_chats
from minecraft_doudou.durable_log import DurableChatTail
from minecraft_doudou.event_receiver import EventInbox
from minecraft_doudou.event_relay import EventOutbox
from minecraft_doudou.events import parse_chat


class ChatAdmissionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = str(self.root / "inbox.sqlite")
        self.inbox = EventInbox(self.db, chat_owner="Huhabubu")
        self.addCleanup(self.inbox.close)
        self.queue = ReplyQueue(self.db)
        self.addCleanup(self.queue.close)

    def test_owner_all_messages_others_must_mention_and_self_is_excluded(self):
        self.assertTrue(admitted_chat("Huhabubu", "来找我"))
        self.assertTrue(admitted_chat("SekaiSaionji", "DouDou 来聊天"))
        self.assertTrue(admitted_chat("kurumi", "豆豆，你在哪里？"))
        self.assertTrue(admitted_chat("kurumi", "dou dou，请回答"))
        self.assertTrue(admitted_chat("kurumi", "dou douu，早上好"))
        self.assertTrue(admitted_chat("kurumi", "DoU　DoU，还在吗"))
        self.assertFalse(admitted_chat("SekaiSaionji", "我去挖矿"))
        self.assertFalse(admitted_chat("DouDou", "豆豆正在工作"))
        self.assertFalse(admitted_chat("Fake\nName", "豆豆"))
        self.assertIsNone(parse_chat("[CHAT] <kurumi> 普通对话", "*",
                                     owner="Huhabubu"))
        self.assertEqual(parse_chat(
            "[CHAT] <kurumi> dOuDoU 你好", "*", owner="Huhabubu"
        )["sender"], "kurumi")

    def test_receiver_drops_unaddressed_chat_before_sqlite_and_keeps_events(self):
        self.assertEqual(self.inbox.accept(
            "unrelated", {"type": "chat", "sender": "kurumi",
                          "text": "我先去休息"}), "ignored")
        self.assertEqual(self.inbox.accept(
            "owner", {"type": "chat", "sender": "Huhabubu",
                      "text": "直接过来"}), "accepted")
        self.assertEqual(self.inbox.accept(
            "addressed", {"type": "chat", "sender": "kurumi",
                          "text": "豆豆能听到吗"}), "accepted")
        self.assertEqual(self.inbox.accept(
            "danger", {"type": "death"}), "accepted")
        self.assertEqual([x["id"] for x in self.inbox.pending()],
                         ["owner", "addressed", "danger"])
        self.assertIsNone(self.inbox.conn.execute(
            "SELECT 1 FROM events WHERE id='unrelated'").fetchone())

    def test_main_reads_mixed_authors_nonowner_is_chat_only(self):
        self.inbox.accept("a", {"type": "chat", "sender": "Huhabubu",
                                "text": "跟着我"})
        self.inbox.accept("b", {"type": "chat", "sender": "kurumi",
                                "text": "doudou 你好"})
        chats = fetch_chats(self.db, "Huhabubu")
        self.assertEqual([(x["sender"], x["is_owner"]) for x in chats],
                         [("Huhabubu", True), ("kurumi", False)])
        seq = chats[-1]["seq"]
        with self.assertRaisesRegex(ReplyError, "non_owner_task_forbidden"):
            self.queue.prepare("b", seq, "收到", routes=[
                {"event_id": "a", "kind": "chat"},
                {"event_id": "b", "kind": "task", "lane": "game"},
            ], authorized_sender="Huhabubu")
        self.assertEqual(len(self.inbox.pending()), 2)
        self.assertEqual(self.queue.prepare(
            "b", seq, "你好", routes=[
                {"event_id": "a", "kind": "task"},
                {"event_id": "b", "kind": "chat"},
            ], authorized_sender="Huhabubu"), "queued")
        tasks = self.queue.conn.execute(
            "SELECT event_id,instruction FROM task_handoffs"
        ).fetchall()
        self.assertEqual(tasks, [("a", "跟着我")])

    def test_single_wildcard_durable_tail_reads_all_players_once(self):
        log = self.root / "latest.log"
        log.write_text("[CHAT] <Huhabubu> old\n", encoding="utf-8")
        outbox = EventOutbox(self.root / "outbox.sqlite")
        self.addCleanup(outbox.close)
        tail = DurableChatTail(log, "*", outbox, owner="Huhabubu")
        self.assertEqual(tail.poll(), 0)
        with log.open("a", encoding="utf-8") as fp:
            fp.write("[CHAT] <SomeoneNew> hi\n"
                     "[CHAT] <SomeoneNew> dOuDoU 你好\n"
                     "[CHAT] <Huhabubu> 快来\n"
                     "[CHAT] <DouDou> 豆豆正在回复\n")
        self.assertEqual(tail.poll(), 2)
        rows = [json.loads(r[0]) for r in outbox.conn.execute(
            "SELECT payload FROM outbox ORDER BY rowid"
        ).fetchall()]
        self.assertEqual([r["sender"] for r in rows],
                         ["SomeoneNew", "Huhabubu"])
        self.assertEqual(tail.poll(), 0)


if __name__ == "__main__":
    unittest.main()
