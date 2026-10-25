"""Durable task dispatch for the ONE conversation-owning DouDou main agent.

Only a routed chat prepare may create a task. Claim is atomic; once claimed,
a task is NEVER automatically restarted after a crash (game commands may
already have happened). The main agent must review claimed work on recovery.

Only one *game* task can be claimed at once, since M1/Baritone/Mod share one
character. Independent read-only/research tasks may be claimed concurrently.
This module does not spawn agents or invoke Minecraft itself.
"""
import argparse
import json
import re
import sqlite3
import sys
import time

WORKER_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class TaskError(ValueError):
    pass


class TaskQueue:
    def __init__(self, db):
        self.conn = sqlite3.connect(str(db), timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS task_handoffs ("
            "event_id TEXT PRIMARY KEY, source_seq INTEGER NOT NULL, "
            "instruction TEXT NOT NULL, lane TEXT NOT NULL, "
            "priority INTEGER NOT NULL, state TEXT NOT NULL, "
            "created_at REAL NOT NULL, claimed_at REAL, claimed_by TEXT, "
            "finished_at REAL, result_note TEXT NOT NULL DEFAULT '')"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS task_progress ("
            "event_id TEXT PRIMARY KEY, worker TEXT NOT NULL, "
            "stage TEXT NOT NULL, summary TEXT NOT NULL, "
            "updated_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1)"
        )
        self.conn.commit()

    def report_progress(self, event_id, worker, stage, summary):
        """Worker writes status only; no game action, spawn or MAIN wake.

        Worker progress is self-reported; not evidence of execution or
        completion. Only the claimed worker may write to its own task.
        """
        if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
            raise TaskError("invalid_event_id")
        if not isinstance(worker, str) or not WORKER_ID.fullmatch(worker):
            raise TaskError("invalid_worker")
        if stage not in ("started", "working", "blocked"):
            raise TaskError("invalid_progress_stage")
        if (not isinstance(summary, str) or not 1 <= len(summary) <= 160
                or any(ord(c) < 32 for c in summary)):
            raise TaskError("invalid_progress_summary")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT 1 FROM task_handoffs "
                "WHERE event_id=? AND state='claimed' AND claimed_by=?",
                (event_id, worker),
            ).fetchone()
            if row is None:
                raise TaskError("progress_requires_active_worker_claim")
            self.conn.execute(
                "INSERT INTO task_progress "
                "(event_id,worker,stage,summary,updated_at,revision)"
                " VALUES(?,?,?,?,?,1) ON CONFLICT(event_id) DO UPDATE SET "
                "stage=excluded.stage,summary=excluded.summary,"
                "worker=excluded.worker,updated_at=excluded.updated_at,"
                "revision=task_progress.revision+1",
                (event_id, worker, stage, summary, time.time()),
            )
            self.conn.commit()
            return stage
        except BaseException:
            self.conn.rollback()
            raise

    def claim(self, worker):
        if not isinstance(worker, str) or not WORKER_ID.fullmatch(worker):
            raise TaskError("invalid_worker")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            game_busy = self.conn.execute(
                "SELECT 1 FROM task_handoffs "
                "WHERE lane='game' AND state='claimed' LIMIT 1"
            ).fetchone() is not None
            row = self.conn.execute(
                "SELECT event_id,source_seq,instruction,lane,priority "
                "FROM task_handoffs "
                "WHERE state='queued' AND (?=0 OR lane!='game') "
                "ORDER BY priority DESC,source_seq ASC LIMIT 1",
                (int(game_busy),),
            ).fetchone()
            if not row:
                self.conn.commit()
                return None
            now = time.time()
            self.conn.execute(
                "UPDATE task_handoffs SET state='claimed',claimed_at=?,claimed_by=? "
                "WHERE event_id=? AND state='queued'", (now, worker, row[0]),
            )
            self.conn.commit()
            return {
                "event_id": row[0], "source_seq": row[1],
                "instruction": row[2], "lane": row[3],
                "priority": "urgent" if row[4] else "normal",
                "state": "claimed", "worker": worker,
            }
        except BaseException:
            self.conn.rollback()
            raise

    def finish(self, event_id, worker, status, note=""):
        if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
            raise TaskError("invalid_event_id")
        if not isinstance(worker, str) or not WORKER_ID.fullmatch(worker):
            raise TaskError("invalid_worker")
        if status not in ("completed", "failed", "cancelled"):
            raise TaskError("invalid_finish_status")
        if not isinstance(note, str) or len(note) > 500:
            raise TaskError("invalid_result_note")
        with self.conn:
            changed = self.conn.execute(
                "UPDATE task_handoffs SET state=?,finished_at=?,result_note=? "
                "WHERE event_id=? AND state='claimed' AND claimed_by=?",
                (status, time.time(), note, event_id, worker),
            ).rowcount
        if changed != 1:
            raise TaskError("task_not_claimed_by_worker")
        return status

    def list_tasks(self, limit=50):
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise TaskError("invalid_limit")
        rows = self.conn.execute(
            "SELECT event_id,source_seq,instruction,lane,priority,state,"
            "claimed_by,result_note FROM task_handoffs "
            "ORDER BY CASE state WHEN 'claimed' THEN 0 WHEN 'queued' THEN 1 "
            "ELSE 2 END, priority DESC,source_seq LIMIT ?", (limit,),
        ).fetchall()
        return [
            {"event_id": r[0], "source_seq": r[1], "instruction": r[2],
             "lane": r[3], "priority": "urgent" if r[4] else "normal",
             "state": r[5], "worker": r[6], "result_note": r[7]}
            for r in rows
        ]

    def close(self):
        self.conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Durable DouDou child-task inbox")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("list", "claim", "finish", "progress"):
        p = sub.add_parser(action)
        p.add_argument("--db", required=True)
        if action in ("claim", "finish", "progress"):
            p.add_argument("--worker", required=True)
        if action == "list":
            p.add_argument("--limit", type=int, default=50)
        if action in ("finish", "progress"):
            p.add_argument("--event-id", required=True)
        if action == "progress":
            p.add_argument("--stage", required=True,
                           choices=("started", "working", "blocked"))
            p.add_argument("--summary", required=True)
        if action == "finish":
            
            p.add_argument("--status", required=True,
                           choices=("completed", "failed", "cancelled"))
            p.add_argument("--note", default="")
    args = parser.parse_args(argv)
    queue = TaskQueue(args.db)
    try:
        if args.action == "list":
            result = {"tasks": queue.list_tasks(args.limit)}
        elif args.action == "claim":
            result = {"task": queue.claim(args.worker)}
        elif args.action == "progress":
            result = {"stage": queue.report_progress(
                args.event_id, args.worker, args.stage, args.summary)}
        else:
            result = {"state": queue.finish(
                args.event_id, args.worker, args.status, args.note)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        queue.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (TaskError, OSError, sqlite3.Error) as exc:
        print("task_handoff_error:" + str(exc), file=sys.stderr)
        sys.exit(2)
