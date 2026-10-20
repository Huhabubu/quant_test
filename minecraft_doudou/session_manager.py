"""Fail-closed HeadlessMC tmux session guardian for Muse VM.

The tmux server owns the Java launcher terminal, not a transient agent process.
Calling ensure again reuses an existing tmux session without killing Minecraft.
Account selection, game launch and connect require separate deliberate steps.

No cron/systemd or automatic restarts are installed.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .bridge import SESSION_PATTERN
from .preflight import PreflightError, build_java_argv, inspect_installation

DEFAULT_SESSION = "doudou-managed"
STATE_DIR = Path.home() / ".local/state/doudou-bridge"


class StartBlocked(RuntimeError):
    pass


def snapshot_status(instance, *, clock=time.time, freshness_seconds=8):
    """Read-only heartbeat. Does not prove control or server connection."""
    file = Path(instance) / "config/doudou-io/state.json"
    try:
        if file.stat().st_size > 131072:
            return {"fresh": False, "reason": "oversize"}
        data = json.loads(file.read_text(encoding="utf-8"))
        if data.get("schema") != 1 or not isinstance(data.get("in_world"), bool):
            return {"fresh": False, "reason": "invalid_schema"}
        ts = data.get("captured_at_ms")
        if type(ts) is not int:
            return {"fresh": False, "reason": "invalid_timestamp"}
        age = clock() - ts / 1000.0
        if -1 <= age <= freshness_seconds:
            return {"fresh": True, "in_world": data["in_world"],
                    "age_seconds": round(age, 2)}
        return {"fresh": False, "reason": "stale",
                "age_seconds": round(age, 2)}
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        return {"fresh": False, "reason": "unavailable"}


def java_candidates(mcdir, proc_root="/proc"):
    """Best-effort positive evidence of a Java game; never send signals.

    A zero result CANNOT prove an unmanaged process does not exist.
    """
    root = str(Path(mcdir).resolve())
    candidates = []
    try:
        proc_entries = list(Path(proc_root).iterdir())
    except OSError:
        return []
    for pid_dir in proc_entries:
        if not pid_dir.name.isdecimal():
            continue
        try:
            raw = (pid_dir / "cmdline").read_bytes()
            if len(raw) > 262144:
                continue
            argv = [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]
            if not argv or "java" not in Path(argv[0]).name.lower():
                continue
            joined = " ".join(argv).lower()
            if root in " ".join(argv) and any(word in joined
                    for word in ("headlessmc", "neoforge", "minecraft")):
                candidates.append(int(pid_dir.name))
        except OSError:
            continue
    return sorted(candidates)


@contextmanager
def exclusive_lock(path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise StartBlocked("unsafe_lock_symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class SessionManager:
    def __init__(self, mcdir, *, session=DEFAULT_SESSION, runner=subprocess.run,
                 lock_file=None, proc_root="/proc", clock=time.time):
        if not SESSION_PATTERN.fullmatch(session):
            raise ValueError("无效 tmux session 名")
        self.mcdir = str(Path(mcdir).expanduser().resolve())
        self.instance = str(Path(self.mcdir) / "neoforge-26.2")
        self.session = session
        self.runner = runner
        self.lock_file = Path(lock_file or STATE_DIR / "manager.lock")
        self.proc_root = proc_root
        self.clock = clock

    def _run(self, args, *, timeout=10, env=None):
        return self.runner(args, capture_output=True, text=True,
                           check=False, timeout=timeout, env=env)

    def has_session(self):
        result = self._run(["tmux", "has-session", "-t", "=" + self.session])
        return result.returncode == 0

    def status(self):
        active = self.has_session()
        heartbeat = snapshot_status(self.instance, clock=self.clock)
        pids = java_candidates(self.mcdir, self.proc_root)
        if active:
            reason = "terminal_session_present"
        elif heartbeat["fresh"] or pids:
            reason = "unmanaged_game_may_be_running"
        else:
            reason = "no_known_live_instance"
        return {"tmux_session": self.session, "tmux_attached": active,
                "game_state": heartbeat, "matching_java_pids": pids,
                "reason": reason,
                "note": "Never kill existing Java. Stale/absent state does not prove exit."}

    def ensure(self, cwd, java_argv):
        """Start the launcher once, never automatically launch/connect game."""
        with exclusive_lock(self.lock_file):
            if self.has_session():
                return dict(self.status(), outcome="already_present", started=False)
            current = self.status()
            if current["game_state"]["fresh"] or current["matching_java_pids"]:
                raise StartBlocked("unmanaged_game_may_be_running: refuse_duplicate")
            installation = inspect_installation(self.mcdir)
            directory = Path(cwd).expanduser().resolve(strict=True)
            if not directory.is_dir():
                raise StartBlocked("invalid_working_directory")
            argv = build_java_argv(java_argv, installation["mcdir"])
            env = os.environ.copy()
            env["LC_ALL"] = "C.UTF-8"
            args = ["tmux", "new-session", "-d", "-s", self.session,
                    "-c", str(directory), "--", *argv]
            result = self._run(args, timeout=15, env=env)
            if result.returncode != 0:
                if self.has_session():
                    return dict(self.status(), outcome="session_exists_after_race",
                                started=False)
                raise StartBlocked("tmux_start_failed: " + result.stderr.strip()[:200])
            if not self.has_session():
                raise StartBlocked("launcher_exited_immediately_check_logs")
            return dict(self.status(), outcome="started_launcher_only",
                        started=True,
                        next_step="Verify account and then manually launch/connect")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Idempotent tmux HMC launcher")
    parser.add_argument("action", choices=["status", "ensure"])
    parser.add_argument("--mcdir", required=True)
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--cwd", help="Existing HeadlessMC directory for ensure")
    parser.add_argument("--lock-file", help=argparse.SUPPRESS)
    parser.add_argument("java", nargs=argparse.REMAINDER,
                        help="For ensure: -- java -jar headlessmc.jar")
    args = parser.parse_args(argv)
    manager = SessionManager(args.mcdir, session=args.session, lock_file=args.lock_file)
    try:
        if args.action == "status":
            response = manager.status()
        else:
            cmd = args.java[1:] if args.java and args.java[0] == "--" else args.java
            if not args.cwd or not cmd:
                parser.error("ensure requires --cwd and -- java -jar headlessmc.jar")
            response = manager.ensure(args.cwd, cmd)
    except (StartBlocked, PreflightError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **response}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
