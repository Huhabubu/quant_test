"""Game-log write-off matcher for M1 say attempts (design-first module).

Context: on M1 v0.17.5, ``say`` has no command-level OK receipt; the only
wire feedback is the [chat] self-echo, which must NOT be treated as an ACK
(Issue #5, P0-E protocol observation: 11/11 attempts saw exactly one
async_chat frame and then silence). Delivery evidence therefore comes from
an independent source: the client game log, reconciled after the attempt.

This module is the pure decision core of that design. It is deliberately
NOT wired into the live sender yet (see GAME_LOG_WRITEOFF.md): the
dispatcher still marks attempts ``uncertain``; a future watchdog will call
:func:`reconcile` and may then upgrade a row to ``game_log_confirmed``
with the evidence recorded here. Nothing in this module sends, retries,
or mutates queue state.

An attempt is confirmed only when ALL of the following hold for one log
entry (multi-factor, per the Issue #5 review):
- the entry sits strictly after the attempt's pre-send log offset floor
  (historical lines with identical text can never confirm);
- the entry's sender is exactly the expected speaker (``DouDou``);
- the entry's text equals the queued reply text exactly;
- the entry's timestamp falls inside the attempt window
  [window_start - epsilon, window_end + grace].
"""
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Attempt:
    event_id: str
    reply_text: str
    expected_sender: str
    window_start: float        # epoch seconds; say was sent at/after this
    window_end: float          # epoch seconds; attempt concluded by this
    log_offset_floor: int      # byte offset of the log before the send


@dataclass(frozen=True)
class LogEntry:
    offset: int                # byte offset of this line in its log file
    sender: str
    text: str
    ts: float                  # epoch seconds when the line was observed


@dataclass(frozen=True)
class Verdict:
    confirmed: bool
    reason: str                # machine-stable reason string
    evidence_offset: Optional[int] = None


def reconcile(attempt: Attempt, entries: Iterable[LogEntry], *,
              epsilon_s: float = 1.0, grace_s: float = 30.0) -> Verdict:
    """Decide whether ``entries`` prove delivery of ``attempt``.

    Scans in the order given; the first fully matching entry confirms.
    Non-confirming scans still report the most informative reason so the
    watchdog can audit why a row stayed uncertain.
    """
    saw_after_floor = False
    saw_same_text_other_sender = False
    saw_same_text_outside_window = False
    for entry in entries:
        if entry.offset <= attempt.log_offset_floor:
            continue  # pre-existing history can never be evidence
        saw_after_floor = True
        if entry.text != attempt.reply_text:
            continue
        if entry.sender != attempt.expected_sender:
            saw_same_text_other_sender = True
            continue
        if not (attempt.window_start - epsilon_s
                <= entry.ts <= attempt.window_end + grace_s):
            saw_same_text_outside_window = True
            continue
        return Verdict(True, "confirmed", entry.offset)
    if saw_same_text_outside_window:
        return Verdict(False, "same_text_outside_window")
    if saw_same_text_other_sender:
        return Verdict(False, "same_text_other_sender")
    if saw_after_floor:
        return Verdict(False, "no_matching_line")
    return Verdict(False, "no_entries_after_floor")
