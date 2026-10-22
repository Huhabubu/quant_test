"""Chat-only M1 transport for the one permitted DouDou reply writer.

This is NOT an arbitrary M1 command gateway. Reuses the vetted M1Reader
framing on 127.0.0.1:26000. An M1 'OK' is a command receipt, *not* proof
that the Minecraft server or other players saw the chat.
"""
import re
import socket
import unicodedata

from .bridge import InvalidCommand, quote_hmc
from .m1_probe import HOST, PORT, M1Reader

_OK = re.compile(r"^OK(?:\b|[ :])", re.I)
_ERROR = re.compile(r"^(?:ERR(?:OR)?|FAIL(?:ED)?)(?:\b|[ :])", re.I)


class M1ChatError(RuntimeError):
    pass


def format_m1_say(text):
    """Format exactly one M1 say command, rejecting chat-as-command prefixes."""
    try:
        quote_hmc(text)  # Existing length and ASCII-control validation.
    except InvalidCommand as exc:
        raise M1ChatError("invalid_chat_text") from exc
    # Reject Unicode line separators, format controls and command prefixes;
    # M1 TCP takes raw lines, not a shell or a JSON command.
    if any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp")
           for char in text):
        raise M1ChatError("unsafe_chat_character")
    if text.lstrip().startswith(("/", "#")):
        raise M1ChatError("chat_command_prefix_forbidden")
    if not text.strip():
        raise M1ChatError("empty_chat")
    return "say " + text


def _confirmed_ok(response):
    # M1 may prepend unsolicited [auto-upgrade] notices. Never treat arbitrary
    # non-ERR prose, or those notices, as proof of command acceptance.
    lines = [line.strip() for line in response.splitlines()]
    if any(_ERROR.match(line) for line in lines):
        return False
    return any(_OK.match(line) for line in lines)


def send_m1_chat(text, *, connect=socket.create_connection):
    """Send one bounded chat command to the M1 loopback socket, never retry.

    A missing/ambiguous reply raises: upstream at-most-once reply outbox
    records an uncertain delivery rather than resubmitting automatically.
    """
    command = format_m1_say(text)
    sock = connect((HOST, PORT), 6)
    try:
        sock.settimeout(15)
        reader = M1Reader(sock)
        greeting = reader.read_reply()
        if "M1" not in greeting and "Machine One" not in greeting:
            raise M1ChatError("unexpected_server_banner")
        sock.sendall(b"RAW ON\n")
        raw_response = reader.read_reply()
        if not _confirmed_ok(raw_response):
            raise M1ChatError("raw_mode_not_confirmed")
        sock.sendall((command + "\n").encode("utf-8"))
        response = reader.read_reply()
        if not _confirmed_ok(response):
            raise M1ChatError("m1_say_not_confirmed")
        return {
            "accepted": True, "verified": False, "transport": "m1",
        }
    finally:
        sock.close()
