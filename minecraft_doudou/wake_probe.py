"""One-shot Minecraft chat wake probe for Muse runtime integration tests.

The process exits with code 0 after the FIRST new matching player chat line.
It deliberately does not assume that the Muse platform wakes on process exit.
No Minecraft processes are started, stopped or modified.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import time

from .events import parse_chat, OWNER_PATTERN


def wait_for_first_chat(path, sender, timeout=120.0, interval=0.05):
    """Return the first matching *new* chat or None on timeout."""
    if not OWNER_PATTERN.fullmatch(sender):
        raise ValueError("invalid player name")
    if not 0 < timeout <= 600:
        raise ValueError("timeout must be in (0, 600]")
    path = Path(path)
    deadline = time.monotonic() + timeout
    stream = None
    signature = None
    position = 0
    partial = b""
    first_open = True
    try:
        while time.monotonic() < deadline:
            try:
                stat = path.stat()
                sig = (stat.st_dev, stat.st_ino)
                if stream is None or sig != signature or stat.st_size < position:
                    if stream is not None:
                        stream.close()
                    stream = path.open("rb")
                    signature = sig
                    if first_open:
                        # Prevent a previously written chat from triggering a
                        # new wake probe; arm before the player sends a message.
                        stream.seek(0, os.SEEK_END)
                        first_open = False
                    position = stream.tell()
                    partial = b""
                data = stream.read(64 * 1024)
                position = stream.tell()
                if data:
                    partial += data
                    if len(partial) > 128 * 1024:
                        partial = partial[-8192:]
                    while b"\n" in partial:
                        line, partial = partial.split(b"\n", 1)
                        event = parse_chat(line.decode("utf-8", errors="replace"), sender)
                        if event:
                            return event
            except OSError:
                if stream is not None:
                    stream.close()
                    stream = None
                    signature = None
            time.sleep(interval)
    finally:
        if stream is not None:
            stream.close()
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="One message => one process completion, test Muse wake capability")
    parser.add_argument("--log", required=True, help="Existing Minecraft latest.log file")
    parser.add_argument("--sender", default="Huhabubu")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    try:
        event = wait_for_first_chat(args.log, args.sender, args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if event is None:
        return 124
    print(json.dumps({"event": event, "wake_probe": True}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
