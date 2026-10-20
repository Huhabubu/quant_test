"""Linux PTY for HeadlessMC: visible prompts, bounded diagnostics, controlled process."""

import codecs
import errno
import os
from pathlib import Path
import pty
import signal
import subprocess
import threading
import time

from .bridge import ConsoleUnavailable, InvalidCommand
from .stdio_bridge import StdioConsole


class OwnedPtyProcess:
    """Only signal the process group launched by our own PTY fork."""

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
    """A real interactive terminal plus non-blocking observability.

    Java stdout and stderr share the controlling terminal (they cannot be
    separated on a PTY). Private on-disk diagnostics retain the last two
    bounded log files, even if this bridge/VM session later disappears.
    """

    def __init__(self, argv, cwd=None, diagnostic_path=None):
        super().__init__(argv, cwd=cwd)
        self.master = None
        self.partial = ""
        self.diagnostic_path = Path(
            diagnostic_path or (Path.home() / ".local/state/doudou-bridge/launcher.pty.log")
        ).expanduser()
        self._exit_noted = False
        self._diagnostic_fd = None

    def _open_diagnostics(self):
        self.diagnostic_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # No credentials must be sent as console commands; nevertheless the
        # console can contain private chat, so only the owner may read this.
        if self.diagnostic_path.exists() and self.diagnostic_path.stat().st_size > 2_000_000:
            self.diagnostic_path.replace(self.diagnostic_path.with_suffix(".previous.log"))
        self._diagnostic_fd = os.open(
            self.diagnostic_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600
        )
        os.chmod(self.diagnostic_path, 0o600)

    def _persist(self, raw):
        if self._diagnostic_fd is None:
            return
        try:
            os.write(self._diagnostic_fd, raw)
            if os.fstat(self._diagnostic_fd).st_size > 2_000_000:
                os.close(self._diagnostic_fd)
                self._diagnostic_fd = None
                self.diagnostic_path.replace(self.diagnostic_path.with_suffix(".previous.log"))
                self._open_diagnostics()
        except OSError:
            # Output visibility in memory must work even if the disk fills.
            pass

    def start(self):
        if self.process is not None:
            raise RuntimeError("HeadlessMC 已启动")
        self._open_diagnostics()
        try:
            pid, master = pty.fork()
        except BaseException:
            self._close_diagnostics()
            raise
        if pid == 0:
            try:
                if self.cwd:
                    os.chdir(self.cwd)
                locale = (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE")
                          or os.environ.get("LANG") or "")
                if "utf-8" not in locale.lower() and "utf8" not in locale.lower():
                    os.environ["LC_ALL"] = "C.UTF-8"
                # Do not inherit the private diagnostic descriptor in Java.
                if self._diagnostic_fd is not None:
                    os.close(self._diagnostic_fd)
                os.execvp(self.argv[0], self.argv)
            except Exception as exc:
                os.write(2, ("HeadlessMC exec failed: " + str(exc) + "\n").encode())
                os._exit(127)
        self.process = OwnedPtyProcess(pid)
        self.master = master
        self.reader = threading.Thread(target=self._drain_pty, daemon=True)
        self.reader.start()

    def _append_line(self, line):
        with self.output_lock:
            self.line_number += 1
            self.lines.append([self.line_number, line[:1000]])

    def _drain_pty(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        try:
            while True:
                try:
                    raw = os.read(self.master, 4096)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    self._append_line("[bridge] PTY read error: " + repr(exc))
                    break
                if not raw:
                    break
                self._persist(raw)
                pending += decoder.decode(raw).replace("\r\n", "\n").replace("\r", "\n")
                # JLine often prints prompts without a newline. Keep the
                # incomplete tail *visible* even while the child is alive.
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    self._append_line(line)
                if len(pending) > 8000:
                    self._append_line(pending[:1000])
                    pending = pending[-2000:]
                with self.output_lock:
                    self.partial = pending[-2000:]
        finally:
            pending += decoder.decode(b"", final=True)
            if pending:
                self._append_line(pending)
            with self.output_lock:
                self.partial = ""
            self._note_exit()
            self._close_diagnostics()

    def _note_exit(self):
        code = self.process.poll() if self.process is not None else None
        if code is not None and not self._exit_noted:
            self._exit_noted = True
            msg = "[bridge] HeadlessMC launcher exited: returncode=%d" % code
            self._append_line(msg)
            self._persist((msg + "\n").encode())

    def recent_output(self):
        result = super().recent_output()
        with self.output_lock:
            result["partial"] = self.partial[-2000:]
        result["exit_code"] = self.process.poll() if self.process is not None else None
        result["stderr_merged_with_stdout"] = True
        result["diagnostic_log"] = str(self.diagnostic_path)
        self._note_exit()
        return result

    def send(self, command):
        if not isinstance(command, str) or any(c in command for c in "\r\n\0"):
            raise InvalidCommand("无效的控制台命令")
        with self.lock:
            if not self.available():
                self._note_exit()
                raise ConsoleUnavailable("HeadlessMC 未运行")
            try:
                data = (command + "\n").encode("utf-8")
                sent = 0
                while sent < len(data):
                    sent += os.write(self.master, data[sent:])
            except OSError as exc:
                raise ConsoleUnavailable("HeadlessMC PTY 不可写") from exc

    def _close_diagnostics(self):
        if self._diagnostic_fd is not None:
            try:
                os.close(self._diagnostic_fd)
            except OSError:
                pass
            self._diagnostic_fd = None

    def stop(self):
        if self.process is None:
            self._close_diagnostics()
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
        self._note_exit()
        self._close_diagnostics()
