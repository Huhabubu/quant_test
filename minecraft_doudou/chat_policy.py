"""Shared Python-only chat admission policy.

One owner speaks without a wake word; everyone else must explicitly mention
DouDou (ASCII case-insensitive or Chinese 豆豆). This is a relevance filter,
NOT permission to issue game-control commands.

Minecraft self-echo must never feed a reply loop back into MAIN.
"""
import re

PLAYER_NAME = re.compile(r"[A-Za-z0-9_]{3,16}\Z")
WAKE_WORDS = ("doudou", "豆豆")
SELF_SENDER = "DouDou"


def addressed(text):
    if not isinstance(text, str):
        return False
    # Players often say "dou dou" / "dou douu". Ignore spacing
    # between letters (ASCII and Unicode whitespace); do not turn
    # unrelated public chat into a model wakeup.
    folded = "".join(char for char in text.casefold() if not char.isspace())
    return any(word in folded for word in WAKE_WORDS)


def admitted_chat(sender, text, owner="Huhabubu"):
    if (not isinstance(owner, str) or not PLAYER_NAME.fullmatch(owner)
            or not isinstance(sender, str) or not PLAYER_NAME.fullmatch(sender)
            or not isinstance(text, str) or not text.strip()):
        return False
    if sender == owner:
        return True
    # Sender is a game entity we control, not another speaking player.
    if sender.casefold() == SELF_SENDER.casefold():
        return False
    return addressed(text)


def can_issue_tasks(sender, owner="Huhabubu"):
    return isinstance(sender, str) and sender == owner
