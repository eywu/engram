# Engram V3 — M4 Milestone Report

**Milestone:** M4 — Human-in-the-Loop (HITL) permission gate
**Completed:** 2026-04-22
**Author:** Eric Wu (retrospective)
**Status:** ✅ All stories merged, live demo verified, one critical bug found-and-fixed mid-sprint

---

## What shipped

M4 delivered the HITL permission system: Engram can now pause mid-turn and ask the operator (via Slack Block Kit buttons) to approve or deny individual tool invocations before they execute. The gate is a genuine blocking mechanism — tools do not run until the operator responds or the timeout fires.

### Stories merged (10)

| Story   | Title                                                                     | Merged commit |
|---------|---------------------------------------------------------------------------|---------------|
| GRO-403 | HITL core state machine + permission request storage                      | *(pre-sprint)* |
| GRO-404 | Slack Block Kit question renderer                                          | *(pre-sprint)* |
| GRO-405 | `permission_request` hook wiring                                           | *(pre-sprint)* |
| GRO-406 | Timeout scheduler + cleanup                                                | *(pre-sprint)* |
| GRO-407 | Scope-based allow/deny rules (fast path, no user prompt)                   | *(pre-sprint)* |
| GRO-408 | Button action handler + `AskUserQuestion` round-trip                       | d21a848 |
| GRO-422 | `@app.action` registration for HITL block buttons                          | *(mid-sprint)* |
| GRO-427 | HITL structured observability (8 event types)                              | d68c271 |
| GRO-426 | **Critical gate fix** — migrate from `PermissionRequest` to `can_use_tool` | 4cede63 |
| GRO-409 | Live demo + this report                                                    | this commit |

### Test delta

- M3 end state: **222 tests passing**
- M4 end state: **284 tests passing** (+62)
- Key additions: `ToolGateFakeClient` integration tests (GRO-426) that raise real SDK errors when the gate is bypassed — a test can only pass if the blocking behavior is genuine, not faked

### Code landmarks

- `src/engram/hitl.py` — state machine, registration, answer resolution (GRO-403/408/422/427)
- `src/engram/slack_blocks.py` — question renderer (GRO-404)
- `src/engram/agent.py` — `build_hitl_tool_guard(precheck=can_use_tool)` wiring (GRO-426)
- `src/engram/main.py` — `_schedule_timeout_update` (GRO-408)

---

## Demo outcome (live verification)

Bridge restarted 2026-04-22 15:34 PDT on commit `4cede63`, pid 3457. Both halves of the HITL contract verified with full bridge-log evidence.

### Test 1 — Allow path

**Prompt in owner-DM:** "Write me a haiku about engrams and save it to /tmp/m4-demo3.txt"

```
22:35:47.696  hook.pre_tool_use        Write /tmp/m4-demo3.txt
22:35:47.772  hitl.tool_guard_fired    tool_name=Write
22:35:47.774  hitl.question_registered permission_request_id=a73e207d…
22:35:48.115  hitl.question_posted     slack_channel_ts=1776897348.042059
[ 11.1 second wait — operator not responding ]
22:35:58.872  hitl.answer_received     choice=0 decision=allow
22:35:58.877  hitl.tool_guard_returned decision=allow duration_ms=11104
```

**File stat:** `/tmp/m4-demo3.txt  mtime=22:35:58Z  size=74 bytes`

**Critical observation:** the file did NOT exist at 22:35:55 (mid-wait). The file was written at 22:35:58, *after* `answer_received`. Write executed because the operator approved, not because the guard was a cosmetic overlay. This is the behavior GRO-426 was filed to fix — and proof that the fix landed.

### Test 2 — Deny path

**Prompt:** "Write me another haiku to /tmp/m4-demo4.txt"

```
22:36:28.614  hook.pre_tool_use        Write /tmp/m4-demo4.txt
22:36:28.620  hitl.tool_guard_fired
22:36:28.621  hitl.question_registered permission_request_id=9589070f…
22:36:28.896  hitl.question_posted     slack_channel_ts=1776897388.830489
22:36:30.733  hitl.answer_received     choice=deny decision=deny
22:36:30.736  hitl.tool_guard_returned decision=deny duration_ms=2115
```

**File stat:** `/tmp/m4-demo4.txt  DOES NOT EXIST`

**Slack reply:** agent posted "Deny with (no response)" and cleanly halted the turn via `PermissionResultDeny(interrupt=True)`. No orphan state, no retry loop. Agent turn terminates on deny — safety contract preserved.

### Timeout path

Verified indirectly via the timeout update unit test `test_production_timeout_callback_updates_slack` (merged in GRO-408 as d21a848). The timeout scheduler's real-world behavior was exercised during Codex's autonomous build but the full 5-minute timeout was not reproduced in the live demo — accepted as acceptable because the code path is covered by a passing integration test and the demo's allow/deny evidence proves the broader gate mechanism is live.

---

## SDK surprises encountered

### 1. `PermissionRequest` hook is fire-and-forget (the M4 near-miss)

**Discovery date:** 2026-04-22 13:41 PDT (post-demo attempt 1)

The original M4 design used the Claude Agent SDK's `PermissionRequest` hook. The hook **fires before tool execution** — which read like "it blocks until resolved." It does not. `PermissionRequest` is a **notification** hook that posts the UI and returns; the SDK does not wait for the operator to click before invoking the tool.

