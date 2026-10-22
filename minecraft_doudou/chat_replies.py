"""P0-D: durable, chat-only reply handoff for Muse sub-agents.

Child agents compose text; ONE local dispatcher sends it through the existing,
authenticated HeadlessMC bridge /v1/chat API. No launcher, movement, M1 command,
or unverified game-write fallback is available here.

Delivery is intentionally at-most-once at the transport boundary: an uncertain
network result is retained for operator review, never automatically re-sent.
Bridge HTTP 202 acknowledges console submission, NOT in-game appearance.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .bridge import InvalidCommand, quote_hmc
from .event_receiver import ID_PATTERN

MAX_AGE_SECONDS = 120


class ReplyError(ValueError):
    pass


def validate_bridge_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or not parsed.port or parsed.path != "/v1/chat"
            or parsed.username or parsed.password or parsed.fragment or parsed.query):
        raise ReplyError("bridge_must_be_local_chat_only")
    return url


def read_private_token(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ReplyError("token_file_permissions_must_be_0600")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or any(ord(char) <= 32 for char in token):
        raise ReplyError("invalid_token")
    return token


def submit_chat(url, token, text, *, opener=urlopen):
    validate_bridge_url(url)
    # Reuse the bridge's already-tested quoting/length/command-injection guard.
    quote_hmc(text)
    data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json; charset=utf-8",
    })
    with opener(request, timeout=3) as response:
        body = response.read(2048)
        if response.status != 202:
            raise OSError("chat_bridge_not_accepted")
        parsed = json.loads(body.decode("utf-8"))
        if parsed.get("accepted") is not True:
            raise OSError("chat_bridge_did_not_confirm_submission")
        # Even accepted:true/verified:false is not game-side delivery proof.
        return {"accepted": True, "verified": False}


class ReplyQueue:
    def __init__(self, db):
        # Must be exactly the same inbox.sqlite as event_receiver.
        self.conn = sqlite3.connect(str(db), timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_replies ("
            "event_id TEXT PRIMARY KEY, source_seq INTEGER NOT NULL, "
            "reply_text TEXT NOT NULL, state TEXT NOT NULL, "
            "created_at REAL NOT NULL, attempted_at REAL, finished_at REAL, "
            "result_note TEXT NOT NULL DEFAULT '')"
        )
        self.conn.commit()

    def prepare(self, event_id, through_seq, text, *, now=None):
        if not isinstance(event_id, str) or not ID_PATTERN.fullmatch(event_id):
            raise ReplyError("invalid_event_id")
        if type(through_seq) is not int or through_seq < 1:
            raise ReplyError("invalid_through_seq")
        try:
            quote_hmc(text)
        except InvalidCommand as exc:
            raise ReplyError("invalid_chat_text") from exc
        when = time.time() if now is None else now
        # BEGIN IMMEDIATE serializes sub-agents trying to claim the same batch.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            prev = self.conn.execute(
                "SELECT reply_text FROM chat_replies WHERE event_id=?", (event_id,)
            ).fetchone()
            if prev is not None:
                outcome = "duplicate" if prev[0] == text else "conflict"
                self.conn.commit()
                return outcome
            row = self.conn.execute(
                "SELECT seq,kind,received_at,handled_at FROM events WHERE id=?",
                (event_id,),
            ).fetchone()
            if row is None or row[1] != "chat" or row[3] is not None:
                raise ReplyError("event_not_pending_chat")
            if row[0] > through_seq:
                raise ReplyError("through_seq_before_reply")
            if when - row[2] > MAX_AGE_SECONDS:
                raise ReplyError("chat_is_stale_do_not_reply")
            # Fail closed when through_seq includes a pending chat later than
            # the event being answered, to avoid accidentally ACKing a new turn.
            newer = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE seq>? AND seq<=? "
                "AND kind='chat' AND handled_at IS NULL",
                (row[0], through_seq),
            ).fetchone()[0]
            if newer:
                raise ReplyError("newer_chat_must_be_reviewed_first")
            self.conn.execute(
                "INSERT INTO chat_replies(event_id,source_seq,reply_text,state,created_at)"
                " VALUES(?,?,?,'queued',?)",
                (event_id, row[0], text, when),
            )
            # Classification/response decision is now durable. The physical
            # send status is tracked separately in chat_replies.
            self.conn.execute(
                "UPDATE events SET handled_at=? "
                "WHERE handled_at IS NULL AND seq<=?", (when, through_seq),
            )
            self.conn.commit()
            return "queued"
        except BaseException:
            self.conn.rollback()
            raise

    def send_once(self, deliver):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT event_id,reply_text FROM chat_replies "
                "WHERE state='queued' ORDER BY created_at,event_id LIMIT 1"
            ).fetchone()
            if not row:
                self.conn.commit()
                return None
            self.conn.execute(
                "UPDATE chat_replies SET state='sending',attempted_at=? "
                "WHERE event_id=?", (time.time(), row[0]),
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        # Never attempt again after possible wire send / crash.
        try:
            result = deliver(row[1])
            accepted = isinstance(result, dict) and result.get("accepted") is True
            mocked = accepted and result.get("mock") is True
        except Exception:
            accepted = False
            mocked = False
        state = ("mocked" if mocked else "accepted_unverified") if accepted else "uncertain"
        with self.conn:
            self.conn.execute(
                "UPDATE chat_replies SET state=?,finished_at=?,result_note=? "
                "WHERE event_id=? AND state='sending'",
                (state, time.time(),
                 ("simulation_only" if mocked else "bridge_console_submission_only")
                 if accepted else "operator_review_required",
                 row[0]),
            )
        return {"event_id": row[0], "state": state, "verified": False}

    def stats(self):
        rows = self.conn.execute(
            "SELECT state,COUNT(*) FROM chat_replies GROUP BY state"
        ).fetchall()
        return dict(rows)

    def close(self):
        self.conn.close()


def run_sender(db, *, bridge_url=None, token_file=None, mock_log=None,
               once=False, poll_seconds=0.2):
    if (mock_log is None) == (bridge_url is None):
        raise ReplyError("choose_exactly_one_mock_or_live_transport")
    if bridge_url:
        validate_bridge_url(bridge_url)
        token = read_private_token(token_file)
        deliver = lambda text: submit_chat(bridge_url, token, text)
    else:
        mock = Path(mock_log)
        mock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        def deliver(text):
            with mock.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(
                    {"at": time.time(), "text": text, "mock": True},
                    ensure_ascii=False) + "\n")
            return {"accepted": True, "verified": False, "mock": True}
    queue = ReplyQueue(db)
    try:
        while True:
            result = queue.send_once(deliver)
            if result:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if once:
                return result
            time.sleep(poll_seconds)
    finally:
        queue.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chat-only Muse reply handoff")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "send", "stats"):
        command = sub.add_parser(name)
        command.add_argument("--db", required=True)
        if name == "prepare":
            command.add_argument("--event-id", required=True)
            command.add_argument("--through-seq", required=True, type=int)
            command.add_argument("--text", required=True,
                                 help="MODEL-generated chat text, not a canned template")
        if name == "send":
            command.add_argument("--bridge-url")
            command.add_argument("--token-file")
            command.add_argument("--mock-log")
            command.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "send":
        if bool(args.bridge_url) != bool(args.token_file):
            parser.error("live bridge requires bridge-url and token-file")
        result = run_sender(args.db, bridge_url=args.bridge_url,
                            token_file=args.token_file, mock_log=args.mock_log,
                            once=args.once)
        return 0 if result is None or result["state"] in ("mocked", "accepted_unverified") else 1
    queue = ReplyQueue(args.db)
    try:
        if args.command == "prepare":
            result = queue.prepare(args.event_id, args.through_seq, args.text)
            print(json.dumps({"status": result, "event_id": args.event_id}))
            return 0 if result in ("queued", "duplicate") else 1
        print(json.dumps(queue.stats()))
        return 0
    finally:
        queue.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ReplyError, OSError, sqlite3.Error) as exc:
        print("chat_reply_error:" + str(exc), file=sys.stderr)
        sys.exit(2)
