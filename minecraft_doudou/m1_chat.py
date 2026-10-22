"""Chat-only M1 transport for the one permitted DouDou reply writer.

M1 may interleave asynchronous [chat]/[event] frames with command receipts.
Reuse the existing sentinel-framed M1Reader, but do not assume that the next
frame after a command belongs to that command. A successful M1 receipt is NOT
proof that players saw the chat in Minecraft.
"""
import re
import socket
import time
import unicodedata

from .bridge import InvalidCommand, quote_hmc
from .m1_probe import HOST, PORT, M1Reader

_OK = re.compile(r"^OK(?:\b|[ :])", re.I)
_ERROR = re.compile(r"^(?:ERR(?:OR)?|FAIL(?:ED)?)(?:\b|[ :])", re.I)
_ASYNC_PREFIXES = ("[chat]", "[event]")
MAX_ASYNC_FRAMES = 32
FRAME_WAIT_SECONDS = 15.0


class M1ChatError(RuntimeError):
    pass


def format_m1_say(text):
    """Allow exactly one bounded say command; never accept command injection."""
    try:
        quote_hmc(text)  # Reuse existing 1-200 characters/control guard.
    except InvalidCommand as exc:
        raise M1ChatError("invalid_chat_text") from exc
    if any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp")
           for char in text):
        raise M1ChatError("unsafe_chat_character")
    if text.lstrip().startswith(("/", "#")):
        raise M1ChatError("chat_command_prefix_forbidden")
    if not text.strip():
        raise M1ChatError("empty_chat")
    return "say " + text


def _frame_type(frame):
    """Only unambiguously tagged async frames can be ignored.

    A chat line containing 'OK' (even on a separate line) must never be
    interpreted as a command acknowledgement. Unknown frames fail closed.
    """
    lines = [line.strip() for line in frame.splitlines() if line.strip()]
    if not lines:
        return "unknown"
    first = lines[0].casefold()
    if first.startswith(_ASYNC_PREFIXES):
        return "async"
    if any(_ERROR.match(line) for line in lines):
        return "error"
    # Some valid M1 command receipts include a preceding [auto-upgrade]
    # notice, which was already supported by the original adapter.
    statuses = [line for line in lines
                if not line.casefold().startswith("[auto-upgrade]")]
    if statuses and _OK.match(statuses[0]):
        return "ok"
    return "unknown"


def _await_frame(reader, sock, phase, accepted, *, clock=time.monotonic):
    """Bound total wall time AND number of unsolicited frames per phase.

    No pre-send drain: notifications can arrive after the drain, so always
    filter while waiting for each actual command's acknowledgement.
    """
    deadline = clock() + FRAME_WAIT_SECONDS
    skipped = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise M1ChatError(phase + "_receipt_deadline_exceeded")
        sock.settimeout(remaining)
        frame = reader.read_reply()
        kind = _frame_type(frame)
        if kind == "async":
            skipped += 1
            if skipped > MAX_ASYNC_FRAMES:
                raise M1ChatError(phase + "_too_many_async_frames")
            continue
        if accepted(frame, kind):
            return skipped
        raise M1ChatError(phase + "_not_confirmed")


def send_m1_chat(text, *, connect=socket.create_connection):
    """Send one chat via M1 localhost TCP, never retransmit after uncertainty.

    Each command's receipt may be preceded by queued chat/event notifications.
    Only explicitly tagged notifications can be skipped. An ambiguous or
    missing command receipt always fails closed; the caller's durable outbox
    marks the reply uncertain and requires independent game-log inspection.
    """
    command = format_m1_say(text)
    sock = connect((HOST, PORT), 6)
    try:
        reader = M1Reader(sock)
        ignored = _await_frame(
            reader, sock, "banner",
            lambda frame, kind: kind not in ("async", "error") and
            ("M1" in frame or "Machine One" in frame)
        )
        # M1 v0.17.5 accepts direct commands after the framed greeting.
        # Muse verified no-RAW "where" against live state.json on 2026-10-22.
        # RAW ON instead emits a prose acknowledgement with different framing
        # and caused a 15s timeout. Do not issue it in this chat transport.
        sock.sendall((command + "\n").encode("utf-8"))
        ignored += _await_frame(
            reader, sock, "m1_say", lambda frame, kind: kind == "ok"
        )
        result = {"accepted": True, "verified": False, "transport": "m1"}
        if ignored:
            result["skipped_async_frames"] = ignored
        return result
    finally:
        sock.close()
