"""Crash-safe Minecraft log reader using the relay's existing SQLite outbox.

The file position and parsed events commit in the same SQLite transaction.
A replaced/truncated log is explicitly recorded as a gap; unavailable
rotated history cannot be reconstructed from latest.log alone.
"""
import os
from pathlib import Path
import re
import time
from uuid import uuid4
import json

from .events import parse_chat

LOG_CLOCK = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")
MAX_LOG_LINE = 65536


class DurableChatTail:
    def __init__(self, path, sender, outbox):
        self.path = Path(path)
        self.sender = sender
        self.outbox = outbox
        self.source = str(self.path.resolve())
        self.recovery_target = None
        outbox.conn.execute(
            "CREATE TABLE IF NOT EXISTS log_cursor ("
            "source TEXT PRIMARY KEY, dev INTEGER NOT NULL, "
            "ino INTEGER NOT NULL, offset INTEGER NOT NULL, updated_at REAL NOT NULL)"
        )
        outbox.conn.commit()

    def poll(self, max_lines=512):
        try:
            stream = self.path.open("rb")
        except OSError:
            return 0
        with stream:
            stat = os.fstat(stream.fileno())
            signature = (stat.st_dev, stat.st_ino)
            saved = self.outbox.conn.execute(
                "SELECT dev,ino,offset FROM log_cursor WHERE source=?",
                (self.source,),
            ).fetchone()
            if saved is None:
                # First-ever enrollment: ignore historical chat, start at EOF.
                with self.outbox.conn:
                    self._checkpoint(signature, stat.st_size)
                return 0

            changed = signature != tuple(saved[:2])
            truncated = not changed and stat.st_size < saved[2]
            offset = 0 if changed or truncated else saved[2]
            if self.recovery_target is None:
                # Anything already present at first poll is historical replay.
                self.recovery_target = stat.st_size
            stream.seek(offset)
            events = []
            complete_offset = offset
            for _ in range(max_lines):
                position = stream.tell()
                raw = stream.readline(MAX_LOG_LINE + 1)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    if len(raw) <= MAX_LOG_LINE:
                        break  # Keep the partial line until it is complete.
                    # Oversized line: consume to the next newline and disclose a gap.
                    while raw and not raw.endswith(b"\n"):
                        raw = stream.readline(MAX_LOG_LINE + 1)
                    if not raw:
                        break
                    complete_offset = stream.tell()
                    events.append({"type": "event_gap",
                                   "reason": "oversized_log_line", "at": position})
                    continue
                complete_offset = stream.tell()
                line = raw.decode("utf-8", errors="replace")
                event = parse_chat(line, self.sender)
                if event is None:
                    continue
                source_clock = LOG_CLOCK.match(line)
                event["source_log_clock"] = source_clock.group(1) if source_clock else None
                event["replayed_after_restart"] = complete_offset <= self.recovery_target
                event["received_at"] = time.time()
                events.append(event)

            if changed or truncated:
                events.insert(0, {
                    "type": "event_gap",
                    "reason": "log_replaced_or_truncated",
                    "previous_offset": saved[2],
                })
            if complete_offset == saved[2] and not events:
                return 0
            with self.outbox.conn:
                n = self.outbox.conn.execute(
                    "SELECT COUNT(*) FROM outbox"
                ).fetchone()[0]
                if n + len(events) > 10000:
                    raise RuntimeError("event_outbox_full_stop_instead_of_dropping_silently")
                for event in events:
                    self.outbox.conn.execute(
                        "INSERT INTO outbox(id,payload) VALUES(?,?)",
                        (uuid4().hex, json.dumps(event, ensure_ascii=False,
                                                 separators=(",", ":"))),
                    )
                self._checkpoint(signature, complete_offset)
            return len(events)

    def _checkpoint(self, signature, offset):
        self.outbox.conn.execute(
            "INSERT INTO log_cursor(source,dev,ino,offset,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
            "dev=excluded.dev,ino=excluded.ino,offset=excluded.offset,"
            "updated_at=excluded.updated_at",
            (self.source, signature[0], signature[1], offset, time.time()),
        )
