"""P0-D: durable, chat-only reply handoff for Muse sub-agents.

Child agents compose text; ONE local dispatcher sends it through an explicit,
chat-only transport: the existing HeadlessMC bridge /v1/chat API, or M1's
pre-existing localhost TCP say verb. No launcher/movement/general M1 gateway
or arbitrary console command is available here.

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
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS task_handoffs ("
            "event_id TEXT PRIMARY KEY, source_seq INTEGER NOT NULL, "
            "instruction TEXT NOT NULL, lane TEXT NOT NULL, "
            "priority INTEGER NOT NULL, state TEXT NOT NULL, "
            "created_at REAL NOT NULL, claimed_at REAL, claimed_by TEXT, "
            "finished_at REAL, result_note TEXT NOT NULL DEFAULT '')"
        )
        self.conn.commit()

    def prepare(self, event_id, through_seq, text, *, now=None,
                routes=None, authorized_sender=None):
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
                if outcome == "duplicate" and routes is not None:
                    # A pre-existing legacy reply could have ACKed a task
                    # without persisting any handoff. Never report routed
                    # success in that situation. No new send is permitted.
                    if not isinstance(routes, list):
                        raise ReplyError("invalid_chat_routes")
                    for route in routes:
                        if not isinstance(route, dict):
                            raise ReplyError("invalid_chat_route")
                        eid = route.get("event_id")
                        record = self.conn.execute(
                            "SELECT lane,priority FROM task_handoffs "
                            "WHERE event_id=?", (eid,),
                        ).fetchone()
                        if route.get("kind") == "task":
                            lane = route.get("lane", "game")
                            priority = (1 if route.get("priority", "normal")
                                        == "urgent" else 0)
                            if record != (lane, priority):
                                raise ReplyError("duplicate_without_matching_handoff")
                        elif route.get("kind") == "chat":
                            if record is not None:
                                raise ReplyError("duplicate_conflicting_handoff")
                        else:
                            raise ReplyError("invalid_chat_route")
                self.conn.commit()
                return outcome
            row = self.conn.execute(
                "SELECT seq,kind,received_at,handled_at,payload FROM events WHERE id=?",
                (event_id,),
            ).fetchone()
            if row is None or row[1] != "chat" or row[3] is not None:
                raise ReplyError("event_not_pending_chat")
            if row[0] > through_seq:
                raise ReplyError("through_seq_before_reply")
            if when - row[2] > MAX_AGE_SECONDS:
                raise ReplyError("chat_is_stale_do_not_reply")
            source_event = json.loads(row[4])
            if source_event.get("replayed_after_restart") is True:
                raise ReplyError("recovered_chat_context_only")
            source_time = source_event.get("received_at")
            if (type(source_time) in (int, float)
                    and source_time > 0
                    and when - source_time > MAX_AGE_SECONDS):
                raise ReplyError("source_chat_is_stale_do_not_reply")
            # A new fresh chat must NEVER revive/ACK/execute older stale
            # chat commands. Even if the coordinator is down, fail closed and
            # require explicit quarantine/review before preparing this batch.
            from .event_lifecycle import stale_reason
            previous_chats = self.conn.execute(
                "SELECT id,payload,received_at FROM events "
                "WHERE kind='chat' AND handled_at IS NULL AND seq<=?",
                (through_seq,),
            ).fetchall()
            for old_id, old_payload, old_received in previous_chats:
                try:
                    old_event = json.loads(old_payload)
                    old_reason = stale_reason(old_event, old_received, when)
                except (ValueError, TypeError, AttributeError):
                    old_reason = "malformed_chat_requires_review"
                if old_reason is not None:
                    raise ReplyError("pending_chat_requires_review:" + old_reason)
            # Fail closed when through_seq includes a pending chat later than
            # the event being answered, to avoid accidentally ACKing a new turn.
            newer = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE seq>? AND seq<=? "
                "AND kind='chat' AND handled_at IS NULL",
                (row[0], through_seq),
            ).fetchone()[0]
            if newer:
                raise ReplyError("newer_chat_must_be_reviewed_first")
            if routes is not None:
                # The main agent must explicitly classify EVERY pending chat in
                # this ACK range. Task records are committed in the SAME
                # transaction as the reply and ACK: reply without handoff is
                # impossible when routed mode is used.
                if (not isinstance(routes, list) or not 1 <= len(routes) <= 20
                        or not isinstance(authorized_sender, str)
                        or not authorized_sender):
                    raise ReplyError("invalid_chat_routes")
                pending = self.conn.execute(
                    "SELECT seq,id,payload FROM events "
                    "WHERE kind='chat' AND handled_at IS NULL AND seq<=? "
                    "ORDER BY seq", (through_seq,),
                ).fetchall()
                ids = [r[1] for r in pending]
                if (not ids or ids[-1] != event_id
                        or len(ids) != len(routes)
                        or any(not isinstance(r, dict)
                               or r.get("event_id") != eid
                               for r, eid in zip(routes, ids))):
                    raise ReplyError("routes_must_cover_every_pending_chat_in_order")
                for (source_seq, source_id, payload), route in zip(pending, routes):
                    source = json.loads(payload)
                    if source.get("sender") != authorized_sender:
                        raise ReplyError("unauthorized_chat_in_routed_batch")
                    kind = route.get("kind")
                    if kind == "chat":
                        if set(route) != {"event_id", "kind"}:
                            raise ReplyError("invalid_chat_route")
                        continue
                    if kind != "task" or not set(route).issubset(
                            {"event_id", "kind", "lane", "priority"}):
                        raise ReplyError("invalid_chat_route")
                    lane = route.get("lane", "game")
                    priority = route.get("priority", "normal")
                    if lane not in ("game", "research") or priority not in (
                            "normal", "urgent"):
                        raise ReplyError("invalid_task_route")
                    instruction = source.get("text")
                    if not isinstance(instruction, str) or not instruction.strip():
                        raise ReplyError("invalid_task_instruction")
                    self.conn.execute(
                        "INSERT INTO task_handoffs "
                        "(event_id,source_seq,instruction,lane,priority,state,created_at) "
                        "VALUES(?,?,?,?,?,'queued',?)",
                        (source_id, source_seq, instruction, lane,
                         1 if priority == "urgent" else 0, when),
                    )
            self.conn.execute(
                "INSERT INTO chat_replies(event_id,source_seq,reply_text,state,created_at)"
                " VALUES(?,?,?,'queued',?)",
                (event_id, row[0], text, when),
            )
            # Classification/response decision is now durable. The physical
            # send status is tracked separately in chat_replies.
            # Never ACK death, low_hunger, event_gap, etc. as a side effect
            # of replying to chat. They belong to the main event workflow.
            self.conn.execute(
                "UPDATE events SET handled_at=? "
                "WHERE handled_at IS NULL AND kind='chat' AND seq<=?",
                (when, through_seq),
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
                "SELECT c.event_id,c.reply_text,e.received_at FROM chat_replies c "
                "LEFT JOIN events e ON e.id=c.event_id "
                "WHERE c.state='queued' ORDER BY c.created_at,c.event_id LIMIT 1"
            ).fetchone()
            if not row:
                self.conn.commit()
                return None
            now = time.time()
            if row[2] is None or now - row[2] > MAX_AGE_SECONDS:
                # Even an initially fresh model reply must not be sent as
                # stale chat if a dispatcher was offline for minutes.
                self.conn.execute(
                    "UPDATE chat_replies SET state='expired_unsent',"
                    "finished_at=?,result_note='stale_before_transport' "
                    "WHERE event_id=? AND state='queued'", (now, row[0]),
                )
                self.conn.commit()
                return {"event_id": row[0], "state": "expired_unsent",
                        "verified": False}
            self.conn.execute(
                "UPDATE chat_replies SET state='sending',attempted_at=? "
                "WHERE event_id=?", (now, row[0]),
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        # Never attempt again after possible wire send / crash.
        diagnostic = ""
        try:
            result = deliver(row[1])
            accepted = isinstance(result, dict) and result.get("accepted") is True
            mocked = accepted and result.get("mock") is True
        except Exception as exc:
            accepted = False
            mocked = False
            # Stable, bounded failure category without printing chat or secrets.
            diagnostic = type(exc).__name__
            if type(exc).__name__ == "M1ChatError":
                diagnostic = str(exc)[:80]
        state = ("mocked" if mocked else "accepted_unverified") if accepted else "uncertain"
        skipped = 0
        if accepted and result.get("transport") == "m1":
            candidate = result.get("skipped_async_frames", 0)
            if type(candidate) is int and 0 <= candidate <= 96:
                skipped = candidate
        if accepted:
            if mocked:
                note = "simulation_only"
            elif result.get("transport") == "m1":
                note = "m1_tcp_accepted_unverified"
                if skipped:
                    note += f":async_skipped={skipped}"
            else:
                note = "bridge_console_submission_only"
        else:
            note = "operator_review_required:" + diagnostic
        with self.conn:
            self.conn.execute(
                "UPDATE chat_replies SET state=?,finished_at=?,result_note=? "
                "WHERE event_id=? AND state='sending'",
                (state, time.time(), note, row[0]),
            )
        output = {"event_id": row[0], "state": state, "verified": False}
        if skipped:
            output["skipped_async_frames"] = skipped
        if not accepted:
            output["failure_category"] = diagnostic
        return output

    def stats(self):
        rows = self.conn.execute(
            "SELECT state,COUNT(*) FROM chat_replies GROUP BY state"
        ).fetchall()
        return dict(rows)

    def close(self):
        self.conn.close()


def run_sender(db, *, bridge_url=None, token_file=None, mock_log=None,
               m1_say=False, once=False, poll_seconds=0.2):
    if sum((mock_log is not None, bridge_url is not None, m1_say)) != 1:
        raise ReplyError("choose_exactly_one_mock_bridge_or_m1_transport")
    if bridge_url is not None:
        if not token_file:
            raise ReplyError("bridge_token_file_required")
        validate_bridge_url(bridge_url)
        token = read_private_token(token_file)
        deliver = lambda text: submit_chat(bridge_url, token, text)
    elif m1_say:
        if token_file is not None:
            raise ReplyError("m1_uses_local_tcp_not_bridge_token")
        from .m1_chat import send_m1_chat
        deliver = send_m1_chat
    else:
        if token_file is not None:
            raise ReplyError("mock_must_not_have_bridge_token")
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
    for name in ("prepare", "prepare-routed", "send", "stats"):
        command = sub.add_parser(name)
        command.add_argument("--db", required=True)
        if name in ("prepare", "prepare-routed"):
            command.add_argument("--event-id", required=True)
            command.add_argument("--through-seq", required=True, type=int)
            command.add_argument("--text", required=True,
                                 help="MODEL-generated chat text, not a canned template")
        if name == "prepare-routed":
            command.add_argument("--routes-json", required=True,
                                 help="ordered explicit chat/task decisions for ALL pending chats")
            command.add_argument("--sender", default="Huhabubu")
        if name == "send":
            command.add_argument("--bridge-url")
            command.add_argument("--token-file")
            command.add_argument("--mock-log")
            command.add_argument("--m1-say", action="store_true",
                                 help="Explicit opt-in to local M1 say-only sender")
            command.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "send":
        if bool(args.bridge_url) != bool(args.token_file):
            parser.error("bridge requires bridge-url and token-file")
        result = run_sender(args.db, bridge_url=args.bridge_url,
                            token_file=args.token_file, mock_log=args.mock_log,
                            m1_say=args.m1_say, once=args.once)
        return 0 if result is None or result["state"] in ("mocked", "accepted_unverified") else 1
    queue = ReplyQueue(args.db)
    try:
        if args.command in ("prepare", "prepare-routed"):
            try:
                routes = (json.loads(args.routes_json)
                          if args.command == "prepare-routed" else None)
            except ValueError as exc:
                raise ReplyError("invalid_routes_json") from exc
            result = queue.prepare(
                args.event_id, args.through_seq, args.text, routes=routes,
                authorized_sender=(args.sender
                                   if args.command == "prepare-routed" else None))
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
