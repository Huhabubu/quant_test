"""One-writer M1 fast sender with independent game-log verification.

Send first, never wait for M1's nonexistent say OK. Only read the local
Minecraft log after the dispatch, not M1 async chat frames. This module
does NOT spawn/restore Muse or bypass the single MAIN intent owner.

RUN ONLY IN PLACE OF (never beside) the legacy chat_replies sender.
"""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from .chat_replies import ReplyQueue
from .durable_log import LOG_CLOCK
from .events import CHAT_LINE, ANSI_CSI
from .log_reconcile import Attempt, LogEntry, reconcile
from .m1_chat import submit_m1_chat_fast

SPEAKER = "DouDou"
MAX_LINE = 8192
MAX_BYTES_PER_ATTEMPT = 65536
MAX_ATTEMPTS_PER_PASS = 24


class QuickSenderError(ValueError):
    pass


class LogWriteOff:
    def __init__(self, conn, log):
        self.conn = conn
        self.log = Path(log)
        if not self.log.is_absolute():
            raise QuickSenderError("game_log_must_be_absolute")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_send_attempts ("
            "event_id TEXT PRIMARY KEY, "
            "log_dev INTEGER NOT NULL, log_ino INTEGER NOT NULL, "
            "log_floor INTEGER NOT NULL, scan_offset INTEGER NOT NULL, "
            "attempt_start REAL NOT NULL, evidence_offset INTEGER, "
            "evidence_at REAL, last_reason TEXT NOT NULL DEFAULT 'pending',"
            "UNIQUE(log_dev, log_ino, evidence_offset))"
        )
        self.conn.commit()

    def claim(self, conn, eid, text, started):
        """Pre-wire snapshot recorded atomically with queue's sending claim.

        Never capture floor *after* TCP submission: an immediate echo could
        be included in the historical region and become unprovable.
        """
        with self.log.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if stat.st_size:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    raise QuickSenderError("game_log_last_line_incomplete")
        conn.execute(
            "INSERT INTO chat_send_attempts "
            "(event_id,log_dev,log_ino,log_floor,scan_offset,attempt_start)"
            " VALUES(?,?,?,?,?,?)",
            (eid, stat.st_dev, stat.st_ino, stat.st_size, stat.st_size, started),
        )

    @staticmethod
    def _log_time(line, reference):
        match = LOG_CLOCK.match(line)
        if match is None:
            return None
        try:
            hh, mm, ss = (int(v) for v in match.group(1).split(":"))
            day = datetime.fromtimestamp(reference).replace(
                hour=hh, minute=mm, second=ss, microsecond=0)
            candidates = [day + timedelta(days=delta) for delta in (-1, 0, 1)]
            return min((d.timestamp() for d in candidates),
                       key=lambda t: abs(t - reference))
        except (ValueError, OverflowError, OSError):
            return None

    def scan(self, *, grace_s=30.0):
        """Read bounded log chunks with durable per-attempt offsets.

        The floor/generation/text/sender/time check is strict; each physical
        log line is uniquely claimable by at most one attempt. A failed
        search never queues a retransmission or promotes an M1 self-echo.
        """
        if not 0 < grace_s <= 300:
            raise QuickSenderError("invalid_grace")
        rows = self.conn.execute(
            "SELECT a.event_id,a.log_dev,a.log_ino,a.log_floor,"
            "a.scan_offset,a.attempt_start,c.finished_at,c.reply_text,c.state "
            "FROM chat_send_attempts a JOIN chat_replies c USING(event_id) "
            "WHERE c.state IN ('sent_unverified','sending') "
            "ORDER BY a.attempt_start,a.event_id LIMIT ?",
            (MAX_ATTEMPTS_PER_PASS,),
        ).fetchall()
        if not rows:
            return {"pending_scan": 0, "confirmed": 0, "rotated": 0}
        try:
            stream = self.log.open("rb")
        except OSError:
            return {"pending_scan": len(rows), "confirmed": 0,
                    "rotated": 0, "log": "unavailable"}
        confirmed = rotated = 0
        with stream:
            stat = os.fstat(stream.fileno())
            for eid, dev, ino, floor, cursor, start, end, text, state in rows:
                if (dev, ino) != (stat.st_dev, stat.st_ino) or stat.st_size < cursor:
                    # Original region is gone; NEVER confirm from another
                    # file generation and NEVER replay a possible send.
                    with self.conn:
                        self.conn.execute(
                            "UPDATE chat_replies SET state='uncertain',"
                            "result_note='game_log_generation_lost',finished_at=? "
                            "WHERE event_id=? AND state IN ('sent_unverified','sending')",
                            (time.time(), eid),
                        )
                        self.conn.execute(
                            "UPDATE chat_send_attempts SET last_reason='log_rotated' "
                            "WHERE event_id=?", (eid,),
                        )
                    rotated += 1
                    continue
                stream.seek(cursor)
                attempt = Attempt(eid, text, SPEAKER, start,
                                  end if end is not None else start + 6, floor)
                read = 0
                while read < MAX_BYTES_PER_ATTEMPT:
                    before = stream.tell()
                    line = stream.readline(min(MAX_LINE + 1,
                                               MAX_BYTES_PER_ATTEMPT - read))
                    if not line or not line.endswith(b"\n"):
                        # Leave partial line in place for the next scan.
                        stream.seek(before)
                        break
                    read += len(line)
                    after = stream.tell()
                    if len(line) > MAX_LINE:
                        continue
                    decoded = line.decode("utf-8", errors="replace")
                    match = CHAT_LINE.search(ANSI_CSI.sub("", decoded))
                    stamp = self._log_time(decoded, start)
                    if not match or stamp is None:
                        continue
                    evidence = LogEntry(after, match.group("sender"),
                                        match.group("text"), stamp)
                    verdict = reconcile(attempt, [evidence],
                                        grace_s=grace_s)
                    if not verdict.confirmed:
                        continue
                    try:
                        with self.conn:
                            # UNIQUE(dev,ino,evidence_offset) prevents one
                            # log line from confirming two identical replies.
                            self.conn.execute(
                                "UPDATE chat_send_attempts SET "
                                "evidence_offset=?,evidence_at=?,"
                                "last_reason='confirmed',scan_offset=? "
                                "WHERE event_id=? AND evidence_offset IS NULL",
                                (after, stamp, after, eid),
                            )
                            self.conn.execute(
                                "UPDATE chat_replies SET state='game_log_confirmed',"
                                "result_note='exact_game_log_evidence',finished_at=? "
                                "WHERE event_id=? AND state IN "
                                "('sent_unverified','sending')",
                                (time.time(), eid),
                            )
                        confirmed += 1
                        break
                    except sqlite3.IntegrityError:
                        # Evidence already consumed by an earlier attempt.
                        # Transaction is rolled back; continue to next line.
                        continue
                else:
                    after = stream.tell()
                with self.conn:
                    self.conn.execute(
                        "UPDATE chat_send_attempts SET scan_offset=? "
                        "WHERE event_id=? AND evidence_offset IS NULL",
                        (stream.tell(), eid),
                    )
        return {"pending_scan": len(rows), "confirmed": confirmed,
                "rotated": rotated}


def run(db, log, *, once=False, poll_seconds=0.2):
    if not 0.05 <= poll_seconds <= 5:
        raise QuickSenderError("invalid_poll_seconds")
    queue = ReplyQueue(db)
    try:
        evidence = LogWriteOff(queue.conn, log)
        while True:
            # Always exhaust the send queue before reading the log. Large
            # files are bounded by scan(), which cannot steal a model turn.
            result = queue.send_once(submit_m1_chat_fast,
                                     on_claim=evidence.claim)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            else:
                evidence.scan()
            if once:
                return result
            time.sleep(poll_seconds if result is None else 0)
    finally:
        queue.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="DouDou fast M1 say + log writeoff")
    parser.add_argument("--db", required=True)
    parser.add_argument("--log", required=True,
                        help="absolute client latest.log used for the game-visible echo")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    return run(args.db, args.log, once=args.once)


if __name__ == "__main__":
    try:
        sys.exit(0 if main() is None else 0)
    except (QuickSenderError, sqlite3.Error, OSError) as exc:
        print("fast_sender_error:" + type(exc).__name__, file=sys.stderr)
        sys.exit(2)
