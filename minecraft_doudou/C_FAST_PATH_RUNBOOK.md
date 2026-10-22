# DouDou chat: C fast path is the default (2026-10-22)

**Decision:** The owner chose variant C without further owner-assisted gameplay
benchmarking. The earlier A/B comparison and 11-message real game experiment
are sufficient to choose the default workflow, **not** sufficient to claim
a stable response-time SLA. Further manual p50/p90 testing is postponed until
the owner can participate.

## Muse runtime procedure for each new natural-language chat

Preserve the **existing** event receiver / arm wake mechanism and the
dedicated, single M1 chat sender. What changes is the **child Agent's
tool sequence**, not M1/mod/world control or server routing.

1. The agent is woken by an already-armed event listener. On its first tool
   turn, run exactly one read-only process (absolute entry point and explicit
   working directory/PYTHONPATH from the known working install):

       python3 -m minecraft_doudou.child_once --db "$CHAT_INBOX" \
         --state "$DOUDOU_STATE" --sender Huhabubu

   Use the real inbox and state paths confirmed on the VM. Do not copy the
   above placeholders into production commands or invent a new db.
2. Read the returned **chats** in order, oldest to newest, and the compact
   state. The **chat** field is the newest event and is the only ID on which
   to claim/ACK the batch. If more than one player message is pending, make
   one concise reply that addresses all of them as appropriate. If the
   latest event is already old (>120s), do not attempt a new automatic reply.
   Prepare enforces the durable age/claim boundary. Never ACK unseen events.
3. Formulate a short accurate natural-language answer. For status questions,
   rely only on a fresh snapshot; when state says in_world false, do not
   invent health, food, clock or position. A request to stop, navigate, mine,
   attack, craft, etc. remains a **control handoff to the main Agent** using
   the already established control-relay protocol, not a chat-child action.
4. Call **one** prepare tool as the second and final tool:

       python3 -m minecraft_doudou.chat_replies prepare \
         --db "$CHAT_INBOX" --event-id "<latest chat.event_id>" \
         --through-seq <latest chat.seq> --text "<model-composed safe reply>"

   Save raw stdout, and treat queued as success, duplicate/conflict as no
   permission to retry. Only the independent sender may send game chat.
5. Re-arm the existing child lifecycle and continue the main Agent's work.
   Do not spawn a second sender for the same inbox, and do not send duplicate
   chat through the main Agent. Keep the previous at-most-once/staleness rules.

## Fallbacks (automatic, no need to ask the player to conduct a test)

- No pending chat: no reply. Return to listening.
- child_once returns nonzero (missing DB, >20 pending chats, a pending chat
  from an unexpected sender, or invalid source data): use the older,
  validated multi-step read/classify process. Do not guess an event ID,
  acknowledge an unseen batch, or send a canned response.
- State missing, stale (>3s based on captured_at_ms), or malformed: child_once
  returns in_world false; a greeting may still be answered, but actual
  status requests should be qualified or transferred to main Agent.
- Game-side side effects and stop/control requests follow existing ownership
  boundaries; fast chat only reads state and prepares text.
- M1 v0.17.5 say is observed to provide no ordinary command OK reply.
  This document does **not** pretend the sender's current 15s uncertain
  accounting is fixed; the game-log reconciliation proposal remains a
  separate design/implementation/CI task. Do not replay old uncertain rows.

## Evidence and expected behavior

Muse's P0-E 3x-group experiment: A median 13.45s, B median 15.25s,
C median 8.01s (C samples 9.34/8.01/6.44s). A separate strict
busy-before-event C trial was slower (~14.78s), establishing real platform
scheduling tails. Therefore enable C as the **default workflow**, but
do not claim replies are guaranteed under 8 seconds.

Acceptance for this code-only adoption is: the child uses the one-shot fetch
and one prepare (not separate drain/state reads); static regression/CI passes;
no alternate process is started; no game action/old event is replayed.
The owner has **postponed** additional hands-on gameplay benchmarking.