**Evidence from demo attempt 1:** HITL card posted correctly, green "Answered: choice" checkbox rendered, BUT the haiku file was already written before the button was clicked. The card was a spectator badge, not a gate.

**Fix (GRO-426):** migrate the gate to `can_use_tool` — a genuine async callback the SDK awaits before tool dispatch. `build_hitl_tool_guard(precheck=can_use_tool)` wraps scope checks and shares a `_request_hitl_decision` helper with the legacy hook (kept for observability). Verified via `ToolGateFakeClient` integration tests that the tool is never called before the callback resolves.

**Lesson:** hook-name semantics in the SDK docs are under-specified. When a hook "fires before" a tool, that does not imply "blocks the tool." Always verify blocking behavior with a test that proves the tool didn't run.

### 2. Claude CLI rejects session-id reuse across bridge restarts

**Discovery date:** 2026-04-22 15:32 PDT (during demo setup)

The bridge derives a deterministic `session_id` from `channel_id`. On restart, `SessionState` reconstructs fresh with `agent_session_initialized=False`, so the next turn passes `session_id=<uuid>` to the SDK. But the transcript jsonl at `~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl` still exists from before restart, and the CLI rejects reuse with `ProcessError: "Session ID ... already in use"`.

**Fix (GRO-433, merged 9527048):** detect existing transcript on disk before building SDK options; if the jsonl exists, flip the in-memory flag to `True` so `_build_options(resume=True)` is passed instead of a fresh `session_id`. The SDK then issues `--resume <uuid>`, which is the documented reattach path. The sanitization algorithm for resolving transcript paths matches the Claude CLI bit-for-bit (NFC normalize, `[^a-zA-Z0-9] → '-'`, `realpath`, Java `String.hashCode` + base36 for >200-char paths).

**Lesson:** deterministic session IDs + restart cycles + filesystem-backed session state = collision hazard. The SDK's `resume=True` flag is the documented escape hatch; use it whenever there's evidence of prior state.

### 3. `can_use_tool` wraps hooks — precheck-deny path silently re-routes

The `build_hitl_tool_guard(precheck=can_use_tool)` wrapper has two branches: user-prompt (HITL card) and precheck-deny-short-circuit (scope rule fast-fails without asking). The short-circuit path currently does NOT emit `tool_guard_fired` / `tool_guard_returned` events — observability is silent on that branch (filed as GRO-431 for follow-up).

**Lesson:** wrapper semantics need explicit tests for each branch. Filing observability gaps as follow-ups is acceptable when the core contract is verified; they become M5 budget risks if left unsplit.

---

## Lessons for M5 (self-improvement loop)

1. **Verify blocking behavior with adversarial tests.** M5 will introduce cross-channel meta-summaries and nightly synthesis with its own tool invocations. Any gate-like mechanism (e.g., exclusion list enforcement at the memory_search layer — see GRO-434) must be proven with a test that fails on bypass, not just a "hook fired" assertion. The M4 near-miss came from trusting a hook name.

2. **Deterministic IDs + persistent state = collision hazard.** M5's nightly process reuses the memory.db, writes summaries with `UNIQUE(channel_id, day, trigger)` (GRO-439/441 revised ACs). Plan for `ON CONFLICT` semantics from day one — we already did, informed by GRO-433's lesson.

3. **HITL must be explicitly disabled for nightly.** Nightly runs have no operator to click buttons. GRO-440 (M5.2) carries an explicit `hitl_config.enabled=False` requirement, verified in GRO-436 (M5.0.75 smoke test). Do not inherit HITL by default outside the bridge context.

4. **Channel-scoped isolation must be enforced at the data layer, not just the UI.** OQ31's opt-in exclusion list was a UI-level consent surface; GRO-434 (M5.0) wired it into the SQL `WHERE` clause so the exclusion is physically impossible to bypass. Same pattern applies to any M5 feature that claims channel isolation.

5. **Observability events pay for themselves during demo prep.** The 8 HITL log events added in GRO-427 were the only reason the GRO-426 bypass was immediately diagnosable. M5.0-obs (GRO-437) extends this pattern: heartbeat file + dedicated nightly JSONL + failure-path DM. Ship the observability before the code it observes, not after.

---

## Done criteria

- [x] Bridge restarted clean (launchd verified; no errors in log)
- [x] One real button-click interaction works end-to-end (Test 1, 15:35 PDT)
- [x] Timeout path verified (via integration test `test_production_timeout_callback_updates_slack`)
- [x] `m4-report.md` committed (this file)
- [x] Demo evidence attached (Linear GRO-409 comment + this report)

---

## Appendix: related mid-sprint stories filed

These emerged from M4 work and were filed for M5+ scheduling rather than blocking M4 completion:

- **GRO-430** — precheck-deny short-circuit + updated_input branch test coverage (P3)
- **GRO-431** — emit `tool_guard_fired/returned` events on precheck-deny path (P3)
- **GRO-432** — deprecate `build_permission_request_hook` (zero production wiring, confusion trap) (P3)
- **GRO-433** — ✅ **merged** — bridge-restart session JSONL collision fix
- **GRO-434** — ✅ **merged** — M5.0 memory_search channel-exclusion (OQ31 isolation)
- **GRO-428** — ❌ **canceled** — memory stop_watermark_not_found investigation (not a bug; trigger eliminated by GRO-433)
- **GRO-447** — memory log-message polish (P4, nit)
