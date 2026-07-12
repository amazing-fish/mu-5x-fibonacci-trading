---
feature_ids: [R0]
topics: [research-provenance, strategy-release, execution-roadmap]
doc_kind: design
created: 2026-07-12
issue: 45
---

# Reproducible experiment and strategy-release provenance design

## Why this blocks 30-B

The current staged observation records `strategy_name` and a configuration hash, but those two values do not prove which code produced the decision, which trusted dataset supported the strategy, which evaluation windows and execution assumptions were used, or who reviewed the selection. The current `baseline` selector is a name returned from code, while preferred parameters reference ignored report paths. A later `strategy_release_id` addition would change the v1 `OrderIntent` fingerprint and persisted schema.

R0 therefore establishes the minimum immutable release reference that 30-B can bind on day one. It does not decide whether an order may be sent and does not add any Broker path.

## Goals

- Make experiment evidence reproducible from pinned trusted data, code, configuration, windows, assumptions, and deterministic result summaries.
- Separate stable rule identity from exact release/evidence identity.
- Represent review as a detached statement over one exact candidate fingerprint, avoiding a circular “the review URL changes the thing being reviewed” contract.
- Let a strict resolver return an explicitly requested approved release by ID; no mutable “current release” pointer or strategy-name fallback is accepted.
- Produce one MU baseline candidate from repository-tracked trusted data and support promotion only after independent review of its exact candidate fingerprint.

## Non-goals

- No `OrderIntent`, intent factory, signal lineage, business action, confirmation, authorization, ledger, idempotency, or Broker adapter.
- No strategy parameter search, strategy behavior change, trusted refresh, account read, or Broker mutation.
- No automatic performance threshold or automatic strategy approval.
- No claim that a Markdown/HTML report path is authoritative evidence.

## Options considered

### A. Content-addressed release manifest before 30-B — selected

Create a strict candidate/evidence contract, a detached approval statement, and a content-addressed approved release. 30-B receives an explicit release ID and can freeze its schema without embedding the entire research record.

Tradeoff: adds one prerequisite PR, but removes a predictable v1 migration and creates one narrow boundary between research selection and execution.

### B. Put all experiment provenance directly in `OrderIntent`

This would make each intent self-contained, but it duplicates large evidence records, couples execution storage to research schema evolution, and makes every new evidence field an intent migration.

### C. Ship 30-B with name/config only and add v2 later

This is the smallest immediate patch but knowingly freezes an incomplete identity. It would allow the same name/config pair to refer to changed code or unreviewed evidence and requires a breaking migration before 30-C can safely reserve actions.

## Identity model

Three identities remain deliberately separate:

| Identity | Stable across | Changes when |
|---|---|---|
| `strategy_rule_id` | data generations, experiment reruns, code refactors that preserve rule semantics, and approval events | the semantic trading rule intentionally versions |
| `strategy_config_sha256` | presentation and evidence changes | any executable configuration value changes |
| `strategy_release_id` | presentation labels and local paths only | code, config, pinned data, windows, assumptions, results, selection, or approval evidence changes |

The strategy registry is the authoritative owner of rule identity. `StrategyGroup` gains a frozen `StrategyRuleDescriptor` with `strategy_rule_id`, strategy name, semantic version, and supported execution shape. Candidate generation resolves this descriptor from the registry and never accepts an arbitrary rule-ID string. Registry construction rejects duplicate IDs/aliases. The initial descriptor owns `mu.baseline.second_pullback.long_limit.v1`; any semantic rule change must intentionally bump it and is guarded by catalog/static contract tests. The first candidate is scoped to `MU-USDT-SWAP`; other symbols remain observation-only until they have an approved release.

30-B will derive `signal_lineage_id` from the stable rule ID, configuration hash, symbol, and signal time. It will bind `strategy_release_id` into the exact intent fingerprint. This keeps a later data/evidence release from minting a different signal lineage while still creating a different intent revision.

## Contracts

### Canonical primitives

A small shared `mu_strategy.canonical` module owns sorted-key, compact, ASCII JSON serialization with `allow_nan=False` and SHA-256 helpers. Stage 0 moves to this helper with byte-for-byte regression coverage so canonical policy is not duplicated across contract modules.

