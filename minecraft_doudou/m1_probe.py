"""Read-only local TCP compatibility probe for the third-party M1 Minecraft mod.

This is NOT an M1 installer or agent controller. It does not start Minecraft,
install a mod, move the player, send chat, or change a game setting.

M1 documented wire format: one command line per request, reply terminated by
a line exactly equal to <<END. A greeting is terminated the same way.
This module deliberately allows only RAW ON, help, where, look, inv.
"""
import argparse
import json
import socket
import sys

HOST = "127.0.0.1"
PORT = 26000
SENTINEL = b"<<END"
ALLOWED = frozenset({"RAW ON", "help", "where", "look", "inv"})
MAX_REPLY_BYTES = 131072


class M1ProbeError(RuntimeError):
    pass


class M1Reader:
    def __init__(self, sock, max_bytes=MAX_REPLY_BYTES):
        self.sock = sock
        self.pending = b""
        self.max_bytes = max_bytes

    def read_reply(self):
        """Retain TCP bytes after the exact sentinel line; never split by recv."""
        lines = []
        size = 0
        while True:
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line == SENTINEL:
                    return b"\n".join(lines).decode("utf-8", "replace")
                lines.append(line)
                size += len(line) + 1
                if size + len(self.pending) > self.max_bytes:
                    raise M1ProbeError("reply_exceeds_limit")
            if size + len(self.pending) > self.max_bytes:
                raise M1ProbeError("reply_exceeds_limit")
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout as exc:
                raise M1ProbeError("reply_timeout") from exc
            if not chunk:
                raise M1ProbeError("connection_closed_without_sentinel")
            self.pending += chunk

    def command(self, command):
        if command not in ALLOWED:
            raise M1ProbeError("forbidden_command_in_read_only_probe")
        self.sock.sendall((command + "\n").encode("utf-8"))
        return self.read_reply()


def probe(connect=socket.create_connection, port=PORT):
    """Localhost only. No remote host override to avoid exposing game control."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid_port")
    sock = connect((HOST, port), 6)
    try:
        sock.settimeout(15)  # M1 documents a 10s main-thread reply cap.
        reader = M1Reader(sock)
        greeting = reader.read_reply()
        if "M1" not in greeting and "Machine One" not in greeting:
            raise M1ProbeError("unexpected_server_banner")
        results = {}
        for command in ("RAW ON", "help", "where", "look", "inv"):
            response = reader.command(command)
            results[command] = {
                "ok": not response.lstrip().startswith(("ERR", "ERROR")),
                "response": response[:12000],
                "truncated": len(response) > 12000,
            }
        return {"connected": True, "host": HOST, "port": port,
                "read_only": True, "results": results,
                "note": "TCP replies only; no game action or headless actuation verified"}
    finally:
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="M1 read-only 26.2 NeoForge probe; never sends actions")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    try:
        result = probe(port=args.port)
    except (OSError, M1ProbeError, ValueError) as exc:
        print(json.dumps({"connected": False, "read_only": True,
                          "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
