"""One-shot context fetch for the short-chat fast path (P0-E variant C).

A woken chat agent calls this exactly once to obtain, inside a single
process, (a) the newest still-unhandled player chat from the durable inbox
and (b) a compact snapshot of the DouDou client state that a short reply is
allowed to cite. It replaces the multi-step drain + state reads that cost
several agent tool round-trips (measured T2->context: 3.0-6.6s multi-step
vs 0.06-0.18s one-shot; P0-E report, Issue #5 #6335482223).

Entry point:
    python3 -m minecraft_doudou.child_once \
        --db /path/to/inbox.sqlite \
        --state /path/to/doudou-io/state.json \
        [--telemetry /path/to/telemetry-child.jsonl] \
        [--sender Huhabubu]

Output: exactly one JSON line on stdout:
    {"chat": {"seq", "event_id", "text", "received_at"} | null,
     "chats": [all pending authorized chats, oldest-first, max 20],
     "state": {"hp", "food", "clock", "pos"} | {"in_world": false}}

State fields read (and only these): ``in_world``, ``health``, ``hunger``,
``clock_time``, ``x``, ``y``, ``z``. The client mod rewrites state.json
every 10 ticks, so a snapshot is at most ~1s old when the game is running.

Boundaries (review-relevant):
- READ-ONLY peek. The inbox is opened with SQLite ``mode=ro``; this module
  never writes, never claims, never ACKs. Dedupe/claim stays where it
  already lives: ``chat_replies prepare`` atomically claims one event_id.
  If the agent dies between this peek and prepare, the event is still
  unhandled and will be offered again; at-most-once sending is preserved
  because prepare is the single claim point.
- This module never sends chat and never triggers game actions. The chat
  agent may only reply (via prepare); actions/stop requests remain an
  explicit handoff to the main agent.
- Failure contract: a missing/corrupt state file yields
  ``{"in_world": false}`` (the agent can still answer greetings, and must
  not cite state it could not read). A missing/unopenable inbox exits
  non-zero with no stdout payload, which is the caller's signal to fall
  back to the full multi-step path instead of guessing.
"""
import argparse
import json
import sqlite3
import sys
import time

from .chat_policy import admitted_chat

MAX_CHAT_BATCH = 20


def fetch_chats(db_path, sender):
    """Peek the complete bounded pending chat batch, oldest first.

    The subsequent prepare ACKs all events through the newest sequence, so
    omitting earlier unanswered player chats would silently lose them.
    Fail closed on oversized batches instead of returning only the tail.
    """
    uri = "file:{}?mode=ro".format(db_path)
    db = sqlite3.connect(uri, uri=True)
    try:
        rows = db.execute(
            "SELECT seq, id, payload, received_at FROM events "
            "WHERE kind = 'chat' AND handled_at IS NULL "
            "ORDER BY seq DESC LIMIT ?",
            (MAX_CHAT_BATCH + 1,),
        ).fetchall()
    finally:
        db.close()
    if len(rows) > MAX_CHAT_BATCH:
        raise ValueError("pending_chat_batch_too_large")
    chats = []
    for seq, event_id, payload, received_at in reversed(rows):
        inner = json.loads(payload)
        player = inner.get("sender")
        text = inner.get("text")
        if not admitted_chat(player, text, sender):
            # Never hide unqualified foreign records in a batch that MAIN
            # will atomically ACK by sequence; fail closed for review.
            raise ValueError("pending_chat_wrong_sender_or_not_addressed")
        chats.append({
            "seq": seq,
            "event_id": event_id,
            "sender": player,
            "is_owner": player == sender,
            "text": text,
            "received_at": received_at,
        })
    return chats


def fetch_review_count(db_path):
    """Read-only number of quarantined instructions awaiting MAIN review.

    The table may not exist before the coordinator's first migration.
    This adds no model tool roundtrip to the existing child_once fast path.
    """
    db = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
    try:
        try:
            return db.execute(
                "SELECT COUNT(*) FROM event_reviews WHERE state='needs_review'"
            ).fetchone()[0]
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return 0
    finally:
        db.close()


def fetch_chat(db_path, sender):
    """Compatibility helper returning the most recent pending owner chat."""
    chats = fetch_chats(db_path, sender)
    return chats[-1] if chats else None

def fetch_state(state_path):
    """Return the compact reply-citable state snapshot (see module docs)."""
    try:
        with open(state_path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {"in_world": False}
    if not raw.get("in_world"):
        return {"in_world": False}
    # The live Mod includes a millisecond capture clock. Reject stale,
    # malformed or future-dated world state rather than claiming it is current.
    captured = raw.get("captured_at_ms")
    if captured is not None and (
        type(captured) is not int or abs(time.time() - captured / 1000) > 3
    ):
        return {"in_world": False}
    try:
        return {
            "hp": round(raw["health"], 1),
            "food": raw["hunger"],
            "clock": raw.get("clock_time"),
            "pos": [round(raw["x"]), round(raw["y"]), round(raw["z"])],
        }
    except (KeyError, TypeError, ValueError):
        return {"in_world": False}


def run(db_path, state_path, sender, telemetry_path=None):
    """Execute one fetch; returns the payload dict (also printed by main)."""
    t_start = time.time()
    chats = fetch_chats(db_path, sender)
    chat = chats[-1] if chats else None
    t_drained = time.time()
    state = fetch_state(state_path)
    review_pending = fetch_review_count(db_path)
    t_state = time.time()
    if telemetry_path:
        try:
            with open(telemetry_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "kind": "child_once",
                    "t_start": t_start,
                    "t_drained": t_drained,
                    "t_state": t_state,
                    "seq": chat["seq"] if chat else None,
                }) + "\n")
        except OSError:
            pass  # telemetry must never break the fetch
    return {"chat": chat, "chats": chats, "state": state,
            "review_pending": review_pending}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="child_once")
    parser.add_argument("--db", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--sender", default="Huhabubu")
    parser.add_argument("--telemetry")
    args = parser.parse_args(argv)
    try:
        payload = run(args.db, args.state, args.sender, args.telemetry)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print("child_once: inbox unavailable: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
