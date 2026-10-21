# DouDou: HTTP event push and staged M1 integration (2026-10-21)

## Boundary and operating decision

This is a **review-only branch** based on `feature/doudou-minecraft-bridge` at `f0d1d6b`.
No running client or server has been modified. Do not restart online DouDou.

Two separate conditions are required for unattended replies:

1. **Transport**: a game event is detected and posted to a receiver. The new
   `event_relay.py` can do this over loopback HTTP with an idempotency key,
   retry and SQLite outbox.
2. **Muse activation**: the receiver must be a **verified Muse-supported hook**
   capable of waking or scheduling the main agent without opening the App.
   HTTP 2xx, process completion and a custom connector tool invocation are
   *not evidence* of main agent activation. Ask Muse to report that hook's
   actual local address/contract and how it triggers the agent; do not invent
   it or create a fake HTTP receiver and call it end-to-end success.

The repo already offers `GET /v1/events?after=N&wait_ms=20000` with
owner-only chat and a 0.1s log tailer. The new relay **does not replace** it:
it is an optional consumer/sender for environments with a real inbound
event hook. We chose HTTP POST first because a confirmed receiver and
acknowledgement are more valuable than adding an unauthenticated WebSocket.

## Event delivery contract (receiver running on same Muse VM)

Sender is optional, uses only Python standard library, no new external service:

```bash
# Put this in a private local shell only. Never paste the token into GitHub.
export DOUDOU_EVENT_TOKEN='<random 32+ char secret known by local receiver>'
python3 -m minecraft_doudou.event_relay \
  --log '<existing-instance>/logs/latest.log' \
  --state '<existing-instance>/config/doudou-io/state.json' \
  --receiver 'http://127.0.0.1:<VERIFIED_RECEIVER_PORT>/events' \
  --outbox '<PRIVATE_DIR>/doudou-event-outbox.sqlite' \
  --sender Huhabubu
```

The receiver MUST be confirmed before running this. The URL only permits
localhost HTTP on an explicit port (no public bridge or forwarded endpoint).
No Node M1 MCP/HTTP bridge is started.

The sender POSTs:
```json
{"id":"random-unique-id","event":{"type":"chat","sender":"Huhabubu","text":"跟我来","sequence":4}}
```

Headers: `Authorization: Bearer <secret>`,
`X-DouDou-Event-ID: <same id>`; 2xx means receiver accepted the event.
Receiver should de-duplicate by id because a timed-out POST might have
already arrived. Failed messages remain in the SQLite outbox for retry
(including process restart). The receiver must keep security/approval checks.

Current event types:
- `chat`: designated player's new chat line from `latest.log` (no other
  players' raw messages forwarded).
- `health_drop`, `death`, `low_hunger`: transitions from fresh Mod state.
- `inventory_changed`: bounded per-item count differences.
- `player_nearby`: newly detected player count, without publishing identities.
- `event_gap`: source ring buffer overflow notification. **Do not pretend
  dropped messages were delivered**.

Limitations: the watcher initially attaches at EOF; messages written while
the relay process itself is down can still be missed. For fully reliable
restart replay we would need a durable file-offset/inode cursor and a stable
upstream producer. A Muse runtime or VM shutdown also terminates local
receivers. The relay does not prove autonomous 24-hour operation.

## M1 command access

Existing approved Mod: `m1-0.17.5+26.2-neoforge.jar`, TCP
`127.0.0.1:26000`. `m1_probe.py` remains unmodified/read-only. New
`m1_adapter.py` safely checks protocol greeting, `RAW ON`, `<<END`
framing, argument grammar, and a **deny-by-default** capability registry.
It is a local CLI usable by Muse from the **same VM**:

```bash
python3 -m minecraft_doudou.m1_adapter list
python3 -m minecraft_doudou.m1_adapter where
python3 -m minecraft_doudou.m1_adapter look
python3 -m minecraft_doudou.m1_adapter inv
python3 -m minecraft_doudou.m1_adapter scan bed
python3 -m minecraft_doudou.m1_adapter agent_status
```

The already multiplayer-verified `face` requires **both** operator approval
and independent assurance that Baritone was stopped (operator must actually
run `#stop` beforehand):

```bash
python3 -m minecraft_doudou.m1_adapter face --approve-face --baritone-stopped -150 0
```

All other action verbs are visible in `list` as **disabled** pending a
specific safe real-world test. There is no arbitrary M1 command passthrough.
A TCP response only demonstrates protocol handling; game state/server
effects need separate verification. This adapter neither replaces Baritone
navigation nor exposes M1 unauthenticated TCP beyond localhost.

### One-function-at-a-time acceptance order

| Tier | Functions / scenarios | Passing evidence | Gate |
|---|---|---|---|
| A | where, inv, look, scan, describe, slots, agent status | M1 vs DouDou state, exact matching game observation | Read-only first |
| B | face | Same yaw/pitch seen in independent where, no movement; Baritone stopped | Existing MP evidence |
| C | hold, close/openinv, click, equip, organize, moveitem | Observable slot/GUI before/after, no lost items | Disposable inventory |
| D | place/use, takeall, agent use/place, mine | Actual world and inventory delta, server acceptance | Isolated world then separately authorized MP |
| E | movement/follow/patrol | No collision with Baritone; bounded path completion | Keep Baritone sole navigator by default |
| F | attack/defend, auto equip, drop, vault/pack | No player harm, no unexpected item movement | Isolated and separate consent |
| Frozen | craft/agent craft, agent sleep, screenshot | Fix prior anomaly first; avoid any repeat on live server | **Disabled** |

`craft planks` in MP once reported 64 logs missing and zero produced; logs
later returned, but the anomalous execution remains unproven. `agent sleep`
in MP twice queued then silently completed without player motion. No
retesting on valued multiplayer inventory. `screenshot` yielded zero bytes
under HeadlessMC. Existing DouDou 0.2.2 corpse/landmark/eat features stay
unchanged. v0.2.4 sleep diagnostics remain a separate investigation.

## Minimal verification

Run locally or in a GitHub PR CI (pure mocks, no Minecraft required):

```bash
python3 -m unittest -v test_doudou_m1_adapter test_doudou_event_relay
```

For Muse runtime, perform a single authorized chat event with a unique marker:
T0 log append, T1 POST receiver 2xx, T2 main Muse agent triggered *without App
interaction*, T3 game chat reply. Report all four and no repeated replies.
Then temporarily disable the receiver, confirm SQLite pending, re-enable and
verify one accepted event with the same id. Only after T2/T3 are verified can
HTTP event push replace the current single-message wake mechanism.

**Never log credentials, host addresses, full inventories or player positions
in public issues.** No production rollout until these gates pass.
