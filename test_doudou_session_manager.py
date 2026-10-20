"""Deterministic tests: no real tmux, Java or Minecraft processes spawned."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from minecraft_doudou.session_manager import (
    SessionManager, StartBlocked, snapshot_status, java_candidates,
)


class FakeTmux:
    def __init__(self):
        self.alive = False
        self.commands = []
        self.fail_new = False

    def __call__(self, args, **kwargs):
        self.commands.append(list(args))
        if args[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(args, 0 if self.alive else 1, "", "")
        if args[:2] == ["tmux", "new-session"]:
            if self.fail_new:
                return subprocess.CompletedProcess(args, 1, "", "test launch failure")
            self.alive = True
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError("unexpected tmux command: " + str(args))


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mcdir = self.root / "gamedir"
        self.instance = self.mcdir / "neoforge-26.2"
        mods = self.instance / "mods"
        mods.mkdir(parents=True)
        config = self.instance / "config"
        config.mkdir()
        (config / "fml.toml").write_text("earlyWindowControl=false\n", encoding="utf-8")
        for name in ["hmc-specifics.jar", "world-bosses.jar", "geckolib.jar"]:
            (mods / name).touch()
        for i in range(22):
            (mods / ("extra-%d.jar" % i)).touch()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.fake = FakeTmux()
        self.mgr = SessionManager(self.mcdir, session="doudou-managed",
                                  runner=self.fake, proc_root=self.proc,
                                  lock_file=self.root / "locks" / "ensure.lock")
        self.java = ["/usr/bin/java", "-jar", str(self.root / "headlessmc.jar")]

    def heartbeat(self, seconds_ago=0, in_world=True):
        p = self.instance / "config" / "doudou-io"
        p.mkdir(exist_ok=True)
        (p / "state.json").write_text(json.dumps({
            "schema": 1, "in_world": in_world,
            "captured_at_ms": int((time.time() - seconds_ago) * 1000)
        }), encoding="utf-8")

    def test_first_ensure_starts_launcher_once_and_reuses(self):
        first = self.mgr.ensure(self.root, self.java)
        self.assertTrue(first["started"])
        self.assertEqual(first["outcome"], "started_launcher_only")
        calls = [c for c in self.fake.commands if c[:2] == ["tmux", "new-session"]]
        self.assertEqual(len(calls), 1)
        self.assertIn("-Dhmc.files.mc=" + str(self.mcdir), calls[0])
        self.assertEqual(self.mgr.ensure(self.root, self.java)["outcome"], "already_present")
        self.assertEqual(len([c for c in self.fake.commands
                              if c[:2] == ["tmux", "new-session"]]), 1)

    def test_session_reuse_even_when_game_state_exists(self):
        self.fake.alive = True
        self.heartbeat()
        self.assertFalse(self.mgr.ensure(self.root, self.java)["started"])

    def test_fresh_unmanaged_state_blocks_second_launch(self):
        self.heartbeat()
        with self.assertRaisesRegex(StartBlocked, "unmanaged"):
            self.mgr.ensure(self.root, self.java)
        self.assertNotIn("new-session", [x[1] for x in self.fake.commands])

    def test_unmanaged_java_pid_blocks_launch_with_stale_state(self):
        self.heartbeat(seconds_ago=120)
        pid = self.proc / "1234"
        pid.mkdir()
        (pid / "cmdline").write_bytes((
            "/usr/bin/java\0-Dhmc.files.mc=" + str(self.mcdir)
            + "\0-jar\0headlessmc.jar\0"
        ).encode())
        self.assertEqual(java_candidates(self.mcdir, self.proc), [1234])
        with self.assertRaisesRegex(StartBlocked, "unmanaged"):
            self.mgr.ensure(self.root, self.java)

    def test_stale_snapshot_is_not_claimed_healthy(self):
        self.heartbeat(seconds_ago=40)
        self.assertFalse(snapshot_status(self.instance)["fresh"])
        self.assertEqual(self.mgr.status()["reason"], "no_known_live_instance")
        self.assertIn("does not prove", self.mgr.status()["note"])

    def test_no_start_when_unsafe_installation(self):
        (self.instance / "config" / "fml.toml").write_text("earlyWindowControl=true")
        with self.assertRaises(ValueError):
            self.mgr.ensure(self.root, self.java)

    def test_invalid_java_args_fail_before_tmux_spawn(self):
        with self.assertRaises(ValueError):
            self.mgr.ensure(self.root, ["java", "-jar", "wrong.jar"])
        self.assertFalse(self.fake.alive)

    def test_tmux_failure_does_not_report_started(self):
        self.fake.fail_new = True
        with self.assertRaisesRegex(StartBlocked, "tmux_start_failed"):
            self.mgr.ensure(self.root, self.java)

    def test_status_never_stops_running_game(self):
        self.fake.alive = True
        self.mgr.status()
        self.assertTrue(all(c[1] != "kill-session" for c in self.fake.commands))

    def test_invalid_session(self):
        with self.assertRaises(ValueError):
            SessionManager(self.mcdir, session="doudou;kill", runner=self.fake)

if __name__ == "__main__":
    unittest.main()
