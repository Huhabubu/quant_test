"""Linux PTY adapter matching HeadlessMC's interactive console."""

import codecs
import errno
import os
import pty
import signal
import subprocess
import threading
import time

from .bridge import ConsoleUnavailable, InvalidCommand
from .stdio_bridge import StdioConsole


class OwnedPtyProcess:
    """Operate on the child we started; never attach to arbitrary PIDs."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        if self.returncode is None:
            try:
                found, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.returncode = -1
            else:
                if found:
                    self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.05)
        return self.returncode

    def _signal_group(self, number):
        if self.poll() is None:
            try:
                os.killpg(self.pid, number)
            except ProcessLookupError:
                pass

    def terminate(self):
        self._signal_group(signal.SIGTERM)

    def kill(self):
        self._signal_group(signal.SIGKILL)


class PtyConsole(StdioConsole):
    """Attach a real controlling PTY to the launcher started by the bridge."""

    def __init__(self, argv, cwd=None):
        super().__init__(argv, cwd=cwd)
        self.master = None

    def start(self):
        if self.process is not None:
            raise RuntimeError("HeadlessMC 已启动")
        pid, master = pty.fork()
        if pid == 0:
            try:
                if self.cwd:
                    os.chdir(self.cwd)
                os.execvp(self.argv[0], self.argv)
            except Exception as exc:
                os.write(2, ("HeadlessMC exec failed: " + str(exc) + "\n").encode())
                os._exit(127)
        self.process = OwnedPtyProcess(pid)
        self.master = master
        self.reader = threading.Thread(target=self._drain_pty, daemon=True)
        self.reader.start()

    def _drain_pty(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        while True:
            try:
                raw = os.read(self.master, 4096)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                break
            if not raw:
                break
            pending += decoder.decode(raw).replace("\r\n", "\n").replace("\r", "\n")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                self._append_line(line)
            if len(pending) > 8000:
                self._append_line(pending[:1000])
                pending = ""
        pending += decoder.decode(b"", final=True)
        if pending:
            self._append_line(pending)

    def _append_line(self, line):
        with self.output_lock:
            self.line_number += 1
            self.lines.append([self.line_number, line[:1000]])

    def send(self, command):
        if not isinstance(command, str) or any(c in command for c in "\r\n\0"):
            raise InvalidCommand("无效的控制台命令")
        with self.lock:
            if not self.available():
                raise ConsoleUnavailable("HeadlessMC 未运行")
            try:
                os.write(self.master, (command + "\n").encode("utf-8"))
            except OSError as exc:
                raise ConsoleUnavailable("HeadlessMC PTY 不可写") from exc

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        if self.reader is not None:
            self.reader.join(timeout=2)
