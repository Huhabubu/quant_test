"""Optional local HTTP push relay for Minecraft events.

Run beside the existing DouDou client; never modifies or restarts Minecraft.
Receiver must be a separately verified local Muse-compatible event hook.
A 2xx HTTP response means delivery to that hook, NOT agent activation.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .events import MinecraftChatEvents


def validate_receiver(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or not parsed.port or not parsed.path.startswith("/")
            or parsed.username or parsed.password or parsed.fragment or parsed.query):
        raise ValueError("receiver_must_be_loopback_http_with_port_and_path")
    return url


def item_counts(state):
    counts = Counter()
    for item in state.get("inventory", []):
        if isinstance(item, dict) and isinstance(item.get("item_id"), str):
            qty = item.get("count", 0)
            if type(qty) is int and qty > 0:
                counts[item["item_id"]] += qty
    return counts


class StateChangeTracker:
    """Convert fresh state changes into small meaningful events, not raw snapshots."""
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.previous = None
        self.last_timestamp = None

    def poll(self):
        try:
            if self.path.stat().st_size > 131072:
                return []
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return []
        ts = state.get("captured_at_ms")
        if (state.get("schema") != 1 or type(ts) is not int
                or abs(self.clock() - ts / 1000) > 3
                or not isinstance(state.get("session_id"), str)
                or not isinstance(state.get("in_world"), bool)):
            return []
        if ts == self.last_timestamp:
            return []
        self.last_timestamp = ts
        if not state["in_world"]:
            self.previous = None
            return []
        previous = self.previous
        self.previous = state
        if previous is None or previous.get("session_id") != state["session_id"]:
            return []
        events = []
        before_health, health = previous.get("health"), state.get("health")
        if type(health) in (int, float) and type(before_health) in (int, float) and health < before_health:
            events.append({"type": "health_drop", "before": before_health, "now": health})
        if state.get("dead") is True and previous.get("dead") is not True:
            events.append({"type": "death"})
        before_food, food = previous.get("hunger"), state.get("hunger")
        if type(food) is int and type(before_food) is int and food <= 6 < before_food:
            events.append({"type": "low_hunger", "now": food})
        old_items, new_items = item_counts(previous), item_counts(state)
        changed = sorted(set(old_items) | set(new_items))
        differences = [
            {"item_id": item, "before": old_items[item], "now": new_items[item]}
            for item in changed if old_items[item] != new_items[item]
        ]
        if differences:
            events.append({"type": "inventory_changed", "changes": differences[:20],
                           "truncated": len(differences) > 20})
        old_players = {x.get("uuid") for x in previous.get("nearby_entities", [])
                       if isinstance(x, dict) and x.get("category") == "player" and x.get("uuid")}
        new_players = {x.get("uuid") for x in state.get("nearby_entities", [])
                       if isinstance(x, dict) and x.get("category") == "player" and x.get("uuid")}
        if new_players - old_players:
            events.append({"type": "player_nearby", "new_count": len(new_players - old_players)})
        for event in events:
            event["captured_at_ms"] = ts
        return events


class EventOutbox:
    """Persistent at-least-once queue. Receiver deduplicates by event id."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.conn.commit()

    def put(self, event):
        if self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0] >= 10000:
            raise RuntimeError("event_outbox_full_stop_instead_of_dropping_silently")
        event_id = uuid4().hex
        self.conn.execute("INSERT INTO outbox(id, payload) VALUES (?,?)",
                          (event_id, json.dumps(event, ensure_ascii=False, separators=(",", ":"))))
        self.conn.commit()

    def next(self):
        row = self.conn.execute("SELECT id,payload FROM outbox ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            return None
        return row[0], json.loads(row[1])

    def ack(self, event_id):
        self.conn.execute("DELETE FROM outbox WHERE id=?", (event_id,))
        self.conn.commit()

    def close(self):
        self.conn.close()


def http_deliver(receiver, token, event_id, event, *, opener=urlopen):
    validate_receiver(receiver)
    data = json.dumps({"id": event_id, "event": event}, ensure_ascii=False).encode("utf-8")
    req = Request(receiver, data=data, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json; charset=utf-8",
        "X-DouDou-Event-ID": event_id,
    })
    with opener(req, timeout=3) as response:
        if not 200 <= response.status < 300:
            raise OSError("receiver_non_2xx")


def deliver_pending(outbox, receiver, token, *, deliver=http_deliver,
                    max_events=256, max_seconds=0.75):
    """Drain quickly without starving the log tail when a chat burst arrives."""
    start = time.monotonic()
    delivered = 0
    for _ in range(max_events):
        if delivered and time.monotonic() - start >= max_seconds:
            break
        pending = outbox.next()
        if pending is None:
            break
        event_id, event = pending
        try:
            deliver(receiver, token, event_id, event)
        except (OSError, ValueError):
            break  # Keep same ID; remote 2xx alone never proves Muse processing.
        outbox.ack(event_id)
        delivered += 1
    return delivered


def pump_once(watcher, cursor, tracker, outbox, receiver, token, *, deliver=http_deliver):
    result = watcher.get(after=cursor, wait_ms=250)
    # When watcher memory overflows, this fact must be surfaced in the outbox.
    if result["dropped"]:
        outbox.put({"type": "event_gap", "after": cursor, "next_cursor": result["next_cursor"]})
    for event in result["events"]:
        outbox.put(event)
    cursor = result["next_cursor"]
    if tracker is not None:
        for event in tracker.poll():
            outbox.put(event)
    deliver_pending(outbox, receiver, token, deliver=deliver)
    return cursor


def main(argv=None):
    parser = argparse.ArgumentParser(description="Push game events to an authorized localhost receiver")
    parser.add_argument("--log", required=True, help="Existing Minecraft latest.log")
    parser.add_argument("--state", help="Existing DouDou state.json; optional")
    parser.add_argument("--receiver", required=True, help="Confirmed localhost HTTP POST endpoint")
    parser.add_argument("--outbox", required=True, help="Private sqlite spool path")
    parser.add_argument("--sender", default="Huhabubu",
                        help="specific sender, or '*' for ONE all-player reader")
    parser.add_argument("--owner", default="Huhabubu",
                        help="owner whose messages need no wake word")
    args = parser.parse_args(argv)
    try:
        validate_receiver(args.receiver)
    except ValueError as exc:
        parser.error(str(exc))
    token = os.environ.get("DOUDOU_EVENT_TOKEN", "")
    if len(token) < 32 or any(ord(char) <= 32 for char in token):
        parser.error("DOUDOU_EVENT_TOKEN must have >=32 non-whitespace characters")
    from .durable_log import DurableChatTail
    tracker = StateChangeTracker(args.state) if args.state else None
    outbox = EventOutbox(args.outbox)
    watcher = DurableChatTail(args.log, args.sender, outbox, owner=args.owner)
    try:
        while True:
            watcher.poll()  # Atomic log-offset + event capture; replay after restart.
            if tracker is not None:
                for event in tracker.poll():
                    outbox.put(event)
            sent = deliver_pending(outbox, args.receiver, token)
            time.sleep(0.1 if sent or outbox.next() else 0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        outbox.close()


if __name__ == "__main__":
    sys.exit(main())
