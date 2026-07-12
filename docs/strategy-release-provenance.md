---
feature_ids: [R0, issue-45]
topics: [strategy-release, provenance, reproducibility, trading-safety]
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

## Historical experiment protocol

`mu.baseline.walk_forward.cold_start.v1` reads an explicit `data/live/generations/<run_id>/manifest.json`. It never reads `current.json`, recalculates historical freshness from wall-clock time, refreshes data, or calls a provider. The reader validates schema v3 identity, source containment, row counts, and the canonical content SHA-256 of every effective interval.

TRAIN, VALIDATION, and OUT_OF_SAMPLE are explicit contiguous `[start_ms, end_ms)` windows with `input_start_ms == start_ms`. Each window starts with fresh indicator, position, pending-signal, and equity state. Only complete 1h candles whose source interval is inside the split are used, and their regime becomes available at the 1h close rather than its open; pre-window state and future hourly closes cannot leak into the split. The existing deterministic OHLC backtest closes any remaining position with `end_of_data`. The candidate records starting equity, fee profile/rate, fill model, slippage, and partial-fill model; result summaries use canonical finite decimal strings.

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

Missing, deleted, dismissed, self-authored, or mismatched review evidence fails closed. Promotion reads the authoritative GraphQL review node and rejects `includesCreatedEdit=true`, so a summary edited after submission cannot be presented as the original approval bytes. Only after live verification does promotion capture the SCM coordinates and canonical snapshot, construct the content-addressed release, and atomically write `config/strategy-releases/<strategy_release_id>.json`.

Authenticity and integrity remain separate. Promotion establishes authenticity by querying SCM and checking reviewer independence. Runtime does not contact SCM; it verifies the embedded snapshot digest and all duplicated bindings. A digest alone is not treated as proof of reviewer identity.

## Runtime resolution and rollback

`StrictStrategyReleaseResolver.resolve(strategy_release_id, *, expected_rule_id, expected_symbol)` validates `sr1_[0-9a-f]{64}` before path construction and reads exactly one artifact. Candidate construction binds the full config payload symbol to both the dataset and supported-symbol identity. Resolution rejects missing, malformed, corrupt, rejected, path-mismatched, rule-mismatched, or symbol-mismatched content. There is no strategy-name, newest-file, or mutable current-pointer lookup. Later checkout or registry changes do not reinterpret a self-contained release.

Rollback removes the R0 release artifact or code change; it does not change trusted generations, observations, account state, leverage, orders, or positions. Candidate generation and runtime resolution have no network or Broker path.

## 30-B handoff

30-B may construct future staged dry-run intent data only when configuration supplies an explicit approved `strategy_release_id` and resolution succeeds with the expected MU rule and symbol. Missing approval blocks intent construction. Approval does not authorize Demo or Production mutation, does not prepare an order request, and does not close [Issue #7](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/7).
