# Permission Tiers + YOLO + Upgrade Flow — Ultraplan

**Status:** Approved 2026-04-23 — ready for Linear story filing
**Author:** Eric Wu
**Created:** 2026-04-23
**Signed off:** Eric (Q1/Q2/Q3 resolved below)

## Decisions

- **Q1 — YOLO destructive commands:** Do NOT flat-deny. Implement a *confirm-modal* escalation where owner types the channel name or `CONFIRM DESTRUCTIVE` to proceed. Owner is never truly blocked from acting on their own machine, but accidental click-through is impossible.
- **Q2 — Slash commands:** Multi-arg dispatcher (`/engram upgrade`, `/engram yolo`). One command to remember.
- **Q3 — `engram-self` tonight:** No hotfix. Ship the feature, Eric flips via `/engram upgrade owner-scoped` when it lands. `engram-self` joins the self-improvement loop (meta_eligible: true) once upgraded — that's desired.

## Motivation

Three user-surfaced issues, single root cause:

1. **Private owner-only channels (like `engram-self`) are treated as shared team channels.** They use `task-assistant` template → deny-only permissions, 3-prompt daily cap, no "Always allow" sticky button. Result: friction in channels where Eric is the only human audience.
2. **No way for a user to escalate a channel's trust level.** Today the only way to raise trust is to edit YAML on the host and restart the bridge.
3. **HITL daily cap (`max_per_day: 3`) is too restrictive even for its stated use case.** Rationale in `docs/hitl.md` is "prevent prompt spam in a busy room," but 3/day is low enough to make normal agentic work infeasible.

## Design Principles

1. **Trust is a per-channel spectrum, not a channel-type binary.** Replace `is_owner_dm()` dispatch with an explicit `permission_tier` field.
2. **Absolute deny list is independent of tier.** Credentials, SSH keys, AWS creds, GPG, dotenv: denied in all tiers, including YOLO. SDK enforces deny rules before `bypassPermissions` is consulted.
3. **Escalation requires owner approval.** Anyone can request; only owner can grant. No delegation in v1.
4. **YOLO is time-boxed and auditable.** 24h default, lazy expiry check on each turn, DM notification on expire, slash command to revoke.
5. **Mirror existing patterns, don't invent new ones.** We already have slash commands + interactive approval flow for pending-channel actions; reuse those shapes.

## Three Tiers + Dangerous-Action Escalation