Hashes use lowercase full hex. Git commit identity is a lowercase 40-character SHA-1 because the current repository uses SHA-1 object IDs. Control-relevant numeric experiment values use canonical decimal strings; booleans and non-finite values are rejected.

### `StrategyReleaseCandidate`

The frozen v1 candidate contains:

- `schema_version`;
- `candidate_fingerprint`;
- registry-derived `strategy_rule_id`, `strategy_name`, and `supported_symbols`;
- a frozen exact-field `strategy_config_payload_v1` containing every executable configuration field with canonical tuple/decimal encoding, plus `strategy_config_sha256` derived only from that payload;
- explicit `evaluated_code_commit_sha` for the one clean commit containing the evaluated strategy, registry/config canonicalizer, backtest engine, and candidate runner;
- one `TrustedExperimentDataset` with `run_id`, symbol, requested/effective intervals, and sorted interval/content hashes;
- exactly three contiguous end-exclusive windows: `TRAIN`, `VALIDATION`, and `OUT_OF_SAMPLE`;
- a closed `experiment_protocol_id` and version;
- typed `BacktestAssumptions` containing starting equity, fee profile/rate, fill model, slippage, and partial-fill model;
- deterministic per-window result summaries using canonical decimal strings, closed undefined-value tags, and integer trade counts;
- a closed selection reason code and the canonical result fingerprint.

Candidate identity includes every item above except its own fingerprint and presentation-only labels/paths.

Candidate construction and parsing also enforce the closed protocol's offline-checkable cross-field invariants before fingerprints are considered sufficient:

| Candidate state | Behavior |
|---|---|
| any nested schema version is a boolean, non-integer, or not the exact supported version | reject |
| config or assumptions `fee_profile` is outside the closed `market` / `limit` domain | reject |
| assumptions `fee_profile` or `fee_rate` differs from the frozen strategy config | reject |
| explicit `slippage_bps` is non-zero | reject |
| effective pinned dataset omits protocol-required `15m` / `1h`, or violates schema-v3's native-interval requirement for `5m` | reject |
| any cold-start window result has a different `starting_equity` from the assumptions | reject |
| result return differs from `ending_equity / starting_equity - 1` beyond the declared decimal tolerance | reject |
| result net equity change differs from `gross_profit - gross_loss` beyond the declared decimal tolerance | reject |
| zero-trade result reports P&L, return, equity change, or drawdown; or directional P&L requires more trades than reported | reject |
| fill or partial-fill model is outside the closed v1 enum | reject during typed parsing |

The experiment runner and candidate parser share the same assumption validator. Result arithmetic allows only a `0.00000000001` absolute tolerance for independently quantized 12-decimal float outputs; this covers the bounded rounding difference in the current canonical candidate without permitting economically material contradictions. Recomputing per-result, aggregate-result, config, and candidate fingerprints cannot turn evidence that violates these relationships into a valid candidate. Relationships that require candle/trade replay remain promotion-review evidence rather than being guessed offline.

R0's v1 release domain is deliberately closed to the first approved baseline rather than pretending every registry strategy already has release evidence:

| Rule/config relationship | v1 requirement |
|---|---|
| `strategy_rule_id` / `strategy_name` | `mu.baseline.second_pullback.long_limit.v1` / `baseline` |
| `supported_symbols` and dataset/config symbol | exactly `MU-USDT-SWAP` |
| entry semantic discriminator | `entry_execution: second_pullback` |
| base exit semantic discriminator | `stop_tightening: baseline` |
| regime-specific exit overrides | `yellow_stop_tightening: null`, `green_stop_tightening: null` |

Other executable parameters remain config-addressed and may change only by producing a different candidate and independent approval; they are not frozen to one global hash. Supporting another semantic rule or symbol requires an explicit versioned release-domain binding rather than changing these v1 meanings in place.

### `StrategyReleaseApproval`

Approval is a detached statement over exactly one `candidate_fingerprint`. Its canonical evidence snapshot is embedded in the release and contains:

