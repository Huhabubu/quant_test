import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from minecraft_doudou.account_state import AccountSelectionProbe
from minecraft_doudou.bridge import Controller, InvalidCommand, make_handler
from minecraft_doudou.events import MinecraftChatEvents

TOKEN = "a" * 40


class FakeConsole:
    def __init__(self):
        self.commands = []

    def available(self):
        return True

    def send(self, command):
        self.commands.append(command)


class AccountStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "last" / ".accounts.json"
        self.path.parent.mkdir()
        self.write("DouDou")
        self.probe = AccountSelectionProbe(self.path)

    def write(self, name):
        self.path.write_text(json.dumps(
            {"accounts": {"latest": [{"provider": "offline", "name": name}]}, "version": 0}
        ), encoding="utf-8")

    def force_new_timestamp(self):
        old = self.path.stat().st_mtime_ns
        os.utime(self.path, ns=(old + 10_000_000, old + 10_000_000))

    def test_stale_saved_selection_does_not_prove_current_session(self):
        self.probe.arm()
        self.assertFalse(self.probe.verify()["verified"])
        self.assertEqual(self.probe.verify()["reason"], "no_selection_write_since_prepare")

    def test_select_must_update_file_to_expected_account(self):
        self.probe.arm()
        self.write("Offline")
        self.force_new_timestamp()
        self.assertFalse(self.probe.verify()["verified"])
        self.write("DouDou")
        self.force_new_timestamp()
        self.assertTrue(self.probe.verify()["verified"])

    def test_controller_blocks_launch_until_account_selection_proven(self):
        console = FakeConsole()
        controller = Controller(console, "example.org", 25565,
                                mcdir="/tmp/gamedir", account_probe=self.probe)
        controller.prepare()
        self.assertEqual(console.commands, [
            "config set hmc.files.mc /tmp/gamedir",
            "account -p offline select DouDou",
        ])
        with self.assertRaises(InvalidCommand):
            controller.launch()
        self.write("DouDou")
        self.force_new_timestamp()
        self.assertEqual(controller.launch(), {"accepted": True, "verified": False})

    def test_account_endpoint_is_authenticated_and_returns_no_uuid(self):
        console = FakeConsole()
        controller = Controller(console, "example.org", 25565,
                                mcdir="/tmp/gamedir", account_probe=self.probe)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controller, TOKEN))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for token, expected in (("wrong", 401), (TOKEN, 200)):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                client.request("GET", "/v1/account", headers={"Authorization": "Bearer " + token})
                response = client.getresponse()
                self.assertEqual(response.status, expected)
                data = response.read().decode()
                self.assertNotIn("uuid", data.lower())
                client.close()
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()


class EventHttpTests(unittest.TestCase):
    def test_event_longpoll_and_reject_unknown_query(self):
        events = MinecraftChatEvents("/tmp/does-not-need-to-exist")
        controller = Controller(FakeConsole(), "example.org", 25565, events=events)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controller, TOKEN))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            events.publish_line("[CHAT] <Huhabubu> 你好豆豆\n")
            for path, code in (
                ("/v1/events?after=0&wait_ms=0", 200),
                ("/v1/events?after=-1", 400),
                ("/v1/events?after=foo", 400),
                ("/v1/events?unknown=1", 400),
            ):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                client.request("GET", path, headers={"Authorization": "Bearer " + TOKEN})
                response = client.getresponse()
                self.assertEqual(response.status, code)
                data = json.loads(response.read())
                if code == 200:
                    self.assertEqual(data["events"][0]["text"], "你好豆豆")
                client.close()
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
