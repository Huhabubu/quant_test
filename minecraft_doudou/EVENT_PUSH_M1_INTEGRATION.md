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


## P0-D candidate: concurrent chat sub-agent, single chat writer

Context: Muse's 2026-10-22 P0-C test verified actual concurrency:
main Agent continued a 40s read-only job while a compact listener sub-agent
completed a **simulated** reply before main finished. The model actually
starting work took 6.6–10s after the event; 75ms is only arm-process exit.
P0-C did **not** demonstrate a model-generated reply being sent into Minecraft.

The goal here is deliberately narrow: sub-agent writes **natural-language
text generated by itself** into a durable reply queue; a separate, single
chat-only dispatcher immediately sends it through the **existing** secured
HeadlessMC bridge chat API. This permits chat without waiting for a main-Agent
task boundary. This candidate has **not** been deployed or proved live.

- Reuses Python SQLite, urllib and `bridge.Controller.chat` → `msg` quoting
  rather than implementing another Minecraft transport, model runner, or
  message broker. The bridge must **already exist** and control the correct
  single client. Do not start a second client to make this test pass.
- Sub-agent only gets `chat_replies prepare`, the isolated inbox and task
  state. The single dispatcher alone holds the bridge's stronger token and
  can only call `POST /v1/chat` at a validated loopback URL. This process-level
  boundary does **not** provide OS-level isolation when the agents share a
  filesystem user; model/tool permissions must also enforce the boundary.
- New `chat_replies` table in the **same inbox.sqlite**; unique event ID
  prevents duplicate replies for the same chat. A model reply and ACK-through
  become durable atomically. The selected chat must still be pending, fresh
  (<120s old) and no newer unprocessed chat may be swept under that ACK.
- The dispatcher atomically reserves one queued reply before any call to the
  bridge. A crash or network timeout produces `sending` / `uncertain`
  requiring manual inspection: it is **never blindly retried** because
  `/v1/chat` has no end-to-end game delivery confirmation or idempotency ID.
  A successful 202 is labeled `accepted_unverified`: it only proves console
  submission, not receipt by the Minecraft server.
- Normal ordinary state events and stale chats may be classified without
  replies via P0-B's existing read/ACK path. Do not invent auto-responses to
  old instructions. A stop/cancel request should also be written to the
  existing control handoff for the main task, not executed by chat-only code.

### Isolated first pass: no Minecraft write

Start an independent P0-B receiver and inbox; inject test events with
unique event IDs, then spawn a real model-using Muse chat sub-agent which
reads the batch and **writes its own reply text**. Do not use a hardcoded
Python template. Example CLI after it reads the inbox's event IDs/sequence:

```bash
python3 -m minecraft_doudou.event_receiver drain   --db '<ISOLATED>/inbox.sqlite' --limit 200
python3 -m minecraft_doudou.chat_replies prepare   --db '<ISOLATED>/inbox.sqlite' --event-id '<LATEST_CHAT_ID>'   --through-seq <LAST_READ_SEQ> --text '<SUB_AGENT_MODEL_REPLY>'
python3 -m minecraft_doudou.chat_replies send   --db '<ISOLATED>/inbox.sqlite'   --mock-log '<ISOLATED>/mock-replies.jsonl' --once
python3 -m minecraft_doudou.chat_replies stats   --db '<ISOLATED>/inbox.sqlite'
```

Expect state `mocked` (explicitly **not** a live-game reply). In continuous
testing, run one `send` process without `--once`, and have the child
issue a single `prepare` per selected batch. Do not ACK a batch until every
event within its sequence range was reviewed; never ACK new arrivals unseen.

### Live chat handoff: only after explicit test approval

First verify the **existing** HeadlessMC bridge is actually reachable and
its `/v1/chat` sends on the correct running client. Keep the bridge bearer
token in a separate 0600 file, readable only to the dispatcher. If the Muse
VM currently sends chat exclusively by manual console interaction and
doesn't run this bridge, **report this as an integration blocker** rather
than launching a second Minecraft client or secretly substituting M1/PTY
commands.

