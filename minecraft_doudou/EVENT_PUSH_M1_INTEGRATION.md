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


## P0-B candidate: durable inbox and immediate wake (2026-10-22)

This **draft branch only** supersedes the one-shot receiver and initial-EOF
limitation described above. The currently live Muse v2 receiver is untouched.
No Java mod change or version increment. All components reuse the existing
project parser, Python standard-library HTTPServer and SQLite.

1. Keep the inbox receiver running independently of the one-shot wake process.
   It persists localhost HTTP POST events before returning 200. Invalid JSON,
   missing IDs, 401, 409, or oversized (>64 KiB) messages do not terminate it.
   Create a private 0600 token file, at least 32 random characters, out of Git.
   Set DOUDOU_EVENT_TOKEN to the same secret for the three existing relays.

   ```bash
   python3 -m minecraft_doudou.event_receiver serve --db '<PRIVATE_DIR>/inbox.sqlite' --port 18765 --token-file '<PRIVATE_DIR>/token'
   ```

2. Keep the existing three distinct --sender relays with their separate outbox
   paths and shared receiver URL at http://127.0.0.1:18765/events. The new
   DurableChatTail persists complete log lines and their file offset in the
   **same SQLite transaction**, recovering from relay restarts. Initial
   enrollment begins at EOF to avoid falsely re-delivering old history.
   Changed/truncated files emit event_gap; unrecoverable rotated history is
   reported as such.

3. Muse should arm this **one-shot notifier** as a background task and use the
   already verified process-exit handoff. It exits on pending chat or urgent death / low_hunger / event_gap
   immediately, with no 3-second quiet window. Mere player_nearby or inventory
   changes accumulate silently. Muse may still take time to start its next
   turn, and there is no verified supported webhook to interrupt a busy agent.

   ```bash
   python3 -m minecraft_doudou.event_receiver arm --db '<PRIVATE_DIR>/inbox.sqlite'
   ```

4. On wake, read all unhandled events in bounded pages (default 200, maximum
   1000 per call), integrate the conversation, and explicitly acknowledge the
   highest **fully processed** sequence. Re-arm afterwards. The message
   received_at inside event is the relay capture time; the outer received_at
   is the inbox acceptance time. A recovered older command should be treated
   as context until confirmed, and the same ID must not prompt repeat replies.

   ```bash
   python3 -m minecraft_doudou.event_receiver drain --db '<PRIVATE_DIR>/inbox.sqlite' --after-seq 0 --limit 200
   # Repeat with next_seq until all pending records have been classified.
   python3 -m minecraft_doudou.event_receiver ack --db '<PRIVATE_DIR>/inbox.sqlite' --through-seq <FULLY_PROCESSED_SEQ>
   python3 -m minecraft_doudou.event_receiver stats --db '<PRIVATE_DIR>/inbox.sqlite'
   ```

Only ACK processed records; a crash before ACK deliberately causes replay.
Processed records stay in SQLite as a deduplication archive and cease to count
as pending. A slow/absent receiver leaves events in the relay SQLite outbox.
The receiver rejects new events with 503 when it has 10,000 pending; the relay
does not silently discard its own full outbox either. Invalid oversized source
events can block the relay's sending head and require explicit operator
handling, never silent deletion.

**Known limits**: VM restarts kill all processes until they are rearmed;
Minecraft events absent from local log while the game client itself is offline
cannot be reconstructed. Rotation while the relay is offline can make an old
log segment inaccessible; event_gap discloses the gap. A HTTP 200 is only
durable receipt, not proof of Muse wake/reply.

Isolated verification: run
`python3 -m unittest -v test_doudou_event_receiver test_doudou_durable_log test_doudou_event_relay test_doudou_events`.
Then Muse should test one message through T0/T1/T2/T3, 100 rapid interleaved
chats, a pure-state batch, repeat IDs, bad token/JSON/2MB input, receiver and
relay restarts, log rotation, and delayed handoff. Avoid switching the live
receiver or restarting Minecraft without authorization.