- closed decision: `APPROVED` or `REJECTED`;
- stable reviewer identity and immutable review record ID;
- review timestamp and exact canonical statement bytes;
- SCM repository, pull-request/review coordinates, and the captured review-record snapshot;
- the reviewed `candidate_fingerprint` and `evaluated_code_commit_sha`;
- SHA-256 recomputed locally from those canonical evidence bytes.

The approval statement cannot approve a different candidate or implementation commit, omit the reviewer/record/time/reference/statement, or use a free-text status. Integrity and authenticity are separate boundaries:

- the promotion gate queries or otherwise verifies the trusted live SCM PR/review record, proves that its reviewer is independent from the PR author and the evaluated commit's mapped author/committer identities, rejects any non-null review `lastEditedAt` or creation-edit flag, checks the exact candidate fingerprint and implementation commit, then captures its coordinates, canonical snapshot, and hash; missing commit identity fails closed;
- the runtime reader parses the embedded snapshot, recomputes its digest, and verifies every duplicated identity, but does not claim that a bare digest cryptographically authenticates a reviewer;
- authenticity comes from repository review/merge provenance at promotion time. Offline cryptographic signatures and a trusted-key infrastructure are intentionally outside R0.

A reviewer can therefore approve the candidate without predicting the final release ID, while the release retains a tamper-evident snapshot of the SCM evidence used by the promotion gate.

The GitHub GraphQL review response is a strict required-nullable contract. Promotion handles every edit-provenance state as follows:

| Live response state | Promotion behavior |
|---|---|
| top-level `errors` is present and non-empty | reject the response |
| `data.node` is missing or is not an object | reject the response |
| `includesCreatedEdit` is missing or is not a boolean | reject the response |
| `lastEditedAt` is missing | reject the response |
| `includesCreatedEdit: false` and `lastEditedAt: null` | continue through the remaining independent-review gates |
| `includesCreatedEdit: true` | reject the review as edited during creation |
| `lastEditedAt` is a valid timestamp | reject the review as edited after creation |
| `lastEditedAt` is malformed or has the wrong type | reject the response |

This distinction is deliberate: explicit `null` is positive live evidence that GitHub reports no later edit, while an omitted field is incomplete provenance and therefore fails closed.

The offline snapshot is not a signature, but it is still a closed repository-specific contract rather than an arbitrary self-authored envelope. Construction and runtime parsing reject any snapshot that violates an offline-checkable promotion invariant:

| Embedded snapshot state | Offline behavior |
|---|---|
| provider is not `github` or repository is not `amazing-fish/mu-5x-fibonacci-trading` | reject |
| review record ID is not a positive GitHub numeric database ID | reject |
| review URL does not exactly bind the trusted repository, PR number, and review record ID | reject |
| reviewer equals the captured evaluated-commit author, compared case-insensitively | reject |
| statement is not the canonical three-line approval for the embedded candidate fingerprint and evaluated commit | reject |
| duplicated decision, candidate, implementation, snapshot digest, or release content address differs | reject |

The live promotion query remains the authenticity boundary for PR-author and evaluated-commit committer independence, edit history, review state, and record existence. The offline checks prevent a self-consistent artifact from changing the trusted coordinates or the evidence fields that v1 already embeds; they do not claim to replace that live verification.

### `StrategyRelease`

The release contains the full candidate and approval plus `strategy_release_id = "sr1_" + SHA256(canonical candidate + canonical approval)`. The release constructor requires matching candidate fingerprints and an `APPROVED` decision. Rejected evidence can be stored as review evidence but cannot construct an execution-eligible release.

Presentation notes and local report paths are diagnostics outside the identity-bearing payload. Content hashes and review references, not prose, are authoritative.

### `ExperimentProtocol` v1

The initial closed protocol ID is `mu.baseline.walk_forward.cold_start.v1`. It freezes behavior currently used by the repository instead of leaving runner semantics implicit:

