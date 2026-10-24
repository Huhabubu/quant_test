"""Independent DouDou runtime health and inbox lifecycle monitor.

NO LLM calls, NO Minecraft commands, NO automatic sender retries.
Systemd/supervisor (not this model session) owns the receiver, relay and
sender process lifetimes; this process checks configured service health.
A one-shot Muse arm is NOT a persistent listener and cannot be revived by
Python without a supported Muse host interface.

The coordinator scans and quarantines stale player chat so it cannot block
future messages or be revived as a new task. The original event remains in
SQLite with a durable needs_review record.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .event_lifecycle import EventReviews


class CoordinatorError(ValueError):
    pass


def _absolute_path(value, label):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise CoordinatorError(label + "_must_be_absolute")
    return value


def load_config(path):
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise CoordinatorError("invalid_configuration")
    for name in ("db", "status_path", "lock_path"):
        _absolute_path(data.get(name), name)
    delay = data.get("interval_seconds", 3)
    if type(delay) not in (int, float) or not 0.5 <= delay <= 60:
        raise CoordinatorError("invalid_interval")
    data["interval_seconds"] = delay
    services = data.get("systemd_units", {})
    if not isinstance(services, dict):
        raise CoordinatorError("invalid_systemd_units")
    # systemctl receives an argv list, never a shell; only exact safe names.
    import re
    pattern = re.compile(r"[A-Za-z0-9_.@-]{1,128}\Z")
    if any(not isinstance(name, str) or not name
           or not isinstance(unit, str) or not pattern.fullmatch(unit)
           or not unit.endswith(".service")
           for name, unit in services.items()):
        raise CoordinatorError("invalid_systemd_unit")
    data["systemd_units"] = services
    scope = data.get("systemd_scope", "user")
    if scope not in ("user", "system", "none"):
        raise CoordinatorError("invalid_systemd_scope")
    data["systemd_scope"] = scope
    path = data.get("state_path")
    if path is not None:
        _absolute_path(path, "state_path")
    return data


def _private_dir(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _atomic_status(path, payload):
    _private_dir(path)
    tmp = str(path) + ".tmp." + str(os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _systemd_service(unit, scope="user"):
    # Query only. Use the host's ACTUAL service manager.
    # --system requires a registered system unit and host privileges to set
    # one up; it does not grant or request those permissions.
    if scope not in ("user", "system"):
        return "unobservable"
    try:
        result = subprocess.run(
            ["systemctl", "--" + scope, "is-active", unit],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unobservable"
    if result.returncode == 0 and result.stdout.strip() == "active":
        return "active"
    if result.stdout.strip() in ("inactive", "failed", "activating",
                                 "deactivating"):
        return result.stdout.strip()
    return "unobservable"


def _state_health(path, now):
    if not path:
        return "not_configured"
    try:
        with open(path, encoding="utf-8") as stream:
            state = json.load(stream)
        ts = state.get("captured_at_ms")
        if type(ts) is not int or abs(now - ts / 1000) > 3:
            return "stale_or_unavailable"
        return "fresh" if state.get("in_world") is True else "not_in_world"
    except (OSError, ValueError, AttributeError):
        return "stale_or_unavailable"


def sample(config, *, now=None, service_check=None):
    now = time.time() if now is None else now
    reviews = EventReviews(config["db"])
    try:
        # The sweep and prepare-routed each serialize under SQLite BEGIN
        # IMMEDIATE. This removes race between quarantine and ACK.
        quarantined = reviews.sweep(now=now)
        review_count = reviews.pending_count()
        conn = reviews.conn
        rows = conn.execute(
            "SELECT kind,COUNT(*),MIN(received_at) FROM events "
            "WHERE handled_at IS NULL GROUP BY kind"
        ).fetchall()
        counts = {kind: count for kind, count, _ in rows}
        total = sum(counts.values())
        chat = counts.get("chat", 0)
        ages = {kind: round(max(0, now - first), 2)
                for kind, _, first in rows if first is not None}
        pending_tasks = {}
        try:
            pending_tasks = dict(conn.execute(
                "SELECT state,COUNT(*) FROM task_handoffs GROUP BY state"
            ).fetchall())
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
        scope = config.get("systemd_scope", "user")
        if service_check is None:
            service_check = lambda unit: _systemd_service(unit, scope)
        services = {
            name: (service_check(unit) if scope != "none" else "unobservable")
            for name, unit in config.get("systemd_units", {}).items()
        }
        # A missing D-Bus or unsupported systemctl environment is *unknown*,
        # not evidence that a daemon crashed. Keep it separately visible.
        unhealthy = sorted(
            name for name, value in services.items()
            if value in ("inactive", "failed", "deactivating")
        )
        unobservable = sorted(
            name for name, value in services.items()
            if value == "unobservable"
        )
        payload = {
            "sampled_at": now,
            "inbox": {"pending_total": total, "pending_chat": chat,
                      "pending_by_kind": counts, "oldest_age_s": ages},
            "reviews": {"newly_quarantined": quarantined,
                        "needs_review": review_count},
            "tasks": pending_tasks,
            "services": services,
            "unhealthy_units": unhealthy,
            "unobservable_units": unobservable,
            "systemd_scope": scope,
            "game_state": _state_health(config.get("state_path"), now),
            "main_agent": {
                "availability": "unobservable_without_host_api",
                "automatic_revival": "not_supported_by_current_host",
                "chat_attention_needed": chat > 0,
            },
            "service_management": ("external_systemd_supervisor"
                                   if scope != "none" else "not_configured"),
        }
        _atomic_status(config["status_path"], payload)
        return payload
    finally:
        reviews.close()


def monitor(config, *, once=False):
    _private_dir(config["lock_path"])
    # Distinct coordinator instances cannot both own scan/status writes.
    # This does NOT establish ownership of old un-supervised senders;
    # operators must retire those before enabling systemd units.
    fd = os.open(config["lock_path"], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CoordinatorError("coordinator_already_running") from exc
        while True:
            payload = sample(config)
            if once:
                print(json.dumps(payload, ensure_ascii=False))
                break
            time.sleep(config["interval_seconds"])
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="DouDou autonomous runtime monitor")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true",
                        help="one deterministic scan; no background service")
    args = parser.parse_args(argv)
    monitor(load_config(args.config), once=args.once)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CoordinatorError, OSError, ValueError, sqlite3.Error) as exc:
        print("runtime_coordinator_error:" + str(exc), file=sys.stderr)
        sys.exit(2)
