"""Conservative DouDou adapter for M1's local TCP protocol.

This is a local tool, not the upstream unauthenticated HTTP/MCP bridge.
Read operations are enabled; face requires explicit per-call operator approval.
Other M1 verbs remain inventoried but disabled until separate live acceptance.
"""
import argparse
import json
import math
import re
import socket
import sys

from .m1_probe import M1Reader, M1ProbeError, HOST, PORT

# Record the official M1 surface without exposing untested world mutations.
CAPABILITIES = {
    "where": ("perception", "read", "multiplayer_verified"),
    "look": ("perception", "read", "multiplayer_verified"),
    "inv": ("perception", "read", "multiplayer_verified"),
    "scan": ("perception", "read", "multiplayer_observed"),
    "describe": ("screen", "read", "pending_live_test"),
    "slots": ("screen", "read", "pending_live_test"),
    "agent_status": ("agent", "read", "multiplayer_observed"),
    "defend_status": ("agent", "read", "multiplayer_verified"),
    "face": ("camera", "manual", "multiplayer_verified"),
    "hold": ("inventory", "disabled", "pending_live_test"),
    "equip": ("inventory", "disabled", "pending_live_test"),
    "organize": ("inventory", "disabled", "pending_live_test"),
    "moveitem": ("inventory", "disabled", "pending_live_test"),
    "openinv": ("screen", "disabled", "pending_live_test"),
    "close": ("screen", "disabled", "pending_live_test"),
    "click": ("screen", "disabled", "pending_live_test"),
    "type": ("screen", "disabled", "pending_live_test"),
    "slot": ("screen", "disabled", "pending_live_test"),
    "takeall": ("storage", "disabled", "pending_live_test"),
    "stash": ("storage", "disabled", "pending_live_test"),
    "place": ("interaction", "disabled", "singleplayer_verified_only"),
    "agent_use": ("interaction", "disabled", "pending_live_test"),
    "agent_place": ("interaction", "disabled", "pending_live_test"),
    "mine": ("mining", "disabled", "pending_live_test"),
    "agent_mine": ("mining", "disabled", "pending_live_test"),
    "goto": ("movement", "disabled", "baritone_owns_navigation"),
    "moveto": ("movement", "disabled", "baritone_owns_navigation"),
    "move": ("movement", "disabled", "baritone_owns_navigation"),
    "agent_follow": ("movement", "disabled", "baritone_owns_navigation"),
    "agent_patrol": ("movement", "disabled", "baritone_owns_navigation"),
    "agent_stop": ("movement", "disabled", "pending_live_test"),
    "agent_jump": ("movement", "disabled", "pending_live_test"),
    "agent_sneak": ("movement", "disabled", "pending_live_test"),
    "agent_sprint": ("movement", "disabled", "pending_live_test"),
    "attack": ("combat", "disabled", "not_tested"),
    "defend": ("combat", "disabled", "automatic_behavior_off"),
    "agent_shield": ("combat", "disabled", "pending_live_test"),
    "craft": ("crafting", "disabled", "multiplayer_inventory_anomaly"),
    "cancraft": ("crafting", "disabled", "pending_live_test"),
    "agent_craft": ("crafting", "disabled", "multiplayer_inventory_anomaly"),
    "agent_sleep": ("sleep", "disabled", "multiplayer_silent_failure"),
    "sleep": ("sleep", "disabled", "multiplayer_silent_failure"),
    "vault": ("storage", "disabled", "requires_bank_vault_mod"),
    "pack": ("storage", "disabled", "requires_backpack_mod"),
    "recover": ("agent", "disabled", "not_tested"),
    "agent_drop": ("inventory", "disabled", "destructive"),
    "autoupgrade": ("inventory", "disabled", "keep_off"),
    "cmd": ("server", "disabled", "arbitrary_server_command"),
    "screenshot": ("perception", "disabled", "headless_zero_bytes"),
    "worlds": ("screen", "disabled", "world_selection"),
    "joinworld": ("screen", "disabled", "world_selection"),
    "pause": ("screen", "disabled", "world_exit_risk"),
}
TARGET = re.compile(r"^[a-zA-Z0-9_:\\-]{1,64}$")


class M1ActionDenied(ValueError):
    pass


def format_command(action, args=(), *, approve_face=False, baritone_stopped=False):
    """Never pass through arbitrary remote M1 commands."""
    if action not in CAPABILITIES:
        raise M1ActionDenied("unknown_m1_action")
    if action in ("where", "look", "inv", "describe", "slots", "agent_status", "defend_status"):
        if args:
            raise M1ActionDenied("unexpected_arguments")
        return {"agent_status": "agent status", "defend_status": "defend status"}.get(action, action)
    if action == "scan":
        if len(args) > 1:
            raise M1ActionDenied("scan_requires_zero_or_one_argument")
        if not args:
            return "scan"
        value = str(args[0])
        if value.isdecimal():
            if not 1 <= int(value) <= 32:
                raise M1ActionDenied("scan_range_must_be_1_to_32")
        elif not TARGET.fullmatch(value):
            raise M1ActionDenied("invalid_scan_target")
        return "scan " + value
    if action == "face":
        if not (approve_face and baritone_stopped):
            raise M1ActionDenied("face_requires_approval_and_baritone_stop")
        if len(args) not in (1, 2):
            raise M1ActionDenied("face_requires_yaw_optional_pitch")
        try:
            values = [float(v) for v in args]
        except (TypeError, ValueError) as exc:
            raise M1ActionDenied("invalid_angle") from exc
        if not all(math.isfinite(x) for x in values):
            raise M1ActionDenied("invalid_angle")
        if not -180 <= values[0] <= 180 or (len(values) == 2 and not -90 <= values[1] <= 90):
            raise M1ActionDenied("angle_out_of_range")
        return "face " + " ".join(f"{v:g}" for v in values)
    raise M1ActionDenied("m1_action_disabled_pending_live_verification")


def call_m1(action, args=(), *, approve_face=False, baritone_stopped=False,
            connect=socket.create_connection, port=PORT):
    command = format_command(action, args, approve_face=approve_face,
                             baritone_stopped=baritone_stopped)
    sock = connect((HOST, port), 6)
    try:
        sock.settimeout(15)
        reader = M1Reader(sock)
        banner = reader.read_reply()
        if "M1" not in banner and "Machine One" not in banner:
            raise M1ProbeError("unexpected_server_banner")
        # M1Reader.command is read-only; use its framing method for vetted writes.
        sock.sendall(b"RAW ON\n")
        raw_reply = reader.read_reply()
        if raw_reply.lstrip().startswith(("ERR", "ERROR")):
            raise M1ProbeError("raw_mode_rejected")
        sock.sendall((command + "\n").encode("utf-8"))
        reply = reader.read_reply()
        return {
            "command": command, "response": reply[:12000],
            "response_truncated": len(reply) > 12000,
            "transport_ok": True, "verified_in_game": False,
            "note": "Check state.json and/or server effects before calling an action successful",
        }
    finally:
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="DouDou M1 gated localhost adapter")
    parser.add_argument("action", nargs="?", default="list")
    parser.add_argument("args", nargs="*")
    parser.add_argument("--approve-face", action="store_true")
    parser.add_argument("--baritone-stopped", action="store_true")
    options = parser.parse_args(argv)
    if options.action == "list":
        print(json.dumps(CAPABILITIES, ensure_ascii=False, indent=2))
        return 0
    try:
        result = call_m1(options.action, options.args,
                         approve_face=options.approve_face,
                         baritone_stopped=options.baritone_stopped)
    except (OSError, M1ProbeError, M1ActionDenied) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
