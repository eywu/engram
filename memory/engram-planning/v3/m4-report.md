# M4 Report: HITL for Engram V3

Date: 2026-04-22
Milestone: M4
Ticket: GRO-409

## Summary

M4 shipped the native human-in-the-loop path for the V3 Slack bridge: SDK permission requests now become Slack Block Kit questions, operator answers flow back into the pending Claude turn, and channel/manifest configuration can tune HITL behavior.

The live bridge was restarted cleanly through launchd during GRO-409. The live Slack button-click and six-minute timeout demos were prepared, but remain blocked on an operator-side Slack reply/click. The automated environment could verify Slack history and launchd state, but could not type or click in Slack because macOS UI automation was unavailable.

## What Shipped

Seven M4 stories landed before this report:

1. GRO-402: Removed hardcoded embedding-cost status scaffolding and kept status output tied to real runtime data.
2. GRO-403: Added HITL foundation primitives: `PendingQuestion`, `HITLRegistry`, and per-channel daily rate limiting.
3. GRO-404: Wired Claude Agent SDK `PermissionRequest` hooks into the agent options path.
4. GRO-405: Added Slack egress for HITL questions with Block Kit buttons, resolved-state updates, and timeout-state updates.
5. GRO-406: Added Slack ingress handlers for button actions and thread replies.
6. GRO-407: Added end-to-end HITL integration coverage for happy path, timeout, restart recovery, thread replies, and caps.
7. GRO-408: Added `HITLConfig`, manifest defaults, README docs, and runtime wiring for channel-specific HITL settings.

Test count delta for M4 functional work:

- Before M4: 214 test functions at `0efce7d` (parent of GRO-402).
- After GRO-408: 263 test functions at `d21a848`.
- Delta: +49 test functions.
- GRO-409 added one regression test for production timeout update wiring; current suite collection is 272 tests.

## GRO-409 Live Verification

### Bridge Restart

Command used:

```sh
launchctl unload ~/Library/LaunchAgents/com.engram.bridge.plist
launchctl load ~/Library/LaunchAgents/com.engram.bridge.plist
```

Verification:

- `launchctl list | grep engram` returned `com.engram.bridge` with exit status `0`.
- The bridge emitted fresh boot, Socket Mode session, and `engram.ready` lines.
- A second restart was performed after a small timeout-wiring fix; it also came up cleanly.

Redacted recent log evidence:

```text
2026-04-22T08:30:38Z engram.boot version=0.1.0
2026-04-22T08:30:40Z engram.starting socket_mode=True
2026-04-22T08:30:41Z slack_bolt.AsyncApp A new session was established
2026-04-22T08:30:41Z engram.ready
```

### Button-Click Demo

Requested live prompt:

```text
Run printf 'GRO-409 happy path' in the shell. If you need permission, ask first with the buttons, then tell me the exact output.
```

Status:

- Owner-DM setup instruction was posted.
- Slack history showed only bot-originated setup messages.
- No human owner-DM reply was observed.
- No HITL button message was observed.
- No resolved `Answered:` update was observed.

Because the reply/click did not occur, this report does not claim the live button-click path was verified.

### Timeout Demo

Requested timeout procedure:

1. Send a command prompt that triggers a permission question.
2. Do not click any button for at least six minutes.
3. Verify the original question updates to `Timed out` and the bridge does not hang the channel session.

Status:

- The live timeout demo remains blocked on the same missing owner-DM reply.
- During GRO-409, a production wiring gap was found: timeout updates existed in `egress.py`, but `main.py` did not schedule the update after posting a question.
- `main.py` now schedules `update_question_timeout()` when `asyncio.wait_for()` cancels the pending question future.
- Regression test added: `tests/test_hitl_integration.py::test_production_timeout_callback_updates_slack`.

Automated verification:

```text
uv run pytest
272 passed in 3.59s
```

## SDK Surprises

- SDK permission suggestions are opaque enough that Slack labels need defensive extraction. M4 handles dict-shaped suggestions and SDK objects separately.
- The timeout mechanism depends on `asyncio.wait_for()` cancelling the pending future. That cancellation is useful, but the production Slack update must be wired explicitly.
- Restarting the bridge loses in-memory pending questions. M4 tests cover graceful recovery for stale button clicks, but durable pending-question state would be needed for resumable HITL.
- Per-channel `ClaudeSDKClient` instances still require strict serialization. The per-channel lock remains a hard boundary for correctness.
- User-level `setting_sources` in owner DM matters for both tool availability and cost profile; team channels need stricter project-level settings.

## Lessons for M5

- The self-improvement loop should capture live transcripts automatically from Slack history after each demo run, with redaction before commit.
- HITL observability needs first-class counters: questions posted, buttons clicked, thread replies, timeouts, stale clicks, and rate-limit denials.
- Timeout copy should be revisited. The current Slack timeout text says the bot will proceed with a best guess, while the hook returns a deny with interrupt semantics.
- Pending HITL state should move out of process memory if restart-resume becomes a product requirement.
- A local demo harness should exist for Socket Mode flows. Without a human Slack click or a user token, a coding agent can verify logs and history but cannot honestly complete a real button-click demo.

## PR Description Attachment

No PR description was updated from this environment because no GitHub CLI or GitHub connector was available. The redacted transcript status above is the current attachable demo note.
