# Sender redesign: one-shot say + independent game-log write-off

Status: **design + decision-core tests only**. The live sender is unchanged.
This document is the pre-implementation design requested in Issue #5
(#6335603441 §2): submit design/tests first, change the live sender after.

## Problem (measured, not hypothetical)

On M1 v0.17.5, `say` has no command-level OK receipt. With frame-category
diagnostics enabled (see `m1_chat.py`, `M1_CHAT_DIAG`), 11/11 real
attempts showed: greeting accepted instantly; the say phase received
exactly one `async_chat` frame (the self-echo of the spoken line) and then
nothing for the full 15s receipt wait, ending in `reply_timeout`. The
messages did appear in the game, yet every row was recorded `uncertain`,
and each attempt blocked the single dispatcher for ~15.25s.

Conclusions that constrain the design:

- Waiting for an OK is waiting for a frame that does not exist.
- The `[chat]` self-echo must **not** be promoted to an ACK: it is
  indistinguishable from backlog echoes of earlier lines. It stays a
  diagnostic signal only.
- Delivery evidence must come from an independent channel: the client
  game log, reconciled against the specific attempt.

## Design

Split the current single step into two decoupled stages:

### Stage 1 — one-shot dispatch (replaces the receipt wait)

For each queued row, the dispatcher:

1. Records the attempt start (wall + monotonic), and the **pre-send log
   position**: current log generation id + byte offset floor, taken from
   the same durable cursor machinery the relay already maintains.
2. Connects, reads the greeting (bounded, as today), sends exactly one
   `say`, and closes as soon as the write completes or a short transport
   error occurs. No 15s receipt wait.
3. Persists the attempt outcome as `sent_unverified` (new state) with the
   attempt window and offset floor stored on the row. A transport-level
   failure (connect/write error) still lands `uncertain` immediately.
4. Never retries: `sent_unverified` and `uncertain` are both terminal for
   the dispatcher. At-most-once is preserved structurally.

### Stage 2 — independent write-off watchdog

A separate pass (its own process or a `chat_replies reconcile` command run
on an interval) owns all `sent_unverified` rows:

1. For each row, it scans game-log entries **after the row's offset
   floor** using a durable read cursor (rotation/truncation produces an
   `event_gap`; a row whose evidence region is lost to a gap can never be
   confirmed and stays `uncertain`).
2. It feeds (attempt, entries) into the pure matcher in
   `log_reconcile.py`. Confirmation requires ALL of: offset > floor;
   sender exactly `DouDou`; text exactly the queued `reply_text`;
   timestamp within [attempt_start − ε, attempt_end + grace].
3. On confirm, the row transitions `sent_unverified → game_log_confirmed`
   and stores audit evidence: evidence offset, generation, match
   timestamp, matcher reason. The original attempt record is kept; the
   state history is append-only. On no-match, the row stays
   `sent_unverified` until the scan horizon passes, then remains
   `uncertain`-equivalent for reporting (no silent success, no retry).

### Window sizing (no arbitrary short timeout)

The watchdog must not declare failure just because the log is slow to
flush. It confirms **opportunistically**: it keeps scanning while new log
data arrives and only retires a row when the observed log timestamp has
moved past `attempt_end + grace` (grace ≫ worst observed flush/relay lag;
the P0-E telemetry puts log visibility at ≲0.35s after the line is
spoken, so a 30s default grace is two orders of magnitude of headroom,
and it is a *retirement* bound, not a confirmation deadline). If the log
stalls entirely, rows simply stay pending; nothing is misjudged.

### False-positive guards

- **Offset floor** — identical text spoken before this attempt (history,
  backlog echoes) is below the floor and can never confirm.
- **Single-writer scope** — only rows this dispatcher attempted carry an
  offset floor + window, so only they can be confirmed. Lines the main
  agent sends directly through the launcher are not in `chat_replies`
  and cannot be written off against a queue row; an identical text from
  the main agent inside the window is the one residual collision, which
  is why confirmation also requires the offset to be after the floor AND
  the timestamp to be inside this attempt's window — and why the audit
  record keeps the evidence offset for human review.
- **Exact text + exact sender** — no fuzzy matching anywhere.
- **`async_chat` is never an input to the matcher.** It remains available
  in the frame diagnostics for protocol debugging only.

### Failure modes kept explicit

- Log rotation/gap over the evidence region → row never confirms (audited
  as `event_gap`), stays uncertain. No guessing.
- Log unreadable / DB fault → watchdog reports an error and exits without
  touching rows (same fail-closed posture as the receiver).
- Multiple queued rows with identical text in overlapping windows: each
  row has its own floor; a single log line may confirm at most the
  earliest still-unconfirmed row whose floor precedes it — the watchdog
  must consume evidence offsets monotonically per row set and record
  which row consumed which offset. (Flagged here as the one rule the
  implementation must encode carefully; the pure matcher itself is
  per-attempt and does not arbitrate between rows.)

## What is in this change vs later

In this change: the frame-diagnostics (already used for the 11/11
observation) productized in `m1_chat.py`, and the pure matcher
`log_reconcile.py` with deterministic tests. Not in this change: the
`sent_unverified` state, the one-shot dispatcher change, the watchdog,
schema/audit columns — those land after this design is reviewed.
