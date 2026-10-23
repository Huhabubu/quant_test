# Live cutover: MAIN Muse owns chat and delegates tasks (PR #7)

**Target**: authorized Minecraft test server only. This is an operational rollout
procedure, NOT a claim that GitHub merge automatically configures the Muse host.
Do not restart Minecraft, replace mods, restart receiver/relays, or expose ports
to change the chat agent routing. Only the Muse host operator has the actual
runtime/session controls. Always use the current PR #7 HEAD and check its CI.

## Gate 0 — preflight, no live changes

1. Inspect the running Muse runtime for: main session, old C chat child,
   chat wake loop(s), sender command/process count, existing live receiver/relays,
   full path of live SQLite inbox, state.json, active PYTHONPATH and repository.
   Record process IDs/commands and task ownership. Do not infer these paths.
2. Check git SHA from the existing PR #7 worktree and record actual commit.
   Existing evidence: Muse tested candidate d5003ec6 in `qt-pr7`, local tests
   and GitHub CI passed. If PR head changes, recheck CI and smoke tests.
3. Take a **transactionally consistent SQLite backup**, with SQLite backup API
   or `.backup`, not raw copying a live WAL database. Save backup with
   appropriate file permissions. Record existing `events`,
   `chat_replies` queued/sending/uncertain counts and unhandled chats.
   **Never replay** prior uncertain/sending messages on restart.
4. Identify whether old C child is performing a send or has unresolved
   pending messages. Plan a handoff boundary only after its current model
   response/prepare settles. Verify that the production code path will use
   PR #7 Python modules, NOT a stale installed module from an old worktree.
5. Identify **supported** Muse main-session listener/rearm and child-spawn
   mechanism. If the main session cannot regain chat wake handling after
   the old child is closed, abort; code and a Python daemon do not create a
   persistent Muse model session.
6. The main Agent must explicitly accept these invariants: it is the
   sole chat/intent owner; only one sender writes player-facing chat; children
   never chat directly, never interpret untrusted player commands themselves,
   and use the task claim/finish discipline.

## Gate 1 — main Agent dry run, still no live chat writes

- Run `python3 -m minecraft_doudou.child_once --db "$CHAT_INBOX"
  --state "$DOUDOU_STATE" --sender Huhabubu` from the PR #7 source tree.
  This is read-only. Check pending event IDs and safe game state.
- On a cloned inbox, classify *each* ordered `chats` item as `chat` or
  `task`, and use `python3 -m minecraft_doudou.chat_replies prepare-routed
  --db "$CLONE_INBOX" --sender Huhabubu --event-id <latest-id>
  --through-seq <latest-seq> --routes-json '<complete-json-array>'
  --text '<model-generated-text>'`. Confirm task_handoffs uniqueness and
  correct ACK (only authorized chat events); mock sender only.
- Check `python3 -m minecraft_doudou.task_handoffs list
  --db "$CLONE_INBOX"` and atomic `claim`/explicit `finish`.
  Muse tested these already, so rerun only if source or host changes.
- Verify the main Agent can `spawn` a **read-only** worker, continue
  listening, and process new chat while worker remains active.
- Stop/interrupt safety: a queued `urgent` game task CANNOT automatically
  preempt a claimed game worker. MAIN must explicitly interrupt worker AND
  stop the real M1/Baritone movement controller, then reconcile the
  task claim and observed game state. Closing the worker agent alone does
  NOT prove Minecraft movement has stopped. If this handoff cannot be
  verified safely, disable automated stop/priority claims pending repair;
  do not represent them as completed.

## Gate 2 — reversible switch on the authorized test-server session

1. Put both old C child and MAIN in a short mutually understood handoff
   state: **no new `prepare` until one chat owner is selected**. Allow
   already-in-progress sender to finish or mark uncertain; no second sender.
2. Disarm/close the **old C chat child and its chat wake loop**. Retain
   any separate non-chat event watcher, but ensure it does not also claim
   player chat. Do not kill main Muse or Minecraft.
3. Point the existing main Muse session's chat wake path at the existing
   live inbox; use `event_receiver arm --db "$CHAT_INBOX" --types chat`
   when a dedicated chat arm is supported by the host. If the host has a
   single combined arm, it must route other `nonchat` events too and ACK
   them via their own policy; `prepare-routed` intentionally does NOT
   mark them handled. Avoid two competing arms for the same chat.
4. On each new chat, MAIN runs `child_once` then **only**
   `prepare-routed` (exactly one batch of explicit ordered `routes`).
   This is the C fast data path, but MAIN owns the conversation. No
   `chat_replies prepare` legacy route and no direct `say` from MAIN.
5. Use the **single** already available `chat_replies send --db
   "$CHAT_INBOX" --m1-say` (without `--once`). If a live sender is
   already active, KEEP it; do not spawn a duplicate. Confirm unique
   writer and at-most-once semantics.
6. When MAIN routes a task, it examines `task_handoffs list`, atomically
   claims an available task, spawns a supported Muse child worker with
   the returned task ID and original instruction, and supervises its
   completion. MAIN owns interruptions and user-facing progress.
   Research workers may be parallel; only one logical `game` claim at
   once; M1/Baritone/Mod physical movement must also have one controller.

## Gate 3 — evidence, hard stops, rollback

Observe *natural* test-server activity; do not demand the owner send
artificial chats. At least cover: simple greeting; one mining instruction;
follow-up question while worker is active; second queued game task;
interruption; task completion feedback. Capture per-event_id timing:
T0 game chat -> T1 inbox -> T2 MAIN resumed -> T3 routed commit ->
T4 game-visible chat. Record worker spawn/active/result separately.

- Any duplicate player-facing reply, task silently ACKed without a
  handoff, main unable to wake, wrong game-control owner, unsafe task
  continuation after STOP, or backlog growing without resolution is a
  **stop/rollback condition**.
- Rollback is SINGLE-owner and non-destructive: disarm new MAIN chat
  entry, stop new worker scheduling, inspect claimed tasks/queued replies,
  then restore original old-C chat path only after the new chat writer
  is definitely out of service. Keep the SQLite data and audit logs,
  never replay `uncertain` or `sending` replies, never auto-reexecute
  `claimed` game tasks. Report owner cutover status, git SHA, process
  tree, original task statuses, and failure reason.
- Known limitations: the Muse host does not currently support warm main
  or child session auto-recovery across host runtime restart, and M1
  `say` may block the single sender for 15 seconds on missing OK. These
  are not solved by this rollout; if they prevent acceptable operation,
  preserve rollback and file explicit follow-up issues.

The main runtime owns decisions; PR #7 supplies safe persistent
handoff primitives, not automatic agent spawning, a second Minecraft
client, or a hard low-latency SLA.
