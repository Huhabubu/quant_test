"""Local authenticated HTTP -> HeadlessMC console bridge (tmux or stdio).

Commands are queued to a real Minecraft client running hmc-specifics.
tmux acceptance does not prove in-game execution.
"""

import argparse
import hmac
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Defaults only; users with remapped keys should adjust after checking "key".
MOVE_KEYS = {"forward": "w", "back": "s", "left": "a", "right": "d"}


class InvalidCommand(ValueError):
    pass


class ConsoleUnavailable(RuntimeError):
    pass


def quote_hmc(text):
    """Quote one HeadlessMC argument; reject control characters."""
    if not isinstance(text, str) or not 1 <= len(text) <= 200:
        raise InvalidCommand("聊天内容必须为 1–200 个字符")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise InvalidCommand("聊天内容含有控制字符")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class TmuxConsole:
    """Send only to an existing session. Does not launch Minecraft."""

    def __init__(self, session, runner=subprocess.run):
        if not SESSION_PATTERN.fullmatch(session):
            raise ValueError("无效 tmux 会话名")
        self.session = session
        self.runner = runner
        self.lock = threading.Lock()

    def available(self):
        result = self.runner(
            ["tmux", "has-session", "-t", "=" + self.session],
            capture_output=True, text=True, check=False, timeout=5,
        )
        return result.returncode == 0

    def send(self, command):
        if "\n" in command or "\r" in command or "\x00" in command:
            raise InvalidCommand("无效控制台命令")
        with self.lock:
            if not self.available():
                raise ConsoleUnavailable("HeadlessMC tmux 会话不存在")
            # Send literal text and then Enter separately to avoid key-name parsing.
            for keys in (["-l", command], ["Enter"]):
                result = self.runner(
                    ["tmux", "send-keys", "-t", "=" + self.session, *keys],
                    capture_output=True, text=True, check=False, timeout=5,
                )
                if result.returncode != 0:
                    raise ConsoleUnavailable("无法向 HeadlessMC 控制台发送命令")


class Controller:
    def __init__(self, console, target_host, target_port):
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", target_host):
            raise ValueError("无效的服务器域名")
        if not 1 <= target_port <= 65535:
            raise ValueError("无效的服务器端口")
        self.console = console
        self.target_host = target_host
        self.target_port = target_port

    def status(self):
        return {
            "console_available": self.console.available(),
            "game_loaded": None,
            "server_connected": None,
            "note": "只验证控制台进程可用；尚不能证明客户端加载或连服成功",
        }

    def connect(self):
        self.console.send(f"connect {self.target_host} {self.target_port}")
        return {"accepted": True, "verified": False}

    def chat(self, text):
        self.console.send("msg " + quote_hmc(text))
        return {"accepted": True, "verified": False}

    def move(self, direction, duration_ms):
        if not isinstance(direction, str) or direction not in MOVE_KEYS:
            raise InvalidCommand("direction 必须为 forward/back/left/right")
        if type(duration_ms) is not int or not 50 <= duration_ms <= 1000:
            raise InvalidCommand("duration_ms 必须为 50–1000 的整数")
        self.console.send(f"key {MOVE_KEYS[direction]} --duration {duration_ms}")
        return {"accepted": True, "verified": False}


def make_handler(controller, token):
    """Keep credentials in HTTP; never log request bodies or headers."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def reply(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.reply(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"service": "doudou-bridge", "ok": True})
                return
            if not self.authorized():
                return
            if self.path == "/v1/status":
                try:
                    self.reply(200, controller.status())
                except (OSError, subprocess.TimeoutExpired):
                    self.reply(503, {"error": "console_unavailable"})
            elif self.path == "/v1/output":
                if hasattr(controller.console, "recent_output"):
                    self.reply(200, controller.console.recent_output())
                else:
                    self.reply(501, {"error": "output_not_supported"})
            else:
                self.reply(404, {"error": "not_found"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path not in ("/v1/chat", "/v1/connect", "/v1/move"):
                self.reply(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= 2048:
                    raise InvalidCommand("请求内容长度超限")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise InvalidCommand("请求体必须是 JSON 对象")
                if self.path == "/v1/chat":
                    if set(body) != {"text"}:
                        raise InvalidCommand("需要且只允许 text 字段")
                    result = controller.chat(body["text"])
                elif self.path == "/v1/move":
                    if set(body) != {"direction", "duration_ms"}:
                        raise InvalidCommand("move 需要 direction 和 duration_ms")
                    result = controller.move(body["direction"], body["duration_ms"])
                else:
                    if body:
                        raise InvalidCommand("connect 请求体必须为空对象")
                    result = controller.connect()
                self.reply(202, result)
            except (InvalidCommand, ValueError, UnicodeError, TypeError):
                self.reply(400, {"error": "invalid_request"})
            except (ConsoleUnavailable, OSError, subprocess.TimeoutExpired):
                self.reply(503, {"error": "console_unavailable"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="DouDou Minecraft local bridge")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tmux-session", default="doudou")
    parser.add_argument("--server-host", required=True)
    parser.add_argument("--server-port", type=int, required=True)
    args = parser.parse_args()
    token = os.environ.get("DOUDOU_BRIDGE_TOKEN", "")
    if len(token) < 32 or any(ord(char) <= 32 for char in token):
        parser.error("请设置至少 32 个无空白字符的 DOUDOU_BRIDGE_TOKEN")
    controller = Controller(TmuxConsole(args.tmux_session), args.server_host, args.server_port)
    # Loopback-only; first check whether Muse runs on the same VM.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(controller, token))
    print(f"DouDou bridge listening on http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
