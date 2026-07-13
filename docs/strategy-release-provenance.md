---
feature_ids: [R0, issue-45, issue-49]
topics: [strategy-release, provenance, reproducibility, durable-publication, trading-safety]
doc_kind: spec
created: 2026-07-12
---

# Strategy release provenance

R0 separates a named research strategy from an execution-eligible strategy release. A registry name such as `baseline` is convenient for research, but it does not prove which rule, config, code, data, experiment, or approval was used. Staged execution must consume a content-addressed approved release instead.

## Identities

- `strategy_rule_id` is the registry-owned semantic rule identity. The MU baseline is `mu.baseline.second_pullback.long_limit.v1`.
- `strategy_config_sha256` binds every field in the frozen v1 `StrategyConfig` payload. Canonical serialization rejects missing, extra, non-canonical, and non-finite values.
- `candidate_fingerprint` binds the rule, full config, exact evaluated Git SHA, pinned trusted generation and interval hashes, experiment windows, assumptions, and result summaries.
- `strategy_release_id` is `sr1_` plus the SHA-256 of the unchanged candidate and a verified approval snapshot.

These identities are orthogonal. Registry membership is not approval, a config hash is not code provenance, and a positive backtest result is not broker authorization.

R0 v1 is closed to `mu.baseline.second_pullback.long_limit.v1` / `baseline` on `MU-USDT-SWAP`. Parsing requires `entry_execution=second_pullback`, base `stop_tightening=baseline`, and no yellow/green stop override, matching the registry-owned baseline semantic shape. Numeric settings such as lookbacks, tolerances, fees, and wait bars may still produce a different config-addressed candidate for independent review; they are not collapsed into the rule ID. A future strategy rule or symbol needs an explicit versioned release-domain binding rather than reusing this identity with different executable semantics.

## Historical experiment protocol

`mu.baseline.walk_forward.cold_start.v1` reads an explicit `data/live/generations/<run_id>/manifest.json`. It never reads `current.json`, recalculates historical freshness from wall-clock time, refreshes data, or calls a provider. The reader validates schema v3 identity, source containment, row counts, and the canonical content SHA-256 of every effective interval.

TRAIN, VALIDATION, and OUT_OF_SAMPLE are explicit contiguous `[start_ms, end_ms)` windows with `input_start_ms == start_ms`. Each window starts with fresh indicator, position, pending-signal, and equity state. Before context construction, its `15m` and source `1h` open times must exactly match the fixed-step expected sets; late starts, early ends, gaps, duplicates, and partial boundaries fail closed. Complete 1h candles become visible at close rather than open, so missing-hour carry, pre-window state, and future hourly closes cannot leak into the split. The existing deterministic OHLC backtest closes any remaining position with `end_of_data`. The candidate records starting equity, fee profile/rate, fill model, slippage, and partial-fill model; result summaries use canonical finite decimal strings. Runner and parser use one closed validator: fee profiles are restricted to the executable domain, config and assumption fees must match, slippage must be zero, schema-v3 effective evidence must close over `5m`/`15m`/`1h`, and every window result must use the declared starting equity. Result parsing verifies return/equity/P&L/zero-trade arithmetic within `0.00000000001`; nested schema versions require real integers. Recomputing fingerprints cannot legitimize a cross-field mismatch.

The first locally reviewed MU candidate uses:

- candidate fingerprint `e9eb5a07017565a1d62f21c453c7bbfb7bfa885c92c4b340c713332ecb63f648`;
- evaluated implementation SHA `b92985b2e9709bcd95effb84e77e7975f916c620`;
- trusted generation `e702be27d2de4b2d92b12bf01c70d02d`;
- 7,048 / 2,352 / 2,348 `15m` candles in three hour-aligned cold-start windows;
- byte-identical repeated candidate output.

An independent local evidence review found no P0-P3 issue, but that verdict is deliberately not sufficient for promotion. There is no tracked approved release until a live immutable SCM review passes the promotion gate.

## Candidate, review, and promotion

Candidate generation requires a clean worktree whose `HEAD` exactly equals the caller-supplied `evaluated_code_commit_sha`. An ancestor match is insufficient. The command resolves the baseline rule and config from that checkout, reads only the explicit generation, runs the closed protocol, and atomically writes canonical JSON under ignored `data/strategy-release-candidates/`.

```powershell
python -m mu_strategy.commands.build_strategy_release_candidate `
  --run-id <run-id> --symbol MU-USDT-SWAP `
  --evaluated-code-commit-sha <exact-clean-head> `
  --train-start-ms <ms> --train-end-ms <ms> `
  --validation-end-ms <ms> --oos-end-ms <ms>
```

Promotion queries the trusted live PR, evaluated commit identity, and review record. The PR must contain the evaluated commit; the reviewer must differ from the PR author and the evaluated commit's mapped GitHub author and committer. Missing commit identity fails closed. The live decision must be `APPROVED`, and the review body must equal these canonical bytes with the exact candidate and evaluated SHA:

```text
APPROVED_STRATEGY_RELEASE_V1
candidate_fingerprint=<64 lowercase hex>
evaluated_code_commit_sha=<40 lowercase hex>
```