| Tier | Default for | Permissions | Sticky "Always allow" | HITL cap/day | Credential deny list | Footgun pattern behavior |
|---|---|---|---|---|---|---|
| `task-assistant` | Non-DM channels | Conservative deny-only (no auto-allow) | No | 1000 | Full shared-channel set | Flat deny (no confirm path — teammates can't trigger nuclear stuff) |
| `owner-scoped` | Owner-private channels (new) | Auto-allow Read/Grep/Glob/WebFetch/WebSearch/TodoWrite | Yes | 1000 | Owner-DM set (narrower) | Type-to-confirm modal (owner-only) |
| `yolo` (time-boxed) | Explicit request only | `permission_mode: bypassPermissions` — no HITL prompts | N/A | N/A (no prompts) | Owner-DM set | Type-to-confirm modal (owner-only) |

**Owner-DM `D0ATYM0UMTM`** behaves as `owner-scoped` — no code change, just initial tier value.

**Absolute credential deny list** (applies to all tiers including YOLO, no escalation path):
- `Read/Grep/Glob(~/.ssh/**)`
- `Read/Grep/Glob(~/.aws/**)`
- `Read/Grep/Glob(~/.gnupg/**)`
- `Read/Grep/Glob(~/Library/Keychains/**)`
- `Read/Grep/Glob(**/.env*)`
- (owner-scoped narrow) — does NOT deny `~/.config/**` or shell history
- (task-assistant broad) — ALSO denies `~/.config/**`, `~/.zsh_history`, `~/.bash_history`

Credential access has no escalation because "I want the bot to exfiltrate my SSH key" is never the intent. If you really need that, hand-edit the manifest.

## Dangerous-Action Escalation (new, per Q1)

Some commands look destructive but occasionally the owner really does want them. Example: `rm -rf node_modules`, `pg_dump --clean`, `git reset --hard origin/main`. YOLO should not silently run these, but the owner shouldn't be hard-blocked either.

**Footgun patterns** (match against any `Bash` invocation pre-execution):
- `rm -rf` (any target, because targets are often variables)
- `sudo ` (anywhere in the command)
- `curl ... | sh|bash|zsh` or `wget ... | sh|bash|zsh`
- `dd if=`, `mkfs`, `fdisk`
- `chmod -R 777`
- `> /dev/sda` and friends
- `git push --force` to any non-personal branch (best-effort; matched by regex)
- `drop database|drop table|truncate table` (SQL via psql/mysql clients)
- (list curated in `src/engram/footguns.py`, versioned, testable)

**Escalation flow:**

1. Pre-tool-use hook detects a footgun pattern.
2. Hook returns `PermissionResultAsk` (not auto-allow, even in YOLO).
3. Engram posts a special card in the channel with:
   - ⚠️ header: **Destructive action confirmation required**
   - Full command text in a code block
   - Brief explanation of what matched (`Matched rule: rm -rf`)
   - Text input: "Type `CONFIRM` to proceed"
   - `[Submit]` button (disabled until exact string matches)
   - `[Cancel]` button
4. Only the owner can submit. Non-owner attempt → ephemeral "Owner approval required for destructive actions."
5. On submit: command executes. On cancel/timeout: `PermissionResultDeny`.
6. Submission logged: `footgun.confirmed` with matched-rule, command, user, timestamp.
7. Sticky "Always allow" is explicitly disabled for footgun patterns. Every destructive action requires fresh confirmation — no trust accumulation.

**In `task-assistant` tier**, footgun patterns flat-deny with no escalation path. Rationale: in a shared channel, the bot should never even ask if it can `rm -rf` something, because the answer is always no regardless of who asks. Owner wanting to run destructive commands in a shared channel is a specific-enough request that hand-editing the manifest or moving to a private channel is the right answer.

**In `owner-scoped` and `yolo`**, footgun patterns trigger the confirm-modal. Even yolo isn't a free pass for `rm -rf /` via typo.

## Escalation Flow

### In-channel request

```
user> /engram upgrade owner-scoped this is my private workspace
```

1. Engram posts in source channel: "⏳ Permission upgrade requested — waiting for owner approval."
2. Engram posts in owner-DM with interactive buttons:
   ```
   **Permission upgrade request**
   Channel: #engram-self (C0AUGSB9M1D)
   Requested by: @ey_wu
   Current tier: task-assistant
   Requested tier: owner-scoped
   Reason: "this is my private workspace"

   [Approve until revoked]  [Approve 30d]  [Deny]
   ```
3. Owner clicks. Manifest updated. In-channel message edited to "✅ Upgraded to owner-scoped by @ey_wu" or "❌ Denied."
4. YOLO requests default to 24h expiry: buttons are `[Approve 24h] [Approve 6h] [Deny]`.

### Owner CLI shortcut

```
engram channels upgrade <channel-id> <tier> [--until 24h|30d|permanent]
```

Skips DM round-trip; applies immediately. How Eric fixes `engram-self` tonight.

### YOLO listing & revocation

Slash commands (usable in any channel, but only respond to owner):

```
/engram yolo list       # list all channels with active yolo + expiry
/engram yolo off <name> # immediate revoke
/engram yolo extend <name> 6h  # extend expiry
```

CLI counterparts:
```
engram yolo list
engram yolo off <channel-id>
engram yolo extend <channel-id> 6h
```

## File-by-File Impact Map

### `src/engram/manifest.py`
- **Add:** `PermissionTier(StrEnum)` with values `task-assistant`, `owner-scoped`, `yolo`.
- **Add:** Field on `ChannelManifest`: `permission_tier: PermissionTier`, default `task-assistant`.
- **Add:** Field on `ChannelManifest`: `yolo_until: datetime | None`, default None. Pydantic timezone-aware.
- **Add:** `tier_effective()` method — returns current tier, auto-demoting to pre-yolo tier if `yolo_until` has passed.
- **Add:** `pre_yolo_tier: PermissionTier | None` field — remembers tier to revert to.
- **Add:** `ABSOLUTE_DENY_RULES` constant (copied from current `owner-dm.yaml` deny list, applied to all tiers).
- **Change:** `is_owner_dm()` kept but deprecated in favor of `tier == OWNER_SCOPED`. Leave as thin wrapper for now to avoid breaking egress.py sticky logic until fully migrated.
- **Change:** `add_allow_rule` — no functional change, but `_STICKY_INELIGIBLE_TOOLS` check moves to tier-check.
- **Tests touched:** `test_manifest.py`, `test_permissions.py`.

### `src/engram/templates/manifests/`
- **Rewrite:** `owner-dm.yaml` — set `permission_tier: owner-scoped` explicitly, remove `allow: [...]` (now derived from tier defaults in code).
- **Rewrite:** `task-assistant.yaml` — set `permission_tier: task-assistant`, bump `hitl.max_per_day: 1000`.
- **Add:** Internal `_TIER_DEFAULTS` constant in `manifest.py` — maps tier → allow list + deny list + hitl.max_per_day. Template YAML only declares the tier; everything else comes from code. (Avoids the template drift problem that caused `engram-self` to miss GRO-477's defaults.)

### `src/engram/bootstrap.py`
- **Change:** `apply_manifest_migrations` — extends current GRO-477 migration. For each manifest on load:
  - If no `permission_tier` field: set to `task-assistant` if identity=task-assistant, `owner-scoped` if identity=owner-dm-full.
  - If tier is `owner-scoped` and `allow` is empty: populate from `_TIER_DEFAULTS`.
  - If `yolo_until` is in the past: clear yolo, revert to `pre_yolo_tier`. Log `channel.yolo_expired`.
- **Change:** `_render_manifest` — reads tier from template, applies `_TIER_DEFAULTS` at render time.

### `src/engram/config.py`
- **Change:** `HITLConfig.max_per_day` default 5 → 1000.
- **No change:** `owner_dm_channel_id` already exists; reused for upgrade approval DMs.

### `src/engram/hitl.py`
- **Change:** `HITLRateLimiter.check` — reads `max_per_day` from manifest's tier defaults if not set explicitly. No semantic change; just higher ceilings.
- **No change:** core rate-limiter logic stays.

### `src/engram/egress.py`
- **Change:** `_is_sticky_eligible` — switch from `channel_manifest.is_owner_dm()` to `channel_manifest.tier_effective() == OWNER_SCOPED`. Also returns False for any tool+input that matches a footgun pattern.
- **Add:** `post_upgrade_request_dm` — posts the approval card in owner-DM. Mirrors `_build_hitl_buttons` pattern.
- **Add:** `post_upgrade_result_in_channel` — edits the "⏳ waiting" message to "✅/❌" after approval decision.
- **Add:** `post_yolo_expired_notification` — posts to owner-DM when lazy-check detects expiry.
- **Add:** `post_footgun_confirmation_card` — posts the type-to-confirm card. Uses Slack `plain_text_input` block.
- **Add:** `@app.view("footgun_confirm_submit")` — view-submission handler. Validates typed string matches `CONFIRM`, resolves the pending permission with allow/deny.

### `src/engram/ingress.py` — NEW SLASH COMMANDS
- **Add:** `@app.command("/engram")` — dispatches on first arg. Subcommands: `upgrade`, `yolo`. Mirrors `/exclude-from-nightly` pattern.
  - `/engram upgrade <tier> [reason...]` — creates upgrade request, posts to owner-DM.
  - `/engram yolo list` — owner-only, lists active yolo channels with expiry + buttons.
  - `/engram yolo off <channel>` — owner-only, immediate revoke.
  - `/engram yolo extend <channel> <duration>` — owner-only.
- **Add:** `@app.action("upgrade_decision_*")` handler — mirrors `@app.action(PENDING_CHANNEL_ACTION_ID_PATTERN)`. Validates the clicking user is owner, applies tier change, edits messages.
- **Add:** `@app.action("footgun_confirm_open_*")` → opens the type-to-confirm modal.
- **Add:** Lazy yolo expiry check — in the main message handler, before dispatching to agent, call `manifest.tier_effective()`. If it returned a demoted tier, persist the demotion + fire the DM notification.

### `src/engram/footguns.py` — NEW MODULE
- **Add:** `FOOTGUN_PATTERNS` — list of `(regex, description)` tuples.
- **Add:** `match_footgun(tool_name: str, tool_input: dict) -> FootgunMatch | None` — returns match details or None.
- **Add:** Pre-tool-use hook wires through this for Bash/BashOutput invocations. Hook returns PermissionResultAsk with a special marker that egress.py recognizes as "render confirm modal."
- **Tests:** `tests/test_footguns.py` — pattern coverage table-driven; negative cases too (`rm -f single-file.txt` should NOT match, `git push --force origin feature-branch` should NOT match if branch pattern is personal-only).

### `src/engram/agent.py`
- **Change:** `permission_mode` resolution — reads from `manifest.tier_effective()`:
  - `task-assistant` → `"default"`
  - `owner-scoped` → `"default"`
  - `yolo` → `"bypassPermissions"`
- **No change:** allow/deny rules continue to flow through existing path.

### `src/engram/cli_channels.py` — NEW COMMANDS
- **Add:** `engram channels upgrade <channel-id> <tier> [--until DURATION]`
- **Add:** `engram channels tier <channel-id>` — show current tier, yolo status, expiry.
- **No change:** existing `approve`, `deny`, `reset` stay.

### `src/engram/cli.py` — NEW SUBGROUP
- **Add:** `engram yolo list|off|extend` subgroup. Reuses logic from in-Slack commands.

### `src/engram/nightly/` or new `src/engram/scheduled.py`
- **Add:** Daily sweep job — finds all manifests with `yolo_until` in the past that weren't caught by lazy check (because channel had no activity). Fires demotion + DM notification. Runs as part of existing nightly pipeline (adds ~100ms to a job that already takes minutes). Alternative: separate launchd job. Decision: piggyback on nightly for simplicity.

### `docs/hitl.md`
- **Rewrite:** Update daily-cap section to reflect 1000 default. Add tier table.
- **Add:** Section on upgrade flow and YOLO.

### `docs/permission-tiers.md` (new)
- User-facing explainer: tier comparison table, how to request upgrades, how owner approves, how yolo works.

### Tests
- **New:** `tests/test_tiers.py` — tier resolution, yolo expiry logic, `_TIER_DEFAULTS` correctness.
- **New:** `tests/test_upgrade_flow.py` — slash command parsing, approval DM posting, button handler, in-channel message edit.
- **New:** `tests/test_yolo.py` — 24h time-box, lazy expiry check, slash commands, DM notifications.
- **Update:** `test_manifest.py`, `test_egress.py`, `test_permissions.py`, `test_hitl.py` for new tier field.
- **Update:** `test_ingress_hitl.py` for new slash command dispatch.
- **Update:** `test_cli_channels.py` for new CLI verbs.

## Dependency Chain

```
Story 1 (foundation, no user-visible behavior change)
  ├─> Story 2 (/engram upgrade + approval DM)
  ├─> Story 3 (YOLO time-box + lazy expiry)
  │     └─> Story 4 (yolo list/off/extend UX)
  └─> Story 5 (footgun detection + confirm-modal)
```

Story 1 is the keystone. S2, S3, and S5 can run in parallel once S1 lands. S4 depends on S3. Eric's `engram-self` channel gets fixed by him running `/engram upgrade owner-scoped` once S2 is live.

## Story Breakdown (to file in Linear)

### Story 1: Permission tier foundation
**Size:** ~600 LOC production + ~400 LOC tests
**Touches:** `manifest.py`, `bootstrap.py`, `config.py`, templates, `agent.py`, `egress.py` (1-line sticky change), `hitl.py` (ceiling raise), `docs/hitl.md`, 6 test files.

**Acceptance:**
- `ChannelManifest` has `permission_tier`, `yolo_until`, `pre_yolo_tier` fields.
- `_TIER_DEFAULTS` constant owns per-tier allow lists + hitl caps; templates just declare the tier.
- Existing `engram-self` manifest migrates on load to `task-assistant` tier (no behavior change yet).
- Existing owner-DM manifest migrates to `owner-scoped` tier (no behavior change — still has same defaults).
- All existing tests pass. New tier tests cover migration idempotency.
- `is_owner_dm()` kept as deprecated thin wrapper; `_is_sticky_eligible` uses tier.

**No user-facing change in this story.** It's pure refactor. Ship it, verify nothing broke, then build the UX on top.

### Story 2: `/engram upgrade` slash command + approval DM
**Size:** ~400 LOC + ~250 LOC tests
**Depends on:** Story 1
**Touches:** `ingress.py` (new slash command + new action handler), `egress.py` (3 new post functions), `cli_channels.py` (`upgrade` verb), `docs/permission-tiers.md` (new), tests.

**Acceptance:**
- `/engram upgrade owner-scoped [reason]` in any channel → posts in-channel waiting message + DM to owner with 3 buttons.
- Owner clicks Approve → tier updated, both messages edited.
- Non-owner clicks any button → ephemeral "Only the owner can approve upgrades."
- `engram channels upgrade <id> <tier>` CLI works standalone.
- Linear audit: upgrade events logged with who-requested, who-approved, previous tier, new tier.

### Story 3: YOLO time-box + lazy expiry
**Size:** ~250 LOC + ~200 LOC tests
**Depends on:** Story 1
**Touches:** `manifest.py` (`tier_effective` full logic), `ingress.py` (lazy check on each turn), `egress.py` (`post_yolo_expired_notification`), `nightly/` (sweep job), tests.

**Acceptance:**
- `/engram upgrade yolo` with approval → sets `permission_tier=yolo`, `yolo_until=now+24h`, `pre_yolo_tier=<current>`.
- Subsequent turn in channel after expiry → lazy check fires, demotes, DM notifies.
- Channel idle for 48h with expired yolo → nightly sweep fires demotion + DM.
- Demotion event logged with duration-used, pre-yolo-tier.

### Story 4: YOLO management UX
**Size:** ~200 LOC + ~150 LOC tests
**Depends on:** Story 3
**Touches:** `ingress.py` (list/off/extend slash commands + action handlers), `cli.py` (`yolo` CLI subgroup), `egress.py` (list-rendering), tests.

**Acceptance:**
- `/engram yolo list` → owner-DM gets a list of active-yolo channels with expiry, each with `[Extend 6h]` `[Revoke]` buttons.
- `/engram yolo off <channel>` → immediate revoke, channel and DM both get notified.
- `/engram yolo extend <channel> 6h` → extends expiry, DM-confirms.
- Non-owner runs any `/engram yolo *` subcommand → ephemeral "Owner-only."
- CLI equivalents: `engram yolo list|off|extend`.

### Story 5: Footgun pattern detection + type-to-confirm modal
**Size:** ~350 LOC + ~250 LOC tests
**Depends on:** Story 1
**Touches:** new `src/engram/footguns.py`, `ingress.py` (view submission handler + action to open modal), `egress.py` (confirm card + modal block construction), `agent.py` (pre-tool-use hook integration), tests.

**Acceptance:**
- `FOOTGUN_PATTERNS` covers rm-rf, sudo, curl-pipe-sh, dd, mkfs, chmod-777, dev/s[da-z], drop-table, force-push-to-protected (see tier table).
- In `task-assistant` tier: footgun match → flat deny. Logged as `footgun.denied_task_assistant`.
- In `owner-scoped` or `yolo` tier: footgun match → confirm-modal with typed-confirmation input.
- Owner types `CONFIRM` exactly → command runs. Anything else → denied.
- Non-owner clicks the Open-Modal button → ephemeral "Owner approval required for destructive actions."
- Modal timeout (5 min) → deny + interrupt, same as HITL timeout.
- Sticky "Always allow" is never offered for footgun matches, even owner-scoped.
- No false positives for `rm -f one-file.txt`, `git push` to personal feature branch, `curl https://api.example.com/data` (no pipe to shell).

**Why this is a separate story, not part of S3:** YOLO's escalation flow is only meaningful if footgun detection exists. But footgun detection is independently valuable — in `owner-scoped` today, an agent that decides to `rm -rf` something should still hit a confirmation. Shipping this as a standalone capability, tier-aware at the apply step, keeps the commit focused.

### ~~Story 5 (old): Immediate hotfix for `engram-self`~~

**Cancelled per Eric's decision on Q3:** Ship the feature properly; no hand-edit. Once S1+S2 land, Eric runs `/engram upgrade owner-scoped` in `engram-self` and approves in DM. That channel then joins the self-improvement loop naturally (meta_eligible flips to true when tier becomes owner-scoped).

## Risk & Open Questions

**Risk 1: Tier field on existing manifests.** The migration in Story 1 must be idempotent and lossless. Every existing manifest needs a correct tier assignment. Mitigation: migration tests with fixture manifests from both templates + hand-edited variants.

**Risk 2: Slash command dispatch.** `/engram` as a multi-arg dispatcher is new; existing commands are single-purpose (`/exclude-from-nightly`). Could alternatively register separate `/engram-upgrade`, `/engram-yolo` commands for simpler routing. Decision deferred to Story 2 implementation.

**Risk 3: Owner identification.** `config.owner_user_id` exists for nightly reports; needs to be checked at approval-button-click time. If not set, the system can't validate. Mitigation: `engram doctor` check for `owner_user_id` + `owner_dm_channel_id` both being set.

**Risk 4: Absolute deny list scope.** Current `owner-dm.yaml` deny list is narrow (only credentials). If we apply the narrow list to yolo mode, we trust yolo too much. Proposal: yolo uses owner-DM's deny list *plus* a safety net: `Bash(sudo *)`, `Bash(rm -rf *)`, or similar. Open question: should yolo block destructive-looking shell patterns by default? My vote: yes, conservative defaults, user can `/engram upgrade yolo-nuclear` if they really want.

**Risk 5: Multiple concurrent upgrade requests.** What if Eric makes two requests before approving either? v1: second request in same channel replaces first. Old approval card edited to "Superseded by newer request."

**Risk 6: Symphony interaction.** Symphony workflows dispatch Codex to Engram's repo. We don't want Symphony stories to accidentally test YOLO behavior on a live channel. Mitigation: test infrastructure uses in-memory manifest fixtures, never touches real contexts dir. Already the pattern; no new risk.

## Decisions Still Needed From Eric

_All resolved. See "Decisions" at top of doc._

## Timeline Estimate

- **Story 1:** ~3 hours Symphony wall time
- **Story 2:** depends on 1. ~2-3 hours.
- **Story 3:** depends on 1. Runs parallel with 2. ~2 hours.
- **Story 4:** depends on 3. ~2 hours.
- **Story 5:** depends on 1. Runs parallel with 2/3. ~2.5 hours.

With `max_concurrent=2`, critical path is S1 → (S2|S3|S5 pick 2 at a time) → S4. Wall time estimate: **~8-10 hours** if queue stays clean. Kicks off tonight, lands mid-Friday.
