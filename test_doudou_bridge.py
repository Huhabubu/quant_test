import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from minecraft_doudou.bridge import (
    Controller, InvalidCommand, TmuxConsole, make_handler, quote_hmc,
)

TOKEN = "0123456789abcdef0123456789abcdef"


class FakeConsole:
    def __init__(self):
        self.commands = []

    def available(self):
        return True

    def send(self, command):
        self.commands.append(command)


class BridgeTests(unittest.TestCase):
    def test_quote_escapes_injection(self):
        self.assertEqual(quote_hmc('你好 "a\\b"'), '"你好 \\"a\\\\b\\""')
        for item in ["", "a\nb", "a\rb", "x\x00y", "a" * 201, 123]:
            with self.subTest(item=item), self.assertRaises(InvalidCommand):
                quote_hmc(item)

    def test_fixed_server_and_chat(self):
        console = FakeConsole()
        controller = Controller(console, "example.org", 25565)
        self.assertEqual(controller.connect(), {"accepted": True, "verified": False})
        self.assertEqual(controller.chat("hi"), {"accepted": True, "verified": False})
        self.assertEqual(console.commands, ["connect example.org 25565", 'msg "hi"'])
        self.assertIsNone(controller.status()["game_loaded"])

    def test_bounded_movement(self):
        console = FakeConsole()
        controller = Controller(console, "example.org", 25565)
        self.assertEqual(controller.move("forward", 300), {"accepted": True, "verified": False})
        self.assertEqual(controller.move("left", 50), {"accepted": True, "verified": False})
        self.assertEqual(console.commands, [
            "key w --duration 300",
            "key a --duration 50",
        ])
        for direction, duration in (
            ("forward", 0), ("forward", 1001), ("forward", True),
            ("jump", 200), ("w", 200), ("forward", 100.1),
        ):
            with self.subTest(direction=direction, duration=duration):
                with self.assertRaises(InvalidCommand):
                    controller.move(direction, duration)
        self.assertEqual(len(console.commands), 2)

    def test_preparation_required_and_one_shot_launch(self):
        console = FakeConsole()
        controller = Controller(console, "example.org", 25565, mcdir="/tmp/game")
        with self.assertRaises(InvalidCommand):
            controller.launch()
        answer = controller.prepare()
        self.assertFalse(answer["verified"])
        self.assertTrue(controller.status()["profile_preparation_submitted"])
        self.assertEqual(console.commands, [
            "config set hmc.mcdir /tmp/game",
            "account -p offline select DouDou",
        ])
        with self.assertRaises(InvalidCommand):
            controller.prepare()
        self.assertEqual(controller.launch(), {"accepted": True, "verified": False})
        with self.assertRaises(InvalidCommand):
            controller.launch()
        with self.assertRaises(InvalidCommand):
            controller.prepare()
        self.assertEqual(console.commands[-1],
                         'launch neoforge 26.2 --offline -lwjgl -eula -j "-Xmx5G"')
        self.assertEqual(len(console.commands), 3)

    def test_reject_unsafe_profile_data(self):
        for path in ("/tmp/x\nquit", "relative/dir", "/tmp/spaces here", "/tmp/a;quit"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                Controller(FakeConsole(), "example.org", 25565, mcdir=path)
        with self.assertRaises(ValueError):
            Controller(FakeConsole(), "example.org", 25565, mcdir="/tmp/game",
                       player_name="DouDou\nquit")

    def test_tmux_literal_command_and_enter(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return Mock(returncode=0)

        TmuxConsole("doudou", runner).send('msg "hello world"')
        self.assertEqual(calls, [
            ["tmux", "has-session", "-t", "=doudou"],
            ["tmux", "send-keys", "-t", "=doudou", "-l", 'msg "hello world"'],
            ["tmux", "send-keys", "-t", "=doudou", "Enter"],
        ])

    def test_invalid_session(self):
        for session in ("hello;rm", "../tmp", "a b"):
            with self.assertRaises(ValueError):
                TmuxConsole(session)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.console = FakeConsole()
        controller = Controller(self.console, "example.org", 25565, mcdir="/tmp/game")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controller, TOKEN))
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.worker.join(timeout=3)
        self.httpd.server_close()

    def request(self, method, path, body=None, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_auth_and_status(self):
        self.assertEqual(self.request("GET", "/v1/status", token="bad")[0], 401)
        status, data = self.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["console_available"])
        self.assertIsNone(data["server_connected"])

    def test_output_requires_auth_and_support(self):
        self.assertEqual(self.request("GET", "/v1/output", token="bad")[0], 401)
        code, payload = self.request("GET", "/v1/output")
        self.assertEqual(code, 501)
        self.assertEqual(payload["error"], "output_not_supported")

    def test_send_chat(self):
        self.assertEqual(self.request("POST", "/v1/chat", {"text": "你好"})[0], 202)
        self.assertEqual(self.console.commands, ['msg "你好"'])

    def test_no_arbitrary_console_command(self):
        for path, body in [
            ("/v1/chat", {"text": "hello\nquit"}),
            ("/v1/chat", {"command": "quit"}),
            ("/v1/connect", {"host": "evil.example"}),
        ]:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.request("POST", path, body)[0], 400)
        self.assertEqual(self.request("POST", "/v1/command", {})[0], 404)
        self.assertEqual(self.console.commands, [])

    def test_move_http_and_reject_injection(self):
        code, data = self.request("POST", "/v1/move", {
            "direction": "forward", "duration_ms": 250,
        })
        self.assertEqual(code, 202)
        self.assertFalse(data["verified"])
        self.assertEqual(self.console.commands, ["key w --duration 250"])
        for body in (
            {"direction": "forward; quit", "duration_ms": 200},
            {"direction": "forward", "duration_ms": 200000},
            {"direction": "right", "duration_ms": "300"},
            {"direction": "right", "duration_ms": True},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/v1/move", body)[0], 400)
        self.assertEqual(len(self.console.commands), 1)

    def test_prepare_then_launch_endpoints(self):
        self.assertEqual(self.request("POST", "/v1/launch", {})[0], 400)
        self.assertEqual(self.request("POST", "/v1/prepare", {"arbitrary": "yes"})[0], 400)
        code, data = self.request("POST", "/v1/prepare", {})
        self.assertEqual(code, 202)
        self.assertFalse(data["verified"])
        self.assertEqual(self.request("POST", "/v1/prepare", {})[0], 400)
        self.assertEqual(self.request("POST", "/v1/launch", {"command": "quit"})[0], 400)
        status, data = self.request("POST", "/v1/launch", {})
        self.assertEqual(status, 202)
        self.assertFalse(data["verified"])
        self.assertEqual(self.request("POST", "/v1/launch", {})[0], 400)
        self.assertEqual(self.console.commands, [
            "config set hmc.mcdir /tmp/game",
            "account -p offline select DouDou",
            'launch neoforge 26.2 --offline -lwjgl -eula -j "-Xmx5G"'
        ])

    def test_fixed_connect(self):
        self.assertEqual(self.request("POST", "/v1/connect", {})[0], 202)
        self.assertEqual(self.console.commands, ["connect example.org 25565"])


if __name__ == "__main__":
    unittest.main()
