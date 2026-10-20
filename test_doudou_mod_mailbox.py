import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from minecraft_doudou.bridge import Controller, make_handler
from minecraft_doudou.mod_mailbox import ModMailbox, ModUnavailable, InvalidModAction

TOKEN = "b" * 40


class FakeConsole:
    def available(self):
        return True

    def send(self, command):
        raise AssertionError("game action must never be passed as raw console command")


class ModMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "commands").mkdir()
        (self.root / "results").mkdir()
        self.box = ModMailbox(self.root)
        self.write_state()

    def write_state(self, **overrides):
        data = {
            "schema": 1,
            "captured_at_ms": int(time.time() * 1000),
            "in_world": True,
            "session_id": "e" * 32,
            "landmark_memory_scope": "a" * 64,
            "dead": False,
            "hunger": 6,
            "inventory": [
                {"id": "minecraft:golden_carrot", "count": 5, "slot": 4},
                {"id": "minecraft:oak_log", "count": 42, "slot": 10},
            ]
        }
        data.update(overrides)
        (self.root / "state.json").write_text(json.dumps(data), encoding="utf-8")

    def test_live_state_reports_true_counts(self):
        state = self.box.state()
        self.assertEqual(state["inventory"][1]["count"], 42)
        self.write_state(captured_at_ms=0)
        with self.assertRaises(ModUnavailable):
            self.box.state()

    def test_reject_arbitrary_actions_and_injection(self):
        invalid = [
            {"action": "op"},
            {"action": "eat", "slot": True},
            {"action": "eat", "slot": 9},
            {"action": "eat", "extra": "shell"},
            {"action": "sleep", "x": 1, "y": 2},
            {"action": "sleep", "x": 1.5, "y": 2, "z": 3},
            {"action": "open_container", "x": 0, "y": 20, "z": 30_000_001},
            {"action": "eat", "slot": 4, "command": "quit"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(InvalidModAction):
                self.box.submit(value)
        self.assertEqual(list((self.root / "commands").iterdir()), [])

    def test_eat_queue_receipt_and_no_auto_retry(self):
        result = self.box.submit({"action": "eat", "slot": 4})
        id_ = result["id"]
        self.assertFalse(result["verified"])
        pending = self.root / "commands" / (id_ + ".json")
        queued = json.loads(pending.read_text())
        self.assertEqual(queued["action"], "eat")
        self.assertEqual(queued["slot"], 4)
        self.assertEqual(queued["session_id"], "e" * 32)
        self.assertLess(abs(queued["created_at_ms"] - int(time.time() * 1000)), 3000)
        self.assertEqual(self.box.result(id_)["status"], "queued")
        pending.rename(self.root / "commands" / (id_ + ".processing"))
        self.assertEqual(self.box.result(id_)["status"], "unknown_after_claim")
        (self.root / "results" / (id_ + ".json")).write_text(json.dumps({
            "id": id_, "accepted": True, "verified": False
        }))
        self.assertEqual(self.box.result(id_)["status"], "completed")
        self.assertFalse(self.box.result(id_)["result"]["verified"])

    def test_in_progress_eating_does_not_appear_crashed(self):
        request = self.box.submit({"action": "eat"})
        id_ = request["id"]
        pending = self.root / "commands" / (id_ + ".json")
        pending.rename(self.root / "commands" / (id_ + ".processing"))
        self.write_state(eating_active=True, eating_id=id_, eating_ticks_elapsed=32)
        result = self.box.result(id_)
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(result["ticks_elapsed"], 32)
        self.write_state(eating_active=False)
        self.assertEqual(self.box.result(id_)["status"], "unknown_after_claim")

    def test_fail_closed_when_not_in_world(self):
        for values in ({"in_world": False}, {"dead": True}):
            self.write_state(**values)
            with self.assertRaises(ModUnavailable):
                self.box.submit({"action": "eat"})

    def test_missing_or_invalid_session_rejected(self):
        self.write_state(session_id="invalid")
        with self.assertRaises(ModUnavailable):
            self.box.submit({"action": "eat"})

    def write_landmarks(self):
        records = [
            {"scope": "a" * 64, "dimension": "overworld", "x": 4, "y": 69,
             "z": -48, "kind": "container", "block_id": "minecraft:chest",
             "last_seen_at_ms": 10},
            {"scope": "a" * 64, "dimension": "overworld", "x": -4, "y": 65,
             "z": -44, "kind": "bed", "block_id": "minecraft:white_bed",
             "last_seen_at_ms": 12},
            {"scope": "b" * 64, "dimension": "the_nether", "x": 4, "y": 69,
             "z": -48, "kind": "container", "block_id": "minecraft:chest",
             "last_seen_at_ms": 20},
        ]
        (self.root / "landmarks.json").write_text(json.dumps(
            {"schema": 1, "entries": records}), encoding="utf-8")

    def test_landmark_query_scoped_durable_and_not_assumed_current(self):
        self.write_landmarks()
        answer = self.box.landmarks()
        self.assertEqual(answer["count_matching"], 2)
        self.assertEqual(answer["entries"][0]["kind"], "bed")
        self.assertIn("historical", answer["verification"])
        self.assertEqual(self.box.landmarks("container")["entries"][0]["x"], 4)
        self.assertEqual(len(self.box.landmarks(limit=1)["entries"]), 1)
        self.assertTrue(self.box.landmarks(limit=1)["truncated"])
        with self.assertRaises(InvalidModAction):
            self.box.landmarks(kind="../../etc/passwd")
        with self.assertRaises(InvalidModAction):
            self.box.landmarks(limit=100000)
        self.write_state(landmark_memory_scope="invalid")
        with self.assertRaises(ModUnavailable):
            self.box.landmarks()

    def test_bounded_block_interaction_payload(self):
        result = self.box.submit({"action": "sleep", "x": 3, "y": 65, "z": -4})
        self.assertEqual(self.box.result(result["id"])["status"], "queued")

    def test_http_auth_and_methods(self):
        controller = Controller(FakeConsole(), "example.org", 25565, mailbox=self.box)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controller, TOKEN))
        worker = threading.Thread(target=srv.serve_forever, daemon=True)
        worker.start()
        try:
            def req(method, path, obj=None, token=TOKEN):
                cli = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=2)
                headers = {"Authorization": "Bearer " + token}
                if obj is not None:
                    headers["Content-Type"] = "application/json"
                cli.request(method, path, body=json.dumps(obj) if obj is not None else None, headers=headers)
                rsp = cli.getresponse()
                code, data = rsp.status, json.loads(rsp.read())
                cli.close()
                return code, data
            self.assertEqual(req("GET", "/v1/game-state", token="wrong")[0], 401)
            self.assertEqual(req("GET", "/v1/game-state")[1]["inventory"][1]["count"], 42)
            self.write_landmarks()
            self.assertEqual(req("GET", "/v1/landmarks?kind=bed")[1]["entries"][0]["kind"], "bed")
            self.assertEqual(req("GET", "/v1/landmarks?kind=bed", token="wrong")[0], 401)
            self.assertEqual(req("GET", "/v1/landmarks?limit=0")[0], 400)
            self.assertEqual(req("POST", "/v1/game-action", {"action": "jump"})[0], 400)
            code, submitted = req("POST", "/v1/game-action", {"action": "eat"})
            self.assertEqual(code, 202)
            code, result = req("GET", "/v1/game-action?id=" + submitted["id"])
            self.assertEqual(code, 200)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(req("GET", "/v1/game-action?id=bad")[0], 400)
        finally:
            srv.shutdown()
            worker.join(timeout=3)
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
