"""Local authenticated HTTP -> HeadlessMC console bridge (tmux or stdio).

Commands are queued to a real Minecraft client running hmc-specifics.
tmux acceptance does not prove in-game execution.
"""

import argparse
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from .mod_mailbox import ModMailbox, ModUnavailable, InvalidModAction


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

    def launch_submitted(self):
        """Persisted in tmux itself; survives a lost/restarted HTTP bridge."""
        response = self.runner(
            ["tmux", "show-options", "-v", "-t", "=" + self.session,
             "@doudou_launch_submitted"],
            capture_output=True, text=True, check=False, timeout=5)
        return response.returncode == 0 and response.stdout.strip() == "1"

    def reserve_launch(self):
        """Cross-process fail-closed one-shot, even with multiple HTTP bridges.

        The lock serializes check+mark, then marking BEFORE sending intentionally
        requires operator intervention after uncertain delivery.
        """
        lockfile = Path.home() / ".local/state/doudou-bridge" / ("launch-" + self.session + ".lock")
        lockfile.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lockfile, flags, 0o600)
        try:
            with os.fdopen(fd, "rb", closefd=False):
                fcntl.flock(fd, fcntl.LOCK_EX)
                if not self.available():
                    raise ConsoleUnavailable("tmux 启动器会话已结束")
                if self.launch_submitted():
                    raise InvalidCommand("该 tmux 会话已经发过 launch，不得二次启动游戏")
                result = self.runner(
                    ["tmux", "set-option", "-t", "=" + self.session,
                     "@doudou_launch_submitted", "1"],
                    capture_output=True, text=True, check=False, timeout=5)
                if result.returncode != 0:
                    raise ConsoleUnavailable("无法持久记录 launch 门禁，拒绝启动")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def recent_output(self):
        result = self.runner(
            ["tmux", "capture-pane", "-p", "-t", "=" + self.session,
             "-S", "-40"],
            capture_output=True, text=True, check=False, timeout=5)
        if result.returncode != 0:
            raise ConsoleUnavailable("无法读取 tmux 控制台")
        return {
            "console_available": True,
            "lines": list(enumerate(result.stdout.splitlines()[-40:], start=1)),
            "partial": result.stdout.splitlines()[-1][-1000:] if result.stdout.splitlines() else "",
            "note": "tmux 当前屏幕输出，可能包含玩家聊天；不保证启动或游戏命令执行成功",
        }

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
    def __init__(self, console, target_host, target_port, mcdir=None, player_name="DouDou", events=None, account_probe=None, mailbox=None):
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", target_host):
            raise ValueError("无效的服务器域名")
        if not 1 <= target_port <= 65535:
            raise ValueError("无效的服务器端口")
        self.console = console
        self.mailbox = mailbox
        self.events = events
        self.account_probe = account_probe
        self.target_host = target_host
        self.target_port = target_port
        if mcdir is not None and not re.fullmatch(r"/[A-Za-z0-9_./-]+", mcdir):
            raise ValueError("游戏目录必须是无空格的安全绝对路径")
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", player_name):
            raise ValueError("无效的玩家昵称")
        self.mcdir = mcdir
        self.player_name = player_name
        self._prepared = False
        self._launch_lock = threading.Lock()
        self._launch_submitted = False

    def status(self):
        live = None
        if self.mailbox is not None:
            try:
                state = self.mailbox.state()
                live = state.get("in_world")
            except ModUnavailable:
                pass
        submitted = self._launch_submitted
        if hasattr(self.console, "launch_submitted"):
            submitted = submitted or self.console.launch_submitted()
        return {
            "console_available": self.console.available(),
            "launcher_exit_code": (self.console.process.poll() if hasattr(self.console, "process")
                                   and self.console.process is not None else None),
            "game_loaded": live,
            "profile_preparation_submitted": self._prepared,
            "launch_submitted": submitted,
            "account_verified": (self.account_probe.verify()["verified"] if self.account_probe else None),
            "server_connected": True if live is True else None,
            "game_state_fresh": live is not None,
            "note": "in_world=True 仅证明最近客户端处于世界；tmux 重连不代表服务器或聊天通道无故障",
        }

    def prepare(self):
        """Reapply both HMC profile and existing offline account in this session.

        Console echoes/logs must be reviewed: send() cannot prove selection worked.
        This does NOT create or replace the persisted account UUID.
        """
        with self._launch_lock:
            if self._launch_submitted or (hasattr(self.console, "launch_submitted")
                                          and self.console.launch_submitted()):
                raise InvalidCommand("该控制台已提交过 launch，不能重新修改账号或配置")
            if self._prepared:
                raise InvalidCommand("启动配置已发送，不允许重复准备")
            if self.mcdir is None:
                raise InvalidCommand("未指定 --mcdir，拒绝准备启动配置")
            self.console.send("config set hmc.files.mc " + self.mcdir)
            if self.account_probe:
                self.account_probe.arm()
            self.console.send("account -p offline select " + self.player_name)
            self._prepared = True
        return {
            "accepted": True, "verified": False,
            "note": "请先查看 /v1/output，确认离线账号 DouDou 已存在且未报错，再调用 launch",
        }

    def launch(self):
        """Submit the exact last-known-good NeoForge command once per bridge run."""
        with self._launch_lock:
            if self._launch_submitted or (hasattr(self.console, "launch_submitted")
                                          and self.console.launch_submitted()):
                raise InvalidCommand("本控制台已提交过 launch；禁止重复启动")
            if self.mailbox is not None:
                try:
                    if self.mailbox.state().get("in_world"):
                        raise InvalidCommand("Mod 仍在世界中，拒绝重复启动")
                except ModUnavailable:
                    pass
            if self.mcdir is not None and not self._prepared:
                raise InvalidCommand("必须先调用 /v1/prepare 并核对控制台输出")
            if self.account_probe is not None and not self.account_probe.verify()["verified"]:
                raise InvalidCommand("无法证明本会话已选择 DouDou，拒绝启动")
            if hasattr(self.console, "reserve_launch"):
                self.console.reserve_launch()
            self._launch_submitted = True  # fail closed even if send() fails
            self.console.send('launch neoforge 26.2 --offline -lwjgl -eula -j "-Xmx3G"')
            self._launch_submitted = True
        return {"accepted": True, "verified": False}

    def connect(self):
        if self.mailbox is not None:
            try:
                if self.mailbox.state().get("in_world"):
                    raise InvalidCommand("已在游戏世界中，拒绝再次连接")
            except ModUnavailable:
                pass
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
            if self.path == "/v1/account":
                if controller.account_probe is None:
                    self.reply(501, {"error": "account_proof_not_configured"})
                else:
                    self.reply(200, controller.account_probe.verify())
            elif self.path == "/v1/game-state":
                if controller.mailbox is None:
                    self.reply(501, {"error": "client_mod_not_configured"})
                else:
                    try:
                        self.reply(200, controller.mailbox.state())
                    except ModUnavailable:
                        self.reply(503, {"error": "client_mod_unavailable_or_stale"})
            elif self.path.startswith("/v1/landmarks"):
                parsed = urlsplit(self.path)
                if parsed.path != "/v1/landmarks":
                    self.reply(404, {"error": "not_found"})
                    return
                if controller.mailbox is None:
                    self.reply(501, {"error": "client_mod_not_configured"})
                    return
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {"kind", "limit"} or any(len(v) != 1 for v in query.values()):
                        raise InvalidModAction("invalid_query")
                    kind = query.get("kind", [None])[0]
                    if kind == "":
                        raise InvalidModAction("invalid_kind")
                    limit_text = query.get("limit", ["100"])[0]
                    if not limit_text.isdigit():
                        raise InvalidModAction("invalid_limit")
                    self.reply(200, controller.mailbox.landmarks(kind, int(limit_text)))
                except InvalidModAction:
                    self.reply(400, {"error": "invalid_request"})
                except ModUnavailable:
                    self.reply(503, {"error": "client_mod_unavailable_or_stale"})
            elif self.path.startswith("/v1/game-action?"):
                if controller.mailbox is None:
                    self.reply(501, {"error": "client_mod_not_configured"})
                    return
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    if set(query) != {"id"} or len(query["id"]) != 1:
                        raise InvalidModAction("需要单个 id")
                    self.reply(200, controller.mailbox.result(query["id"][0]))
                except InvalidModAction:
                    self.reply(400, {"error": "invalid_request"})
                except ModUnavailable:
                    self.reply(503, {"error": "client_mod_unavailable"})
            elif self.path.startswith("/v1/events"):
                parsed = urlsplit(self.path)
                if parsed.path != "/v1/events":
                    self.reply(404, {"error": "not_found"})
                    return
                if controller.events is None:
                    self.reply(501, {"error": "events_not_supported"})
                    return
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) - {"after", "wait_ms"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("invalid query")
                    args = {key: int(query[key][0]) for key in query}
                    if any(not query[k][0].isdigit() for k in query):
                        raise ValueError("invalid number")
                    self.reply(200, controller.events.get(**args))
                except (TypeError, ValueError):
                    self.reply(400, {"error": "invalid_query"})
            elif self.path == "/v1/status":
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
            if self.path not in ("/v1/chat", "/v1/connect", "/v1/move", "/v1/launch", "/v1/prepare", "/v1/game-action"):
                self.reply(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= 2048:
                    raise InvalidCommand("请求内容长度超限")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise InvalidCommand("请求体必须是 JSON 对象")
                if self.path == "/v1/game-action":
                    if controller.mailbox is None:
                        self.reply(501, {"error": "client_mod_not_configured"})
                        return
                    result = controller.mailbox.submit(body)
                elif self.path == "/v1/chat":
                    if set(body) != {"text"}:
                        raise InvalidCommand("需要且只允许 text 字段")
                    result = controller.chat(body["text"])
                elif self.path == "/v1/prepare":
                    if body:
                        raise InvalidCommand("prepare 请求体必须为空对象")
                    result = controller.prepare()
                elif self.path == "/v1/launch":
                    if body:
                        raise InvalidCommand("launch 请求体必须为空对象")
                    result = controller.launch()
                elif self.path == "/v1/move":
                    if set(body) != {"direction", "duration_ms"}:
                        raise InvalidCommand("move 需要 direction 和 duration_ms")
                    result = controller.move(body["direction"], body["duration_ms"])
                else:
                    if body:
                        raise InvalidCommand("connect 请求体必须为空对象")
                    result = controller.connect()
                self.reply(202, result)
            except (InvalidCommand, InvalidModAction, ValueError, UnicodeError, TypeError):
                self.reply(400, {"error": "invalid_request"})
            except ModUnavailable:
                self.reply(503, {"error": "client_mod_unavailable_or_stale"})
            except (ConsoleUnavailable, OSError, subprocess.TimeoutExpired):
                self.reply(503, {"error": "console_unavailable"})

    return Handler


def main():
    """Attach bridge to existing tmux-managed HeadlessMC; never own/kill Java."""
    from .preflight import inspect_installation, PreflightError
    from .account_state import AccountSelectionProbe
    from .events import MinecraftChatEvents
    from .mod_mailbox import ModMailbox

    parser = argparse.ArgumentParser(description="DouDou attachable local HTTP bridge")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tmux-session", default="doudou-managed")
    parser.add_argument("--mcdir", required=True)
    parser.add_argument("--account-state-file", required=True)
    parser.add_argument("--owner-name", default="Huhabubu")
    parser.add_argument("--server-host", required=True)
    parser.add_argument("--server-port", type=int, required=True)
    args = parser.parse_args()
    token = os.environ.get("DOUDOU_BRIDGE_TOKEN", "")
    if len(token) < 32 or any(ord(char) <= 32 for char in token):
        parser.error("请设置至少 32 个无空白字符的 DOUDOU_BRIDGE_TOKEN")
    try:
        installation = inspect_installation(args.mcdir)
    except (OSError, PreflightError) as exc:
        parser.error("游戏目录预检失败: " + str(exc))
    console = TmuxConsole(args.tmux_session)
    if not console.available():
        parser.error("tmux session 不存在，请先运行 session_manager ensure（不会自动启动第二个客户端）")
    events = MinecraftChatEvents(
        os.path.join(installation["instance"], "logs", "latest.log"),
        allowed_sender=args.owner_name)
    mailbox = ModMailbox(os.path.join(installation["instance"], "config", "doudou-io"))
    probe = AccountSelectionProbe(args.account_state_file)
    controller = Controller(console, args.server_host, args.server_port,
        mcdir=installation["mcdir"], account_probe=probe, events=events,
        mailbox=mailbox)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(controller, token))
    try:
        events.start()
        print(f"DouDou REATTACH bridge at http://127.0.0.1:{args.port}; tmux={args.tmux_session}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        events.stop()
        # NEVER terminate or kill HMC; that process belongs to tmux.


if __name__ == "__main__":
    main()
