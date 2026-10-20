"""Tail the Minecraft client's real local chat log without starting another agent.

A daemon reader publishes bounded events; authenticated callers can wait on a
Condition instead of repeatedly grepping the whole log. This transport does
NOT wake Muse or prove model latency.
"""

from collections import deque
import os
from pathlib import Path
import re
import threading
import time


CHAT_LINE = re.compile(r"\[CHAT\]\s*<(?P<sender>[A-Za-z0-9_]{3,16})>\s*(?P<text>.*)")
OWNER_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def parse_chat(line, allowed_sender):
    match = CHAT_LINE.search(line)
    if not match or match.group("sender") != allowed_sender:
        return None
    text = match.group("text").strip()
    if not text:
        return None
    return {"type": "chat", "sender": match.group("sender"), "text": text[:500]}


class MinecraftChatEvents:
    def __init__(self, log_path, allowed_sender="Huhabubu", interval=0.1, maxlen=256):
        if not OWNER_PATTERN.fullmatch(allowed_sender):
            raise ValueError("无效的聊天发送者名称")
        self.path = Path(log_path)
        self.allowed_sender = allowed_sender
        self.interval = interval
        self.events = deque(maxlen=maxlen)
        self.condition = threading.Condition()
        self.last_sequence = 0
        self.stop_event = threading.Event()
        self.reader = None
        self.file_available = False

    def start(self):
        if self.reader is not None:
            raise RuntimeError("事件监听已经启动")
        self.reader = threading.Thread(target=self._run, name="mc-chat-tail", daemon=True)
        self.reader.start()

    def stop(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.reader is not None:
            self.reader.join(timeout=2)

    def publish_line(self, line):
        """Visible method for tests; publishes *only* the designated player's chat."""
        event = parse_chat(line, self.allowed_sender)
        if event is None:
            return
        with self.condition:
            self.last_sequence += 1
            event = dict(event, sequence=self.last_sequence,
                         received_at=time.time())
            self.events.append(event)
            self.condition.notify_all()

    def get(self, after=0, wait_ms=0):
        """Sequence-based long polling, no duplicates unless callers reuse cursors."""
        if not isinstance(after, int) or after < 0:
            raise ValueError("after 必须是非负整数")
        if not isinstance(wait_ms, int) or not 0 <= wait_ms <= 20000:
            raise ValueError("wait_ms 必须在 0–20000 范围")
        deadline = time.monotonic() + wait_ms / 1000
        with self.condition:
            while (after >= self.last_sequence and not self.stop_event.is_set()
                   and time.monotonic() < deadline):
                self.condition.wait(timeout=max(0, deadline - time.monotonic()))
            oldest = self.events[0]["sequence"] if self.events else self.last_sequence + 1
            found = [dict(ev) for ev in self.events if ev["sequence"] > after]
            return {
                "events": found,
                "next_cursor": self.last_sequence,
                "dropped": after < oldest - 1,
                "log_available": self.file_available,
                "note": "实时日志事件只代表本地收到；不能保证 Muse 被唤醒或已回复",
            }

    def _run(self):
        stream = None
        signature = None
        last_position = 0
        first_attach = True
        try:
            while not self.stop_event.is_set():
                try:
                    stat = self.path.stat()
                    sig = (stat.st_dev, stat.st_ino)
                    # Recover from truncation, replacement and log rotation.
                    if sig != signature or stat.st_size < last_position:
                        if stream is not None:
                            stream.close()
                        stream = self.path.open("r", encoding="utf-8", errors="replace")
                        signature = sig
                        if first_attach:
                            # Do not treat earlier chat history as a new request.
                            stream.seek(0, os.SEEK_END)
                        first_attach = False
                        last_position = stream.tell()
                    with self.condition:
                        self.file_available = True
                    while True:
                        position = stream.tell()
                        line = stream.readline()
                        if not line:
                            break
                        if not line.endswith("\n"):
                            # The writer has not finished flushing this line.
                            stream.seek(position)
                            break
                        last_position = stream.tell()
                        self.publish_line(line)
                except (OSError, UnicodeError):
                    with self.condition:
                        self.file_available = False
                    if stream is not None:
                        stream.close()
                        stream = None
                        signature = None
                    # Try again; no need to spawn a shell or external grep.
                self.stop_event.wait(self.interval)
        finally:
            if stream is not None:
                stream.close()