Missing, deleted, dismissed, self-authored, or mismatched review evidence fails closed. Promotion reads the authoritative GraphQL review node and rejects either `lastEditedAt != null` or `includesCreatedEdit=true`, so any edited summary cannot be presented as the original approval bytes. Only after live verification does promotion capture the SCM coordinates and canonical snapshot, construct the content-addressed release, and atomically write `config/strategy-releases/<strategy_release_id>.json`.

Authenticity and integrity remain separate. Promotion establishes authenticity by querying SCM and checking reviewer independence. Runtime does not contact SCM; it accepts only the closed `github` / `amazing-fish/mu-5x-fibonacci-trading` evidence contract, requires a positive numeric review ID and an exact URL that binds the repository, PR, and review record, rejects case-insensitive reviewer overlap with the captured evaluated-commit author, verifies the canonical three-line statement, and then checks the embedded snapshot digest and all duplicated bindings. A digest alone is not treated as proof of reviewer identity or live record existence.

## Durable publication and recovery

Candidate and approved-release writers share the strategy-domain publisher in `mu_strategy.research.strategy_artifact_publication`; neither command owns a private atomic-write clone. Publication is immutable and content-addressed: an identical artifact at the final path is an idempotent success, while different bytes at that path are a typed conflict and are never overwritten.

The publisher writes and file-syncs a unique temporary file, then creates and file-syncs `.<filename>.publication-pending`. It syncs the pending record's directory and every newly created parent-directory entry before atomically linking the complete temporary inode at the final path with no-overwrite semantics. This closes the check/install race that an overwriting replace would leave at an immutable identity. After syncing the final directory, it hard-links the already durable publication record as `.<filename>.publication-committed` and syncs that witness entry; this is the authoritative artifact commit point. The pending record remains the read barrier while that witness may only be transiently visible. The ordinary success path removes the barrier and directory-syncs the removal. Hidden UUID temporary cleanup is always best effort.

Promotion and `StrictStrategyReleaseResolver` check publication state before and after reading. Any visible pending record blocks consumption, including the interval where a newly linked witness is visible but its directory fsync has not completed. A commit witness becomes readable only after that barrier is no longer visible; it must parse under the closed publication-record schema and bind the exact final-byte SHA-256. Files with neither sidecar remain readable for compatibility with legacy JSON artifacts, but a failed pre-commit publication cannot fall through to that legacy shape because pending is durable before final installation. A failed post-commit barrier removal first restores pending from the witness, using a hard link and then an exact file-sync'd copy fallback. If both restoration paths fail while pending remains absent, the publisher makes one direct parent-directory fsync attempt to prove deletion durable. When that proof also fails, it does not report a false publication failure: the exact final file and witness already crossed the commit point, the currently visible state is committed, and a restart can only retain that state or conservatively resurrect the matching pending barrier for explicit recovery. If any restoration leaves pending visible, the command raises and readers remain blocked. Corrupt, mismatched, or changing sidecars fail closed.

Recovery is explicit. After proving that the previous writer has stopped, rerun the exact candidate-build or promotion command with `--recover-publication`. Recovery requires the recomputed canonical bytes to match the SHA-256 bound by the pending record, re-syncs that record and its directory before any final-path action, re-syncs the recorded parent lineage, and either installs or verifies the exact final file before establishing or re-syncing the committed witness and durably removing the pending read barrier. A malformed record, a record for different bytes, or conflicting final content remains fail-closed and requires investigation rather than deletion or overwrite. Ordinary publication never silently adopts pending state.

The final-file and committed-witness installs require same-directory hard-link support, available on the supported NTFS and ordinary POSIX filesystems. An unsupported filesystem raises a typed publication error and leaves pending state; there is no fallback to an overwriting replace.

## Runtime resolution and rollback

`StrictStrategyReleaseResolver.resolve(strategy_release_id, *, expected_rule_id, expected_symbol)` validates `sr1_[0-9a-f]{64}` before path construction and reads exactly one committed artifact. Candidate construction binds the full config payload symbol to both the dataset and supported-symbol identity and revalidates the protocol cross-field relationships above. Resolution rejects pending-only or sidecar-mismatched publication, missing, malformed, corrupt, rejected, untrusted-SCM, non-independent, non-canonical-statement, review-coordinate-mismatched, protocol-inconsistent, path-mismatched, rule-mismatched, or symbol-mismatched content. There is no strategy-name, newest-file, temporary-file, or mutable current-pointer lookup. Later checkout or registry changes do not reinterpret a self-contained release.

Rollback removes the R0 release artifact or code change; it does not change trusted generations, observations, account state, leverage, orders, or positions. Candidate generation and runtime resolution have no network or Broker path.

## 30-B handoff

30-B may construct future staged dry-run intent data only when configuration supplies an explicit approved `strategy_release_id` and resolution succeeds with the expected MU rule and symbol. Missing approval blocks intent construction. Approval does not authorize Demo or Production mutation, does not prepare an order request, and does not close [Issue #7](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/7).
