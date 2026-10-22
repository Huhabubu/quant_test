"""Persistent localhost event inbox and one-shot Muse wake notifier.

Uses Python's HTTPServer and SQLite; a 200 response means a durable inbox
write, not that Muse has read or acted on an event.
"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MAX_BODY = 65536
MAX_PENDING = 10000
WAKE_TYPES = ("chat", "death")


class EventInbox:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(str(path), timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, "
            "kind TEXT NOT NULL, payload TEXT NOT NULL, received_at REAL NOT NULL, "
            "handled_at REAL)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS pending_events "
            "ON events(handled_at,seq)"
        )
        self.conn.commit()

    def accept(self, event_id, event):
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.conn:
            previous = self.conn.execute(
                "SELECT payload FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if previous is not None:
                return "duplicate" if previous[0] == encoded else "conflict"
            count = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE handled_at IS NULL"
            ).fetchone()[0]
            if count >= MAX_PENDING:
                return "full"
            self.conn.execute(
                "INSERT INTO events(id,kind,payload,received_at) VALUES(?,?,?,?)",
                (event_id, event["type"], encoded, time.time()),
            )
            return "accepted"

    def pending(self, after_seq=0, limit=1000):
        rows = self.conn.execute(
            "SELECT seq,id,payload,received_at FROM events "
            "WHERE handled_at IS NULL AND seq>? ORDER BY seq LIMIT ?",
            (after_seq, limit),
        ).fetchall()
        return [
            {"seq": seq, "id": eid, "event": json.loads(payload),
             "received_at": received}
            for seq, eid, payload, received in rows
        ]

    def counts(self):
        row = self.conn.execute(
            "SELECT COUNT(*), MIN(received_at) FROM events "
            "WHERE handled_at IS NULL"
        ).fetchone()
        return {"pending": row[0], "oldest_age_s":
                round(max(0.0, time.time() - row[1]), 3) if row[1] else 0}

    def actionable(self):
        placeholders = ",".join("?" for _ in WAKE_TYPES)
        return self.conn.execute(
            "SELECT seq,id,kind FROM events WHERE handled_at IS NULL "
            f"AND kind IN ({placeholders}) ORDER BY seq LIMIT 1", WAKE_TYPES
        ).fetchone()

    def ack_through(self, seq):
        with self.conn:
            return self.conn.execute(
                "UPDATE events SET handled_at=? "
                "WHERE handled_at IS NULL AND seq<=?",
                (time.time(), seq),
            ).rowcount

    def close(self):
        self.conn.close()


def validate_request(headers, raw):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    eid = data.get("id")
    header_id = headers.get("X-DouDou-Event-ID")
    event = data.get("event")
    if (not isinstance(eid, str) or not ID_PATTERN.fullmatch(eid)
            or (header_id is not None and header_id != eid)
            or not isinstance(event, dict)
            or not isinstance(event.get("type"), str)
            or not 1 <= len(event["type"]) <= 64):
        return None
    if ("text" in event and (not isinstance(event["text"], str)
                             or len(event["text"]) > 500)):
        return None
    return eid, event


def serve(db, port, token):
    inbox = EventInbox(db)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/events":
                self.send_json(404, {"error": "not_found"})
                return
            if not hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_json(400, {"error": "invalid_content_length"})
                return
            if length <= 0 or length > MAX_BODY:
                self.send_json(413 if length > MAX_BODY else 400,
                               {"error": "invalid_body_size"})
                return
            self.connection.settimeout(2.0)
            try:
                raw = self.rfile.read(length)
            except OSError:
                return  # Disconnect stalled/malformed local request; keep serving.
            parsed = validate_request(self.headers, raw)
            if parsed is None:
                self.send_json(400, {"error": "invalid_event"})
                return
            try:
                status = inbox.accept(*parsed)
            except sqlite3.Error:
                self.send_json(503, {"error": "storage_unavailable"})
                return
            codes = {"accepted": 200, "duplicate": 200,
                     "conflict": 409, "full": 503}
            self.send_json(codes[status], {"status": status})

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
    finally:
        inbox.close()


def arm(db, poll_seconds=0.1, timeout=0):
    inbox = EventInbox(db)
    started = time.monotonic()
    try:
        while True:
            event = inbox.actionable()
            if event is not None:
                print(json.dumps({"wake": True, "first_seq": event[0],
                                  "type": event[2], **inbox.counts()}), flush=True)
                return 0
            if timeout and time.monotonic() - started >= timeout:
                return 124
            time.sleep(poll_seconds)
    finally:
        inbox.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Persistent DouDou inbox and Muse wake")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "arm", "drain", "ack", "stats"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--db", required=True)
        if name == "serve":
            cmd.add_argument("--port", type=int, default=18765)
            cmd.add_argument("--token-file", required=True)
        if name == "arm":
            cmd.add_argument("--timeout", type=float, default=0)
            cmd.add_argument("--poll-seconds", type=float, default=0.1)
        if name == "drain":
            cmd.add_argument("--after-seq", type=int, default=0)
            cmd.add_argument("--limit", type=int, default=200)
        if name == "ack":
            cmd.add_argument("--through-seq", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            parser.error("invalid port")
        secret_path = Path(args.token_file)
        if secret_path.stat().st_mode & 0o077:
            parser.error("token file permissions must be private (0600)")
        token = secret_path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            parser.error("token too short")
        serve(args.db, args.port, token)
        return 0
    if args.command == "arm":
        if not 0.02 <= args.poll_seconds <= 5 or args.timeout < 0:
            parser.error("invalid poll/timeout")
        return arm(args.db, args.poll_seconds, args.timeout)
    inbox = EventInbox(args.db)
    try:
        if args.command == "drain":
            if args.after_seq < 0 or not 1 <= args.limit <= 1000:
                parser.error("invalid pagination")
            events = inbox.pending(args.after_seq, args.limit)
            print(json.dumps({"events": events,
                              "next_seq": events[-1]["seq"] if events else args.after_seq,
                              **inbox.counts()}, ensure_ascii=False))
        elif args.command == "ack":
            if args.through_seq < 0:
                parser.error("invalid sequence")
            print(json.dumps({"acked": inbox.ack_through(args.through_seq),
                              **inbox.counts()}))
        else:
            print(json.dumps(inbox.counts()))
    finally:
        inbox.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