Once the safe existing bridge and test-server target are verified, use:

```bash
python3 -m minecraft_doudou.chat_replies send   --db '<ISOLATED>/inbox.sqlite'   --bridge-url 'http://127.0.0.1:<EXISTING_BRIDGE_PORT>/v1/chat'   --token-file '<PRIVATE>/bridge-token'
```

Run only **one chat writer**, with prior main-Agent chat auto-replies
disabled for the test. Check the actual Minecraft chat log separately:
bridge `202` with `verified:false` is insufficient.

### P0-D acceptance requirements (Muse)

- Main Agent busy on a harmless 40s task. Chat sub-Agent receives two
  fresh chats mid-task, **generates natural-language text** rather than
  using templates and queues one context-aware reply; single dispatcher
  sends it without waiting for main to finish.
- Capture t0 game log, t1 inbox write, t2 sub-Agent begins actual model
  work, t3 model reply enqueued, t4 actual Minecraft game chat visible,
  t5 main task complete. Only t4 < t5 establishes the desired feature.
- Two child agents racing to answer the same event produce at most one
  queued reply; stale >120s chat is context-only. A simulated network
  timeout never silently re-sends, and any `sending` / `uncertain` row
  stays visible for manual reconciliation.
- Verify chat-only sender cannot call movement, attack, launch, connect or
  game-action endpoints; verify no unauthorized account/extra client and
  correct server. Abort if controller route is not independently verified.

No production deployment, no mod version bump, no extra public port, and
no M1 write-action permission change in this PR.


## P0-D M1 say transport candidate (2026-10-22)

Muse established that the *currently running* HeadlessMC is attached to the
main Agent's private exec stdin, **not tmux**. Do not try to reattach via
bridge.py or start a second game JVM. M1 v0.17.5 already runs locally at
127.0.0.1:26000 and Muse previously verified M1's say verb in the multiplayer
test server. Prefer reusing this mature existing mod channel.

- New m1_chat.py uses the existing m1_probe.M1Reader framing and mandatory
  M1 greeting -> RAW ON -> single say <text> command -> <<END reply.
  No arbitrary command parameter, no movement/attack/craft/console access;
  host/port are hardcoded 127.0.0.1:26000 with no remote selection.
- Explicit dispatcher selection ONLY: chat_replies send --m1-say (mutually
  exclusive with --bridge-url / --token-file or --mock-log).
  Message length 1–200 characters. No ASCII/Unicode format or line controls,
  line separators, initial slash commands, or Baritone-prefixed # commands.
  The model-generated text still must be reviewed/selected by the chat agent.
- A positive M1 OK receipt results in accepted_unverified and note
  m1_tcp_accepted_unverified, NEVER verified game delivery. Replayed log
  history (replayed_after_restart=true) and source-delayed (>120s) chats
  are treated as context-only; queued replies also expire before sending.
  ERR/FAIL, ambiguous
  reply, timeout, closed TCP or unknown greeting become uncertain.
  A reply marked sending/uncertain is NEVER automatically retransmitted, since
  M1 does not provide an idempotency key or definitive server ACK.
- The underlying M1 TCP port is a pre-existing unauthenticated local channel.
  These Python allowlists are an application-level guard, NOT kernel or OS
  isolation against a same-user process choosing to access M1 directly.
  Keep M1 bound only to loopback, do not expose ports or add credentials to Git.
- The dedicated sender is one process; do not run a competing direct M1 say
  writer or the main Agent's independent chat auto-reply for the same event.
  EventInbox/chat_replies remains the source of truth for idempotency.

### Isolated tests before game write

