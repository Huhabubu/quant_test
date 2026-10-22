"""Deterministic regressions for the child_once fast-path fetch.

Fixtures only: a real on-disk inbox with the production events schema and
a real state.json file. No game, no network, no model.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from minecraft_doudou import child_once

SCHEMA = (
    "CREATE TABLE events ("
    "seq INTEGER PRIMARY KEY, id TEXT NOT NULL, kind TEXT NOT NULL, "
    "payload TEXT NOT NULL, received_at REAL NOT NULL, handled_at REAL)"
)


def _make_inbox(path, rows):
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    for seq, event_id, sender, text, received_at, handled_at in rows:
        payload = json.dumps({
            "type": "chat", "sender": sender, "text": text,
            "source_log_clock": "12:00:00", "received_at": received_at,
        })
        db.execute(
            "INSERT INTO events VALUES (?, ?, 'chat', ?, ?, ?)",
            (seq, event_id, payload, received_at, handled_at),
        )
    db.commit()
    db.close()


def _make_state(path, **overrides):
    state = {
        "in_world": True, "health": 16.833332, "hunger": 11,
        "clock_time": "23:27", "x": -142.3, "y": 67.0, "z": 90.9,
    }
    state.update(overrides)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


class ChildOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "inbox.sqlite")
        self.state_path = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_greeting_peek_returns_newest_unhandled(self):
        _make_inbox(self.db_path, [
            (1, "old-id", "Huhabubu", "earlier", 100.0, 101.0),
            (2, "greet-id", "Huhabubu", "hello doudou", 200.0, None),
        ])
        _make_state(self.state_path)
        payload = child_once.run(self.db_path, self.state_path, "Huhabubu")
        self.assertEqual(payload["chat"]["event_id"], "greet-id")
        self.assertEqual(payload["chat"]["seq"], 2)
        self.assertEqual(payload["chat"]["text"], "hello doudou")

    def test_status_snapshot_carries_citable_fields(self):
        _make_inbox(self.db_path, [
            (1, "q-id", "Huhabubu", "are you hungry", 100.0, None),
        ])
        _make_state(self.state_path)
        payload = child_once.run(self.db_path, self.state_path, "Huhabubu")
        state = payload["state"]
        # A hunger/time answer may cite exactly these fixture values.
        self.assertEqual(state["food"], 11)
        self.assertEqual(state["hp"], 16.8)
        self.assertEqual(state["clock"], "23:27")
        self.assertEqual(state["pos"], [-142, 67, 91])

    def test_handled_events_are_never_returned_again(self):
        _make_inbox(self.db_path, [
            (1, "done-id", "Huhabubu", "already answered", 100.0, 150.0),
        ])
        _make_state(self.state_path)
        payload = child_once.run(self.db_path, self.state_path, "Huhabubu")
        self.assertIsNone(payload["chat"])

    def test_peek_does_not_mutate_inbox(self):
        _make_inbox(self.db_path, [
            (1, "x-id", "Huhabubu", "hi", 100.0, None),
        ])
        _make_state(self.state_path)
        child_once.run(self.db_path, self.state_path, "Huhabubu")
        db = sqlite3.connect(self.db_path)
        handled = db.execute(
            "SELECT handled_at FROM events WHERE id = 'x-id'").fetchone()[0]
        db.close()
        self.assertIsNone(handled)

    def test_other_senders_are_ignored(self):
        _make_inbox(self.db_path, [
            (1, "s-id", "SekaiSaionji", "not for the owner path", 100.0, None),
        ])
        _make_state(self.state_path)
        payload = child_once.run(self.db_path, self.state_path, "Huhabubu")
        self.assertIsNone(payload["chat"])

    def test_missing_state_file_degrades_without_crash(self):
        _make_inbox(self.db_path, [
            (1, "g-id", "Huhabubu", "hi", 100.0, None),
        ])
        payload = child_once.run(
            self.db_path,
            os.path.join(self.tmp.name, "no-such-state.json"),
            "Huhabubu",
        )
        self.assertEqual(payload["state"], {"in_world": False})
        self.assertEqual(payload["chat"]["event_id"], "g-id")

    def test_out_of_world_state_degrades(self):
        _make_inbox(self.db_path, [
            (1, "g-id", "Huhabubu", "hi", 100.0, None),
        ])
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({"in_world": False}, handle)
        payload = child_once.run(self.db_path, self.state_path, "Huhabubu")
        self.assertEqual(payload["state"], {"in_world": False})


if __name__ == "__main__":
    unittest.main()