- each TRAIN/VALIDATION/OOS evaluation window is `[start_ms, end_ms)` and the three windows are contiguous and ordered;
- `input_start_ms == start_ms`: each split deliberately cold-starts indicators and strategy state, matching the current independent-window walk-forward behavior;
- only complete 1h source candles contained by the split are eligible, and their regime state becomes visible at candle close, never at candle open; pre-window partial hours and future closes are excluded;
- each window requires the exact gap-free set of `15m` opens at 900,000 ms steps and `1h` source opens at 3,600,000 ms steps; late starts, early ends, interior gaps, duplicates, or partial-step boundaries fail before context construction;
- no position, pending signal, indicator state, or equity carries across a split;
- only candles inside the split can create entries;
- any open position at the split end is force-closed using the existing deterministic `end_of_data` rule;
- the protocol binds exact starting equity and complete fee/fill/slippage/partial-fill assumptions;
- result fields and decimal quantization are fixed: trade count, starting/ending equity, gross profit/loss, total return, and maximum drawdown; profit factor is derived outside identity, so a zero-loss window never needs JSON Infinity;
- every summary is calculated from the exact pinned candle set and canonicalized before fingerprinting.

A future warm-up or carry policy requires a new protocol ID; it cannot silently reinterpret a v1 candidate.

### Historical pinned-generation reader

Candidate reruns use a narrow explicit-generation reader, not `current.json` and not the trading-time cache loader. The reader accepts an exact generation directory/run ID, validates schema v3 plus directory/manifest identity, requested/effective interval completeness, published usability/integrity, source-file containment, and every CSV content SHA-256. It does not consult or change the current pointer, recalculate historical eligibility from today’s wall-clock freshness, refresh data, or access the network. Historical freshness remains the published fact in the pinned manifest; content and integrity are revalidated locally.

## Artifact and resolver boundary

- Candidate artifacts are generated under ignored `data/strategy-release-candidates/` and are never automatically promoted.
- Approved immutable releases are tracked as `config/strategy-releases/<strategy_release_id>.json`.
- There is no mutable `current.json` release pointer in v1.
- `StrictStrategyReleaseResolver.resolve(strategy_release_id, *, expected_rule_id, expected_symbol)` requires both expectations, rejects IDs before path construction unless they match `sr1_[0-9a-f]{64}`, reads the exact file, and validates exact fields/schema/content address/approval/rule/symbol/config provenance.
- Missing, corrupt, draft, rejected, mismatched, or unknown artifacts raise a typed fail-closed error.
- The legacy `current_mu_strategy_name()` remains a compatibility/research helper and is explicitly forbidden as a staged execution release resolver.

30-B must receive an explicit approved release ID in configuration or its factory input. It cannot search by name, choose the newest file, or derive approval from registry membership.

## Candidate generation

A narrow offline command/function builds the MU baseline candidate through a two-phase, exact-code workflow:

- tracked trusted generation `e702be27d2de4b2d92b12bf01c70d02d` and its manifest hashes;
- a clean implementation commit created after the contracts, authoritative registry descriptor/config canonicalizer, historical reader, and experiment runner exist; `39aa1e2ed8bb57a32f13a302972de00d36c115a0` is only its roadmap/base lineage, not the evaluated-code claim;
- the canonical baseline descriptor and full frozen config payload for `MU-USDT-SWAP`, verified field-for-field against the registered baseline at that exact commit;
- explicit contiguous train/validation/OOS window boundaries within the pinned 15m/1h history;
- the closed v1 experiment protocol and assumptions matching the repository’s deterministic OHLC behavior.

Phase 1 commits contracts plus the runner. Candidate generation then runs only from a clean worktree whose `HEAD` exactly equals the explicit `evaluated_code_commit_sha`, or inside an isolated checkout at that exact SHA. Ancestor-only validation is forbidden. At generation time the command also resolves the authoritative registry descriptor and compares the complete canonical config payload with that exact checkout. The generator reads only the explicit tracked generation, performs no current-pointer read, refresh, network, or private API call, and writes only the ignored candidate artifact selected by the caller.

An independent reviewer examines the candidate fingerprint, exact evaluated commit, config payload, inputs, protocol, window results, and assumptions. The promotion gate verifies that live independent SCM review and captures its canonical evidence snapshot. Phase 2 combines the unchanged candidate and snapshot into the tracked approved release in a later commit. A final code review covers the release artifact and resolver tests. Any candidate, implementation-commit, or approval-evidence change invalidates the release identity.

