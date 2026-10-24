# DouDou Python chat admission (candidate PR #8)

**Not deployed. Do not switch live relays per minor code commit.**
This change ships with runtime reliability as one future tested rollout.

## Current vs future behavior

Current Muse live install: three sender-specific relays tail the client log
for Huhabubu, SekaiSaionji and kurumi. The old code does not implement
keyword filtering. MAIN's child_once originally admits only Huhabubu,
so other players' collected messages may block the reply batch.

New Python policy (before the model):
- All chat by `Huhabubu` goes to MAIN (no wake word needed).
- Other Minecraft players only if their text includes English `doudou`
  case-insensitively (ignore embedded whitespace such as `dou dou`, `dou douu`), or Chinese `豆豆`. Unaddressed lines never enter
  the event inbox. This is a substring filter, not a word-boundary match.
- `DouDou` itself is excluded (prevents self-reply feedback loops).
- Event receiver independently applies the same rule as defense-in-depth.
- `child_once` returns `sender` and `is_owner` for every admitted
  message; MAIN may reply to an addressed visitor.
- `prepare-routed` permits foreign `kind=chat` but forbids non-owner
  `kind=task`. A mention is not permission to mine, navigate, fight,
  stop, change configuration or override MAIN instructions.
- Non-chat events (death/low_hunger/event_gap) are not gated by words.

## One controlled migration (NOT three relays plus a wildcard fourth)

To include **every** server player (not only the three preconfigured
names), switch to **one** durable log relay tailing the client's shared
`latest.log`, with:

    python3 -m minecraft_doudou.event_relay --log ACTUAL_LOG \
      --outbox ACTUAL_SINGLE_OUTBOX --receiver http://127.0.0.1:18765/events \
      --state ACTUAL_STATE --sender '*' --owner Huhabubu

`DOUDOU_EVENT_TOKEN` must be provided from the current private
configuration. Replace ACTUAL_* after checking real paths. For receiver,
use `event_receiver serve --owner Huhabubu` (the new default).

All previous sender-specific relays **must be disarmed before** the
single wildcard relay begins forwarding, because each has an independent
outbox and a different event ID. Leaving them running causes duplicate
messages. Use SQLite's backup API for a pre-switch snapshot, check
existing outbox backlogs and unhandled chats, and preserve rollback.
The wildcard reader's first enrollment starts from EOF, so never
silently discard old pending events; reconcile them explicitly.
Verify one sender and no duplicate MAIN reply writer remain.

No new LLM, websocket, game-client change or Minecraft server restart
is required. **Run a single controlled **main-server** integration cutover after the
whole PR #8 candidate passes CI; separate test-world/clone-only trials are
not a prerequisite. The owner permits short downtime to restart client or
Python services. Backup the live SQLite inbox before applying lifecycle
sweep, retain the old sender until the replacement is ready, and verify
no duplicate writer during the switch.**
