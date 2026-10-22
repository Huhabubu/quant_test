"""Deterministic regressions for the game-log write-off matcher."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from minecraft_doudou.log_reconcile import Attempt, LogEntry, reconcile

ATTEMPT = Attempt(
    event_id="evt-1",
    reply_text="hello there",
    expected_sender="DouDou",
    window_start=1000.0,
    window_end=1001.0,
    log_offset_floor=5000,
)


def entry(offset, sender="DouDou", text="hello there", ts=1000.5):
    return LogEntry(offset=offset, sender=sender, text=text, ts=ts)


class ReconcileTests(unittest.TestCase):
    def test_exact_match_after_floor_inside_window_confirms(self):
        verdict = reconcile(ATTEMPT, [entry(6000)])
        self.assertTrue(verdict.confirmed)
        self.assertEqual(verdict.evidence_offset, 6000)

    def test_identical_historical_line_before_floor_cannot_confirm(self):
        verdict = reconcile(ATTEMPT, [entry(4999), entry(5000)])
        self.assertFalse(verdict.confirmed)
        self.assertEqual(verdict.reason, "no_entries_after_floor")

    def test_same_text_from_another_sender_cannot_confirm(self):
        verdict = reconcile(ATTEMPT, [entry(6000, sender="Huhabubu")])
        self.assertFalse(verdict.confirmed)
        self.assertEqual(verdict.reason, "same_text_other_sender")

    def test_same_text_outside_window_cannot_confirm(self):
        verdict = reconcile(ATTEMPT, [entry(6000, ts=1200.0)])
        self.assertFalse(verdict.confirmed)
        self.assertEqual(verdict.reason, "same_text_outside_window")

    def test_near_match_text_cannot_confirm(self):
        verdict = reconcile(ATTEMPT, [entry(6000, text="hello there ")])
        self.assertFalse(verdict.confirmed)
        self.assertEqual(verdict.reason, "no_matching_line")

    def test_no_entries_at_all(self):
        verdict = reconcile(ATTEMPT, [])
        self.assertFalse(verdict.confirmed)
        self.assertEqual(verdict.reason, "no_entries_after_floor")

    def test_main_agent_direct_send_is_not_mistaken(self):
        # A line the main agent sent directly through the launcher shares
        # the sender name, but if it precedes this attempt's floor it is
        # history, and a later identical queue text at a different offset
        # still needs its own in-window entry to confirm.
        verdict = reconcile(ATTEMPT, [entry(4000, ts=999.0)])
        self.assertFalse(verdict.confirmed)


if __name__ == "__main__":
    unittest.main()
