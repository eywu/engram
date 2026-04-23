# 06 - Self-Improvement Loop

Engram V3 ships the self-improvement loop as an offline nightly memory pipeline. It does
not run the old Hindsight daemon, does not watch tmux sessions, and does not mutate live
agent instructions during a Slack turn.

## Shipped V3 Loop

1. Live Slack traffic is routed to one Claude SDK client per channel. Each channel has
   its own context directory and serialized turn lock.
2. Memory hooks ingest Claude JSONL transcript rows into `~/.engram/memory.db`.
   SQLite FTS5 provides keyword recall, and embeddings provide semantic recall when
   configured.
3. The nightly job harvests the previous UTC day from `memory.db`, grouped by channel.
   It applies the configured excluded-channel deny-list, evidence threshold,
   deterministic deduplication, and token cap before writing `harvest.json`.
4. Nightly synthesis runs Claude once per harvested channel with HITL disabled,
   `permission_mode="dontAsk"`, and the same memory-search deny-list that live channels
   use. The validator requires the JSON schema before `synthesis.json` is accepted.
   A malformed first response produces `nightly.parse_retry`; a malformed retry produces
   `nightly.parse_fail_final` with raw outputs in the nightly log.
5. Nightly apply upserts one durable summary per `(channel_id, day, trigger)` and flushes
   summary embeddings inline. Re-running the same day overwrites the previous nightly row
   instead of duplicating it.
6. The report phase writes `archive/<date>/report.md` and, unless suppressed, sends the
   owner DM with channel count, flag count, aggregate cost, and the report path.

Weekly mode is a second pass layered on top of daily summaries. It first completes the
daily run for the target date, then harvests exactly seven daily nightly rows per
eligible channel and writes `trigger="nightly-weekly"` summaries. Ineligible channels are
excluded from the weekly meta prompt and from the meta channel's memory-search server.

## Isolation Contract

Excluded channels are filtered at the data layer before the model sees rows. The same
deny-list is applied to:

- daily harvest input,
- weekly meta-synthesis input,
- live `memory_search(scope="this_channel")`, and
- live `memory_search(scope="all_channels")`.

The isolation canary for M5.8 is a synthetic transcript row in an excluded channel with a
unique `[ISOLATION-CANARY-<uuid>]` tag. A valid nightly run produces zero matches for that
tag across generated nightly artifacts.

Memory search uses independent SQLite connections and WAL mode. Live readers must see the
last committed state while nightly apply is writing, return no SQLite `BUSY` errors under
concurrent access, and never expose uncommitted summary text.

## Harvest Rules

Harvest is deterministic and local-only:

- time window: one UTC day for daily harvest,
- grouping: per channel,
- exclusions: `nightly.excluded_channels`,
- deduplication: Jaccard word-overlap threshold from `nightly.dedup_overlap`,
- evidence gate: `nightly.min_evidence`,
- channel cap: newest rows retained within `nightly.max_tokens_per_channel`, and
- output: `<nightly-root>/<date>/harvest.json`.

Weekly harvest reads only prior daily summaries with `trigger="nightly"`. It requires all
seven days in the window and does not compound over previous weekly summaries.

## Rule-Apply Deferral

Per AD-2 option a, rule extraction and rule application are deferred out of the M5
nightly apply path. The shipped apply phase only writes summaries and embeddings. It does
not edit `CLAUDE.md`, channel manifests, skill files, or runtime guardrails.

Future rule-apply work should be a separate human-reviewed architecture with explicit
candidate records, provenance, review state, conflict handling, rollback behavior, and
channel-scoped application. Until that lands, nightly synthesis may surface action items
or open questions in `report.md`, but those are operator-facing report content rather than
automatic instruction changes.

## Removed V0/V2 Assumptions

- No Hindsight daemon.
- No TypeScript tmux orchestration.
- No live prompt mutation during a channel turn.
- No background process reading unrestricted cross-channel transcripts.
- No monthly compounding in the shipped pipeline.
- No automatic rule application from nightly summaries.

## M5+1 Follow-Up Stories

These are the follow-up story stubs carried out of M5.8:

- Rule-apply architecture: design the deferred AD-2 option-a path for reviewed rule
  candidates, approval, application, and rollback.
- Monthly compounding: decide whether a monthly layer is needed after daily plus weekly
  summaries have real operator usage data.
- Dedup threshold retune: rerun calibration after the soak and adjust
  `nightly.dedup_overlap` only if measured duplicate or drop rates justify it.
