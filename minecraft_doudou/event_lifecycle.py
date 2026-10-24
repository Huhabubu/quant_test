"""Explicit quarantine/review lifecycle for stale inbound Minecraft chat.

A stale message must NEVER be replied to, silently swallowed by a newer
prepare-routed batch, or interpreted as a newly authorized game task.

SQLite persists the full original event; quarantine removes it from the
*active* inbox and records a durable needs_review obligation. A human/main
agent must explicitly close that obligation; this module never executes or
replays a task.
"""
import argparse
import json
import sqlite3
import sys
import time

MAX_AGE_SECONDS = 120  # Must match the reply freshness bound.
from .event_receiver import ID_PATTERN


class ReviewError(ValueError):
    pass


def stale_reason(event, received_at, now, max_age=MAX_AGE_SECONDS):
    if event.get("replayed_after_restart") is True:
        return "replayed_after_restart"
    if now - received_at > max_age:
        return "inbox_age_exceeded"
    source_time = event.get("received_at")
    if type(source_time) in (int, float) and source_time > 0:
        if now - source_time > max_age:
            return "source_age_exceeded"
    return None


class EventReviews:
    def __init__(self, db):
        self.conn = sqlite3.connect(str(db), timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS event_reviews ("
            "event_id TEXT PRIMARY KEY, source_seq INTEGER NOT NULL, "
            "state TEXT NOT NULL, reason TEXT NOT NULL, "
            "created_at REAL NOT NULL, closed_at REAL, close_note TEXT NOT NULL DEFAULT '')"
        )
        self.conn.commit()

    def sweep(self, *, now=None, max_age=MAX_AGE_SECONDS, limit=200):
        if not 1 <= limit <= 1000 or max_age <= 0:
            raise ReviewError("invalid_sweep_settings")
        now = time.time() if now is None else now
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # This lock serializes with prepare-routed. Quarantine and ACTIVE
            # inbox removal commit together; no pending chat is destroyed.
            rows = self.conn.execute(
                "SELECT seq,id,payload,received_at FROM events "
                "WHERE kind='chat' AND handled_at IS NULL "
                "ORDER BY seq LIMIT ?", (limit,),
            ).fetchall()
            quarantined = 0
            for seq, eid, payload, received in rows:
                try:
                    event = json.loads(payload)
                    reason = stale_reason(event, received, now, max_age)
                except (ValueError, TypeError, AttributeError):
                    reason = "malformed_chat_requires_review"
                if reason is None:
                    continue
                self.conn.execute(
                    "INSERT INTO event_reviews "
                    "(event_id,source_seq,state,reason,created_at) "
                    "VALUES(?,?,'needs_review',?,?)",
                    (eid, seq, reason, now),
                )
                self.conn.execute(
                    "UPDATE events SET handled_at=? "
                    "WHERE id=? AND kind='chat' AND handled_at IS NULL",
                    (now, eid),
                )
                quarantined += 1
            self.conn.commit()
            return quarantined
        except BaseException:
            self.conn.rollback()
            raise

    def pending_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM event_reviews WHERE state='needs_review'"
        ).fetchone()[0]

    def list_reviews(self, *, limit=50):
        if not 1 <= limit <= 200:
            raise ReviewError("invalid_limit")
        rows = self.conn.execute(
            "SELECT r.event_id,r.source_seq,r.reason,r.created_at,e.payload "
            "FROM event_reviews r JOIN events e ON e.id=r.event_id "
            "WHERE r.state='needs_review' ORDER BY r.source_seq LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for eid, seq, reason, created, payload in rows:
            try:
                event = json.loads(payload)
                text = event.get("text") if isinstance(event, dict) else None
            except (ValueError, TypeError):
                text = None
            result.append({"event_id": eid, "source_seq": seq,
                           "reason": reason, "quarantined_at": created,
                           "text": text})
        return result

    def close_review(self, event_id, *, note):
        if not isinstance(event_id, str) or not ID_PATTERN.fullmatch(event_id):
            raise ReviewError("invalid_event_id")
        if not isinstance(note, str) or not 1 <= len(note) <= 300:
            raise ReviewError("review_requires_nonempty_note")
        with self.conn:
            updated = self.conn.execute(
                "UPDATE event_reviews SET state='reviewed',closed_at=?,close_note=? "
                "WHERE event_id=? AND state='needs_review'",
                (time.time(), note, event_id),
            ).rowcount
        if updated != 1:
            raise ReviewError("review_not_pending")
        return "reviewed"

    def close(self):
        self.conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stale Minecraft chat review")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("sweep", "list", "close"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--db", required=True)
        if name == "list":
            cmd.add_argument("--limit", type=int, default=50)
        if name == "close":
            cmd.add_argument("--event-id", required=True)
            cmd.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    reviews = EventReviews(args.db)
    try:
        if args.action == "sweep":
            payload = {"quarantined": reviews.sweep(),
                       "needs_review": reviews.pending_count()}
        elif args.action == "list":
            payload = {"reviews": reviews.list_reviews(args.limit),
                       "needs_review": reviews.pending_count()}
        else:
            payload = {"state": reviews.close_review(args.event_id,
                                                    note=args.note)}
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    finally:
        reviews.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ReviewError, sqlite3.Error, OSError) as exc:
        print("event_review_error:" + str(exc), file=sys.stderr)
        sys.exit(2)
