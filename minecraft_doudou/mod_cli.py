"""Operator-only local diagnostic for DouDou's Minecraft client mod.

Does not start a game or the HTTP bridge. It reuses the same bounded mailbox
as the bridge so Muse can test a manually launched client independently.
"""
import argparse
import json
import sys

from .mod_mailbox import ModMailbox, ModUnavailable, InvalidModAction


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local read-only state and bounded in-game action diagnostic")
    parser.add_argument("--root", required=True, help="Existing instance/config/doudou-io directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("state")
    memory = sub.add_parser("landmarks")
    memory.add_argument("--kind", choices=["bed", "container", "workstation"])
    memory.add_argument("--limit", type=int, default=100)
    eat = sub.add_parser("eat")
    eat.add_argument("--slot", type=int)
    for name in ("open", "sleep"):
        action = sub.add_parser(name)
        for axis in ("x", "y", "z"):
            action.add_argument(axis, type=int)
    for name in ("open-corpse", "take-corpse"):
        action = sub.add_parser(name)
        action.add_argument("entity_uuid", help="UUID from nearby_entities where category=corpse and owned_by_self=true")
    res = sub.add_parser("result")
    res.add_argument("id")
    args = parser.parse_args(argv)

    box = ModMailbox(args.root)
    try:
        if args.command == "state":
            output = box.state()
        elif args.command == "landmarks":
            output = box.landmarks(args.kind, args.limit)
        elif args.command == "eat":
            payload = {"action": "eat"}
            if args.slot is not None:
                payload["slot"] = args.slot
            output = box.submit(payload)
        elif args.command in ("open-corpse", "take-corpse"):
            output = box.submit({"action": "open_corpse" if args.command == "open-corpse"
                                 else "take_corpse_items",
                                 "entity_uuid": args.entity_uuid})
        elif args.command in ("open", "sleep"):
            payload = {
                "action": "open_container" if args.command == "open" else "sleep",
                "x": args.x, "y": args.y, "z": args.z
            }
            output = box.submit(payload)
        else:
            output = box.result(args.id)
    except (ModUnavailable, InvalidModAction) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
