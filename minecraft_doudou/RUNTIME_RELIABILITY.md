# DouDou runtime reliability — one cohesive candidate, no live switch

**Scope:** persistent Python monitoring and stale-message quarantine. No new
model/API, no Redis/Kafka, no game-action permissions, no live Minecraft restart.
This is an extension of PR #7; do NOT make another runtime/version switch just
because CI passes. The Muse operator must test and authorize one coherent
rollout when the entire phase is ready.

## Why

On the first live PR #7 natural chat, Minecraft->SQLite took ~1 second, but
Muse MAIN got to the reply 139 seconds later. A one-shot `event_receiver arm`
had exited and was not re-armed; host completion notifications occupied
MAIN's conversation. The 120-second reply freshness gate correctly rejected
the stale reply. Without quarantine, the four old chats remained pending
and could be swept into a future fresh batch, possibly resurrecting a task.

## Ownership boundaries

- **Existing receiver + three relays + ONE sender** are ordinary Python
  services. On Linux, run them under the existing supported system manager
  (e.g. `systemd --user`) with **one named unit per process** and bounded
  restart settings. Do not let a second coordinator also spawn them.
- `runtime_coordinator` is one continuously supervised Python monitor.
  It reads SQLite, explicitly quarantines stale/replayed chat, checks
  configured systemd unit activity, and atomically writes a small JSON
  status file. It **never** reads player chat content into status, sends
  a Minecraft message, ACKs a new task, starts an AI agent, sends any
  game action, or auto-retries an uncertain M1 message.
- **Muse MAIN** remains the sole natural-language intent/reply owner.
  Actual model availability is `unobservable_without_host_api`. A pending
  chat reported in JSON is **not** proof that a model has been woken; an
  `arm` returning once is **not** continuous rearming. Until Muse supplies
  a supported session wake/recovery API, model replies after restart are
  best effort and **must not** be described as autonomous restoration.
- **Single game controller** remains a separate, unsolved STOP/physical
  action arbitration requirement. This change grants no new movement.
- The **independent sender log write-off and its 15-second queue blocking**
  remain a separate coherent engineering change, not silently modified here.

## Stale event semantics and safety

- Each monitor tick acquires the same SQLite write lock that
  `prepare-routed` uses. For unhandled chat older than 120s, source-time
  older than 120s, explicitly replayed after restart, or malformed, it
  atomically creates an `event_reviews(state=needs_review)` row and marks
  that *original event excluded from the active inbox*. The original event
  payload and event ID remain in `events`; they are **not** an executed
  task or silently forgotten. Non-chat events remain untouched.
- The MAIN's `child_once` only reads the active inbox. The reviewer can
  inspect quarantined items with:
  `python3 -m minecraft_doudou.event_lifecycle list --db "$CHAT_INBOX"`.
  This intentionally prints original chat **only to the private local CLI**,
  not to public JSON health status. It should not be logged to shared CI.
- Only an explicitly approved reviewer may close a review with
  `event_lifecycle close --db ... --event-id ... --note ...`. A stale
  instruction to mine/build/attack must **not** auto-resume; MAIN may ask
  the player to repeat or confirm in a fresh event. No automatic `claim`.
- The reply queue now independently refuses *any* `prepare` or
  `prepare-routed` that would ACK a stale/replayed old message in its
  `through_seq` range, even if the coordinator is offline. First sweep
  old messages to review, then process only freshly pending chats.
- This is an inbox lifecycle action, **not** end-to-end message delivery
  success. `handled_at` for quarantined events means removed from active
  work; `event_reviews.needs_review` remains an outstanding obligation.

## Installing services safely (after one whole-phase review)

**Do not deploy from this document automatically.** The Muse operator
first checks the exact active Python interpreter/worktree/DB/state paths,
systemd user-manager availability, login persistence (linger), instance
permissions, and whether any old unmanaged receiver/relays/sender already
run. Never put a second sender on the same SQLite database.

Use one template for each existing Python service, with fully verified paths:

```ini
[Unit]
Description=DouDou dedicated runtime component
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/ACTUAL/verified/qt-pr7-or-candidate-worktree
# Each unit's ExecStart is an exact previously verified argv,
# e.g. /ACTUAL/python3 -m minecraft_doudou.event_receiver serve
#      --db /ACTUAL/inbox.sqlite --port 18765 --token-file /PRIVATE/token
# Relay units each need their individual --log, --state, --outbox, --sender,
# --receiver; use a *private* EnvironmentFile for DOUDOU_EVENT_TOKEN.
# Sender command: python -m minecraft_doudou.chat_replies send
#                 --db /ACTUAL/inbox.sqlite --m1-say (NO --once)
ExecStart=/ACTUAL/python3 -m minecraft_doudou.runtime_coordinator --config /PRIVATE/runtime-config.json
Restart=on-failure
RestartSec=2
KillMode=control-group
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
```

That sample ExecStart shows **the coordinator unit only**. Create distinct
receiver, relay-1..3, sender units from their verified existing commands; do
not copy the coordinator ExecStart into every unit. Put user-specific secrets
in a private environment file; do not log the full inbox or token.

Example runtime configuration (replace paths and units with measured values):

```json
{
  "db": "/ACTUAL/inbox.sqlite",
  "status_path": "/ACTUAL/private/runtime-status.json",
  "lock_path": "/ACTUAL/private/runtime-coordinator.lock",
  "state_path": "/ACTUAL/config/doudou-io/state.json",
  "interval_seconds": 3,
  "systemd_units": {
    "receiver": "doudou-receiver.service",
    "relay1": "doudou-relay-player.service",
    "relay2": "doudou-relay-other.service",
    "relay3": "doudou-relay-companion.service",
    "sender": "doudou-sender.service"
  }
}
```

Private directories and configuration should be owner-only. The coordinator
does **read-only** `systemctl --user is-active UNIT` queries; systemd's
`Restart=on-failure` provides process restarts. An inaccessible user bus
is reported `unobservable`, never misreported `active`. A service being
active does not guarantee end-to-end message delivery.

Do a single nonresident smoke check before running a supervised monitor:

```bash
python3 -m minecraft_doudou.runtime_coordinator --config /PRIVATE/runtime-config.json --once
```

It writes a private JSON snapshot but **does** quarantine old chat: test
with a cloned SQLite backup first! Do not point `--once` at the live DB
until its review obligations and one-time backup have been examined.

## One coherent acceptance gate

Use a clone, not live Minecraft:
1. One stale task followed by a fresh greeting: routed prepare fails
   before any ACK; coordinator moves the old task into
   `needs_review` (full source preserved); fresh greeting then routes.
2. Restarted/replayed chat + old source timestamps quarantine safely and
   are not auto-executed. Non-chat event handling stays unchanged.
3. Coordinator can start without Muse or the Minecraft client and report
   `unobservable` correctly; second coordinator refuses its lock.
4. Supervisor's verified units recover from one deliberate *test-only*
   process failure without duplicate sender processes, using an
   isolated staging runtime; failing units become visible in status.
5. Separate future work: sender 15s M1 say block, real physical STOP,
   main session auto-revival, task spawn recovery, active progress broadcast.

**Rollout boundary:** one review of the full batch, one compatibility
check of live worktree/DB, one planned restart/migration of the *Python
infrastructure only* with single-writer handoff, then ordinary passive
metrics. No version flip for each small code edit.
