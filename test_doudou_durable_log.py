import json
import os
from pathlib import Path
import tempfile
import unittest

from minecraft_doudou.durable_log import DurableChatTail
from minecraft_doudou.event_relay import EventOutbox


class DurableLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "latest.log"
        self.db = Path(self.temp.name) / "outbox.sqlite"
        self.log.write_bytes(b"[CHAT] <Huhabubu> old before enrollment\n")

    def open(self):
        out = EventOutbox(self.db)
        return out, DurableChatTail(self.log, "Huhabubu", out)

    def append(self, data):
        with self.log.open("ab") as stream:
            stream.write(data)

    def queued(self, out):
        return [json.loads(row[0]) for row in out.conn.execute(
            "SELECT payload FROM outbox ORDER BY rowid").fetchall()]

    def test_first_attach_and_crash_safe_resume(self):
        out, reader = self.open()
        self.assertEqual(reader.poll(), 0)  # Old history not replayed.
        self.append(b"[12:34:56] [CHAT] <Huhabubu> first\n")
        self.assertEqual(reader.poll(), 1)
        first = self.queued(out)[0]
        self.assertEqual(first["text"], "first")
        self.assertEqual(first["source_log_clock"], "12:34:56")
        self.assertFalse(first["replayed_after_restart"])
        out.close()
        self.append(b"[12:34:57] [CHAT] <Huhabubu> missed while down\n")
        out, reader = self.open()
        self.assertEqual(reader.poll(), 1)
        events = self.queued(out)
        self.assertEqual([x["text"] for x in events], ["first", "missed while down"])
        self.assertTrue(events[-1]["replayed_after_restart"])
        self.assertEqual(reader.poll(), 0)  # No duplicate.
        out.close()

    def test_partial_line_and_ansi_sender_filter(self):
        out, reader = self.open()
        reader.poll()
        self.append(b"[CHAT] \x1b[m<Huhabubu> \xe4\xbd\xa0")
        self.assertEqual(reader.poll(), 0)
        self.append(b"\xe5\xa5\xbd\n[CHAT] <OtherPlayer> skip\n")
        self.assertEqual(reader.poll(), 1)
        self.assertEqual(self.queued(out)[0]["text"], "你好")
        out.close()

    def test_rotation_emits_gap_and_reads_new_file(self):
        out, reader = self.open()
        reader.poll()
        self.append(b"[CHAT] <Huhabubu> before rotate\n")
        self.assertEqual(reader.poll(), 1)
        self.log.rename(Path(self.temp.name) / "archived.log")
        self.log.write_bytes(b"[CHAT] <Huhabubu> after rotate\n")
        self.assertEqual(reader.poll(), 2)
        events = self.queued(out)
        self.assertEqual(events[-2]["type"], "event_gap")
        self.assertEqual(events[-1]["text"], "after rotate")
        out.close()

    def test_outbox_full_does_not_advance_offset(self):
        out, reader = self.open()
        reader.poll()
        self.append(b"[CHAT] <Huhabubu> protect this\n")
        # Inject enough pending records in a single transaction for this test.
        with out.conn:
            out.conn.executemany(
                "INSERT INTO outbox(id,payload) VALUES(?,?)",
                ((f"id_{i}", "{}") for i in range(10000)),
            )
        before = out.conn.execute("SELECT offset FROM log_cursor").fetchone()[0]
        with self.assertRaises(RuntimeError):
            reader.poll()
        self.assertEqual(out.conn.execute("SELECT offset FROM log_cursor").fetchone()[0],
                         before)
        with out.conn:
            out.conn.execute("DELETE FROM outbox")
        self.assertEqual(reader.poll(), 1)
        self.assertEqual(self.queued(out)[0]["text"], "protect this")
        out.close()


if __name__ == "__main__":
    unittest.main()
