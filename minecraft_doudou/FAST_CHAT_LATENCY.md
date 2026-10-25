# DouDou chat latency phase — one staged main-server cutover

**Candidate only.** This branch builds on deployed PR #8 and does not change
Minecraft/Baritone/M1 mining, settings or worlds. The owner requested faster
replies **while workers perform long jobs**. Keep **one Muse MAIN** as the
only intent and reply owner, and only **one Minecraft chat sender**.

## Measured problem, and which latency each fix addresses

Actual main-server T0->T4 was ~42/81/116 seconds under MAIN's simultaneous
mining work, with three other messages expiring at 120 seconds. This is
**host/MAIN scheduling delay**, not evidence of a slow HTTP relay. Python
cannot wake/resume a Muse model session if the host has no supported hook.
Do not claim P50<=8s / P90<=12s until end-to-end live trials demonstrate it.

Separately, M1 0.17.5 `say` lacks a command-level OK receipt. The old
`chat_replies send --m1-say` waits around 15 seconds **per outgoing reply**,
even after the game displays it. This branch solves that independent queue
blocking problem, without equating TCP delivery with game visibility.

## New sender — wire once, never await say OK

- `m1_chat.submit_m1_chat_fast`: bounded TCP connect (2s), greeting
  (3s), and **one** `say` TCP write (2s). Returns immediately after
  `sendall`, NOT after a possibly nonexistent say receipt.
- `ReplyQueue.send_once(on_claim=...)` commits an at-most-once
  sending claim **and** pre-send game-log inode/device/EOF offset
  before the wire. The reply state is `sent_unverified`, not “delivered”.
  Errors are `uncertain`; crashes leave `sending` as a possible send.
  Neither state is automatically sent again.
- `fast_sender.LogWriteOff.scan`: an independent *verification phase*
  reading the actual client game log after the send queue is empty.
  Require same log generation, line END offset strictly after the
  pre-send EOF, exact sender `DouDou`, exact text and local log time
  within the attempt/grace window. One log line is a UNIQUE database
  evidence key, so identical replies cannot consume the same line.
  Game-visible evidence -> `game_log_confirmed`. Missing or rotated
  logs do **not** imply delivery or trigger retries.
- The scan uses bounded chunks per attempt and persists its cursor. Slow
  log reading cannot keep a queued reply waiting behind a 15s say receipt.
  Due to Minecraft log timezone, clock or format differences, a real
  visible message may remain `sent_unverified`; on-site reconciliation
  should check that separately rather than fabricating confirmation.

Start **instead of** the old sender, after a single-writer handoff:

```bash
python3 -m minecraft_doudou.fast_sender \
  --db /ACTUAL/PRIVATE/inbox.sqlite \
  --log /ACTUAL/MINECRAFT/logs/latest.log
```

The `--once` option is only for bounded diagnostics (it may submit one
queued reply), not for a long-running production sender. The new service
uses the actual Minecraft client `latest.log`, *not* server-side logs
or relay outbox, and never exposes the unprotected M1 TCP endpoint publicly.

Do not restart/swap only the sender while another writer is in flight.
Check `chat_replies` queued/sending/uncertain states, back up the SQLite
database through backup API, stop the old sender, establish exactly one
new sender, and keep all existing `uncertain` rows without replay.

## MAIN latency and concurrency discipline

Changes in Python do not give the Muse host an automatic model wake API.
MAIN must **not** issue long-running Baritone commands/long experiments
and wait synchronously for them to finish while holding the only chat
handling session. Instead:

1. MAIN receives a chat arm, runs `child_once` once. That one result now
   includes ordered chats, compact Minecraft state, `active_tasks` and
   `chat_age_s`. A `claimed` task is *not proof* of actual worker activity.
2. MAIN promptly classifies every authorized pending message and calls
   `prepare-routed` **once** (durable ACK + response + handoffs).
   Keep replies concise. No duplicate direct say.
3. Any long game/research operation is delegated to a Muse worker by its
   supported spawn API. MAIN does not wait for the worker's full lifecycle
   nor flood itself with task completion notifications. Worker writes
   persistent progress; MAIN resumes chat arm before doing lower-priority
   research.
4. If host runs MAIN serially and defers chat notifications until the
   active turn finishes, this is a *platform bottleneck*. Report T1->T2
   and turn occupancy, not an imagined Python fix. Prioritize a supported
   host wake/preemption/session API if one exists; do not invent one.
5. Never create another model as an independent reply/intent authority.
   If parallel model sessions turn out to be necessary, maintain a
   single MAIN decision writer, explicit version/epoch checks, and user
   permission before changing the architecture.

## One main-server acceptance, no test flood

- Baseline process/DB backup, record old sender state, single cutover.
- Real player chat while MAIN idle; chat while a worker does **a long
  non-destructive task**; multiple chats in a short burst.
- Capture T0 source chat log, T1 inbox, T2 actual MAIN start, T3
  prepare, T3.5 sender write, T4 game-visible output plus sender log
  confirmation. Count expired/no-reply as failures; report P50/P90 only
  if there is enough representative data.
- Verify the outgoing sender sends exactly once and doesn't wait 15s
  for M1 say OK; a duplicate text line can confirm only one DB row.
- Simulate a short sender restart **on the main server** when idle,
  verify no replay of previous `sending`/`uncertain`/confirmed rows,
  and original service can be restored if format mismatches.
- Physical STOP, Minecraft autonomous mining and full Muse hot-model
  restart remain separate work; do not accidentally expand this test.

A healthy Python relay or even `game_log_confirmed` cannot by itself
make Muse answer in 8 seconds. Separate *model enqueue/start*, *reasoning*,
and *sender* delays in the report.