\`\`\`bash
python3 -m unittest -v test_doudou_m1_chat test_doudou_chat_replies
python3 -m minecraft_doudou.chat_replies send \
  --db '<ISOLATED>/inbox.sqlite' --mock-log '<ISOLATED>/simulated.jsonl' --once
\`\`\`

The first command uses fake M1 sockets and mock bridge; no live game access.
For the P0-D **B-level test on the already authorized multiplayer TEST server**
only, after verifying actual M1 socket availability, the correct loaded client
and an approved single harmless chat, select the live transport:

\`\`\`bash
python3 -m minecraft_doudou.chat_replies send \
  --db '<TEST-INBOX>/inbox.sqlite' --m1-say
\`\`\`

This changes only the transport. Keep a **single** reply dispatcher; preserve
the old P0-B receiver and three Relay processes until isolated acceptance
completes. The live test MUST involve a true spawned model child, a busy main
Agent, a fresh chat event, original prepare stdout in the report, and the
game's *actual chat log* proving message visibility before the main job ends.
Record T0 log capture, T1 inbox accepted, T2 child model starts, T3 prepare
persisted, T4 Minecraft log from DouDou, T5 main task done. A positive M1
receipt alone cannot satisfy T4.

If M1 returns anything other than a recognizable OK (or if socket access
blocks because of exclusive session ownership), stop and report the complete
**redacted** reply/status; do not guess a protocol, retry uncertain sends,
or fall back to the private HeadlessMC stdin. No production switch, mod
upgrade, JVM restart, or public port exposure in this change.


### 2026-10-22 P0-D B retest: fix M1 async-frame displacement

Muse B-level evidence showed a chat/event notification frame delivered after RAW ON
in place of the expected command ACK. The sender misidentified that frame as
the receipt and stopped **before sending say**. The generated reply remained
uncertain and was not automatically retried (correct fail-closed behavior).
In the same test, the main Agent's busy window had already ended 7.4s
before the model finished generating; t4<t5 was therefore impossible even if
the transport had worked.

Updated \`m1_chat.py\` now waits for a **recognized command response** while
skipping only complete sentinel-framed notifications with first line
\`[chat]\` or \`[event]\` (case-insensitive). This applies during greeting,
RAW ON, and say acknowledgement. Skip budget: 32 frames per phase, max 15
seconds elapsed per phase; errors and unrecognized frames still fail closed.
A chat notification containing the word OK cannot masquerade as an ACK.
Successful M1 replies record the count of skipped async frames as a diagnostic
in the chat reply result and SQLite result_note. An uncertain send records a
bounded failure category, never the chat text or credentials.

**Muse isolated retest sequence**:

1. Do not reuse the previously uncertain poetry event. Keep it archived for
   audit and inspect its game-log evidence without resending it.
2. Ensure the background dispatcher is a **single persistent
   \`chat_replies send --m1-say\` process before new messages arrive**; stop
   and verify any old mock sender is actually gone (not only its shell).
   Start with a distinct *isolated* inbox, not the production event inbox.
3. With that sender already running, pre-seed the M1 channel with harmless
   chat notifications, then send **one new uniquely identified harmless
   question** in the authorized multiplayer TEST server. Keep the actual
   client running, do not migrate it to tmux or launch a second JVM.
4. Spawn **one** actual natural-language child model, not a canned reply
   script; collect raw \`prepare\` stdout and M1 sender's JSON status. Start
   the main Agent's read-only busy interval at/after T0, or size it so it
   cannot expire before T4. Time measurements: T0 source game log, T1 inbox,
   T2 child model starts, T3 queue prepare, T4 **game chat actually visible**,
   T5 main busy interval ends. Only independent T4<T5 demonstrates the goal.
5. Confirm \`skipped_async_frames>0\` on the intentionally noisy case and
   \`accepted_unverified\` is **still only TCP command acceptance**. If the
   game log has no DouDou chat, report failed B. If output is \`uncertain\`,
   do not send it again; attach the de-identified failure_category / SQLite
   result_note and first redacted frame category for further diagnosis.
6. Keep existing P0-B production receiver/relay untouched until this
   isolated test and game-log confirmation pass. This patch does not grant
   permission for official-server auto-chat or additional M1 actions.