## Validation and failure semantics

Candidate generation rejects:

- dirty worktrees, `HEAD != evaluated_code_commit_sha`, or ancestor-only/implicit code substitution;
- free-form/unregistered rule IDs, duplicate registry identities, or a canonical config payload that differs from the exact-HEAD registered baseline;
- pinned generation/path/manifest/hash mismatch or any attempt to use `current.json`, wall-clock freshness, refresh, or network fallback.

The promotion gate rejects a missing/non-independent live SCM reviewer, a review record that does not name the exact candidate and implementation commit, or a captured snapshot/hash that differs from the live record.

Release construction, reading, and resolution reject:

- unknown/missing/extra schema fields or enum values;
- invalid/duplicate hashes, IDs, intervals, or symbols;
- a self-contained config payload whose derived hash or candidate binding does not match;
- missing or non-contiguous TRAIN/VALIDATION/OOS windows, overlap, reversed/empty ranges, or data-window overflow;
- unknown experiment protocol, protocol/result mismatch, non-canonical decimal values, or invalid closed undefined tags;
- result fingerprints that do not match canonical summaries;
- approval for another candidate/commit, missing or non-recomputable canonical approval snapshot, or non-approved decision;
- release path/ID/content mismatch;
- attempts to resolve by strategy name or “latest” file.

Later construction/read/resolver paths never compare `evaluated_code_commit_sha` with the current checkout, consult the current registry, or run Git history. They validate the stored self-contained payload, fingerprints, approval binding, and caller-supplied expected rule/symbol. This permits a release created in Phase 2 to resolve from later commits without substituting current code for evaluated code.

A failed candidate write creates no approved release. A corrupt approved artifact is never partially returned. Rollback removes only the R0 contract and release artifacts; it does not modify trusted generations, observations, or Broker state.

## Compatibility and safety

- Stage 0 observation and legacy JSON shapes remain byte-compatible.
- No existing strategy decision or parameter changes.
- No private credentials or account context are needed.
- No leverage, submit, cancel, trusted refresh, or canonical data publication method is reachable.
- Approval means “eligible for future staged dry-run intent construction,” not authorization to mutate a Broker.

## Test strategy

Focused tests cover:

- candidate/approval/release canonical round trips and restart reads;
- full mutation matrices for code/config/data/windows/assumptions/results/approval;
- exact-field, enum, ID, hash, decimal, timestamp, split, and content-address checks;
- authoritative rule-descriptor uniqueness/alias/version tests and full config-payload round trips;
- strict approved resolver with required rule/symbol expectations and every fail-closed artifact/status/mismatch path;
- promotion-gate tests using an injected SCM review provider, including author/reviewer equality, edited-record snapshot mismatch, and candidate/implementation mismatch; runtime tests separately prove only approval-snapshot integrity;
- exact-HEAD/clean-tree generation enforcement and two-phase artifact construction;
- explicit-generation historical reads after the current pointer changes and after wall-clock freshness expires;
- protocol rerun equality, split boundary/cold-start/no-carry/end-of-data behavior, decimal quantization, and zero-loss metric representation;
- stable rule identity across data/release revisions;
- byte-for-byte Stage 0 canonical compatibility after sharing the helper;
- deterministic MU candidate generation from tracked data;
- patches proving no refresh, network, private API, leverage, submit, or cancel call;
- full repository regression, compileall, and diff check.

## 30-B handoff

After R0 merges, 30-B can freeze:

- `signal_lineage_id = hash(strategy_rule_id, strategy_config_sha256, symbol, signal_time_ms)`;
- `business_action_id = hash(environment, SUBMIT_ENTRY, signal_lineage_id)`;
- exact intent fingerprint including `strategy_release_id`, trusted generation/hashes, decision, exact sizing, and expiry;
- expiry at the existing exclusive signal boundary `signal_time_ms + second_pullback_wait_bars * 15m`;
- public-only instrument sizing through exact `tickSz`, `lotSz`, and `ctVal` values.

R0 does not implement any of those execution-domain types.
