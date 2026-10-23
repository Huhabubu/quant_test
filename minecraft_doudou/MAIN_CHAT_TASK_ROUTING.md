# DouDou main-chat / multi-worker routing — candidate v1

**Status: code candidate only. Not deployed to live Muse / Minecraft.**
This replaces the architectural assumption in C_FAST_PATH_RUNBOOK.md
that a chat-only child model owns player messages. The owner explicitly chose
the **MAIN Muse agent as the one conversation/intent owner**; child agents
work only on tasks handed to them by that main agent.

## Ownership and the fast path

- Game client/mod → existing receiver + SQLite inbox → MAIN Muse agent.
- The main agent uses the **existing** `child_once` for a bounded, read-only
  batch of ordered player chat plus fresh compact game state. C is now an
  optimized **data/reply path**, not a separate brain.
- Main agent reads **all** the `chats` returned, not just the final `chat`
  item. It decides, for each one, `chat` or `task`. That distinction is
  semantic: even `停下`, `回来找我`, and `挖完铁再说` are chat source
  events, some requiring game control.
- Main submits **ONE** `chat_replies prepare-routed` for the full batch.
  This atomically writes a bounded reply, durable per-event task handoffs,
  and ACKs only the reviewed **chat** events. A missing/reordered routing
  decision is rejected, and **other events are not ACKed**.
- A single existing `chat_replies send --m1-say` daemon owns textual
  game-chat sends. The main agent **does not send directly** after prepare.
  Existing uncertain-sends are not replayed.
- MAIN remains available to listen and respond while delegating long work to
  child agents. Read-only research children can run in parallel; the one
  Minecraft player has only **one claimed game task** at a time.
- Event handling (death/hunger/gap) remains MAIN's responsibility. The
  filtered `event_receiver arm --types chat` / `--types nonchat` options are
  available for *explicitly* distinct notification loops, but there must be
  only **one MAIN conversation authority**; workers do not monitor chat.

## Example: one hello and one mining instruction arrive together

`child_once` returns `chats` containing, in order, e.g.:

    [{"event_id":"c1","seq":41,"text":"你好"},
     {"event_id":"c2","seq":42,"text":"去挖20个铁矿"}]

The main model classifies both, and runs **one** prepare command:

    python3 -m minecraft_doudou.chat_replies prepare-routed \
      --db "$CHAT_INBOX" --sender Huhabubu \
      --event-id c2 --through-seq 42 \
      --routes-json '[{"event_id":"c1","kind":"chat"},{"event_id":"c2","kind":"task","lane":"game","priority":"normal"}]' \
      --text '你好。采矿任务已记录，我会安排执行。'

IMPORTANT:
- Supply the actual checked `CHAT_INBOX`, `event_id`, `seq` and
  response, never literal sample IDs.
- Classification covers **every unhandled chat** through the chosen
  sequence, in original order. Unknown sender, missing events, invalid
  decision, batches >20, stale requests: fail closed / escalate; do not
  blindly ACK or fabricate a reply. The original incoming chat text is
  persisted in `task_handoffs.instruction` unmodified.
- **Only the main agent** makes the classification and task decision.
  A worker cannot execute based solely on the chat sender, a speculative
  keyword, or a direct untrusted request.
- Keep any existing `chat_replies prepare` call sites separate during
  migration: legacy prepare has no task handoff and must **not** be the
  default user-message entry point.
- `prepare-routed` accepts lane `game` or `research`, priority
  `normal` or `urgent`. Priority does not automatically authorize
  attacks, OP/creative commands, server administration or other writes.

## Worker delivery, completion, and recovery

The MAIN agent may query ready tasks and hand them to workers:

    python3 -m minecraft_doudou.task_handoffs list --db "$CHAT_INBOX"
    python3 -m minecraft_doudou.task_handoffs claim --db "$CHAT_INBOX" --worker game-1
    python3 -m minecraft_doudou.task_handoffs finish --db "$CHAT_INBOX" \
      --event-id c2 --worker game-1 --status completed --note '已核实收集数量'

A claim returns a JSON task record or `{"task":null}`. MAIN must check
it, *then* spawn/notify the selected child via Muse's actual supported
runtime API; **this Python module does NOT spawn agent processes**. Worker
progress/result goes back to MAIN, which decides how to tell the player.
Do not let child agents send user-facing Minecraft chat; no duplicate writer.

- A claimed `game` task blocks a second `game` claim until completed,
  failed, or explicitly cancelled by its owning worker. Research workers
  can claim concurrently. A game task is one *logical* owner; it still
  must arbitrate physical M1/Baritone/Mod movement before execution.
- An urgent `stop` or `cancel` command must be handled by MAIN through
  the currently active game-control channel; the database's urgent
  priority **does not automatically interrupt** a running task. Do not
  claim that this module alone guarantees subsecond stopping.
- On crash/reboot, `claimed` tasks remain claimed; **do not automatically
  respawn/re-execute them** (Minecraft side effects may already have
  occurred). MAIN inspects `task_handoffs list`, reconciles world progress
  and deliberately decides what to do. Completed records are immutable.
- The host Muse runtime currently provides **no supported automatic
  rehydration of a warm agent session on reboot**. Python receiver/relays/
  sender can be supervised, but “MAIN always online” is conditional on
  platform session lifecycle. This candidate does not solve that platform
  limitation or install any runtime hook.
- No direct user/owner involvement is required for code CI. Muse handles
  isolated deployment and actual game timing after the candidate passes.

## Acceptance before live cutover

1. Python regression suite passes, including atomic routed ACK, mixed
   chat/task batch, unknown sender, unrelated non-chat events, concurrency,
   claim durability and idempotence.
2. A single-main ownership experiment: long game task is running in one
   worker **before** a new chat arrives; MAIN responds while task continues.
3. Main receives a new task while another runs: exact event ID arrives in
   task_handoffs, MAIN sees the new task and dispatches when game control is
   free. No task is “done” merely because chat replied “收到”.
4. Crash test before/after routed prepare, and during child execution:
   no dropped task, duplicate reply, or automatic action replay.
5. Main handles “停下来” correctly by interrupting the current worker
   under existing safety policy; no silent database-only acknowledgement.
6. Post-deploy T0→T4 (game message → game-visible reply) P50/P90 with stage
   breakdown; quantify host scheduling and sender's 15s ambiguous M1 receipt.
   No claim of a hard sub-8-second guarantee.

The two known independent follow-up gaps remain: (a) Muse's warm main
session cannot auto-revive across runtime restarts without supported host
lifecycle hooks, and (b) M1 v0.17.5 say may block a sender up to 15s while
awaiting a missing OK; game-log reconciliation is a separate change.
