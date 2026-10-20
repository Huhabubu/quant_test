"""Bridge a HeadlessMC process owned by this Python process via stdin/stdout.

Run this supervisor on the SAME VM with the SAME Java startup argv. It cannot
attach to an independently started Java process's stdin.
"""

import argparse
from collections import deque
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer

from .bridge import ConsoleUnavailable, Controller, InvalidCommand, make_handler


class StdioConsole:
    def __init__(self, argv, cwd=None, process_factory=subprocess.Popen):
        if not argv:
            raise ValueError("必须提供 HeadlessMC 启动命令")
        self.argv = list(argv)
        self.cwd = cwd
        self.process_factory = process_factory
        self.process = None
        self.lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.lines = deque(maxlen=100)
        self.line_number = 0
        self.reader = None

    def start(self):
        if self.process is not None:
            raise RuntimeError("HeadlessMC 已由此控制桥启动")
        self.process = self.process_factory(
            self.argv, cwd=self.cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.reader = threading.Thread(target=self._drain_output, daemon=True)
        self.reader.start()

    def _drain_output(self):
        # Always drain the PIPE; otherwise Java can block on a full stdout pipe.
        for line in self.process.stdout:
            with self.output_lock:
                self.line_number += 1
                self.lines.append([self.line_number, line.rstrip("\r\n")[:1000]])

    def available(self):
        return self.process is not None and self.process.poll() is None

    def send(self, command):
        if not isinstance(command, str) or any(c in command for c in "\r\n\0"):
            raise InvalidCommand("无效的控制台命令")
        with self.lock:
            if not self.available():
                raise ConsoleUnavailable("HeadlessMC 未运行或已退出")
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ConsoleUnavailable("HeadlessMC stdin 不可写") from exc

    def recent_output(self):
        with self.output_lock:
            return {
                "console_available": self.available(),
                "last_sequence": self.line_number,
                "lines": list(self.lines)[-40:],
                "note": "仅为控制台最近输出，不代表游戏动作已确认执行；内容可能包含游戏聊天"
            }

    def stop(self):
        # Only stop the Java process which THIS bridge started.
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdin:
            self.process.stdin.close()
        if self.reader is not None:
            self.reader.join(timeout=2)
        if self.process.stdout and (self.reader is None or not self.reader.is_alive()):
            self.process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description="HeadlessMC stdin/stdout local bridge")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cwd", help="HeadlessMC 运行目录")
    parser.add_argument("--server-host", required=True)
    parser.add_argument("--server-port", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="在 -- 后原样传入原来的 Java 启动命令及参数")
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("请在 -- 后提供完整的 HeadlessMC 启动命令")
    token = os.environ.get("DOUDOU_BRIDGE_TOKEN", "")
    if len(token) < 32 or any(ord(char) <= 32 for char in token):
        parser.error("请设置至少 32 个无空白字符的 DOUDOU_BRIDGE_TOKEN")
    console = StdioConsole(command, cwd=args.cwd)
    controller = Controller(console, args.server_host, args.server_port)
    # Bind BEFORE launching Java so port conflicts cannot spawn a second client.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(controller, token))
    try:
        console.start()
        print("Local bridge at http://127.0.0.1:%d; HMC PID=%d" % (args.port, console.process.pid), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        console.stop()


if __name__ == "__main__":
    main()
