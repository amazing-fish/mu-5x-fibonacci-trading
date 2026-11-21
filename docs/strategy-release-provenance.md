---
feature_ids: [R0, issue-45, issue-49, issue-69]
topics: [strategy-release, provenance, reproducibility, durable-publication, trading-safety]
doc_kind: spec
created: 2026-07-12
---

# Strategy release provenance

> **Scope note:** Active code in `mu_strategy/research/strategy_releases.py` implements the lightweight canonical config payload codecs (`StrategyConfigPayloadV1`, `StrategyConfigPayloadV2`, `parse_strategy_config_payload`, `TrustedExperimentDatasetV1`) used by research replay provenance and Stage 0 fingerprints. Earlier unconsumed release-candidate CLI, SCM promotion, artifact-publisher, and staged order-intent store scaffolds were pruned; re-introducing automated release promotion or staged execution remains gated on future execution milestones.

R0 separates a named research strategy from an execution-eligible strategy release. A registry name such as `baseline` is convenient for research, but it does not prove which rule, config, code, data, experiment, or approval was used. Staged execution must consume a content-addressed approved release instead.

## Implemented config & dataset provenance identities

- `strategy_rule_id` is the registry-owned semantic rule identity (`mu_strategy/strategies/registry.py`). The MU baseline is `mu.baseline.second_pullback.long_limit.v1`.
- `strategy_config_sha256` binds every field in its explicitly versioned frozen `StrategyConfig` payload (`mu_strategy/research/strategy_releases.py`). New `StrategyConfigPayloadV2` payloads include the reference calendar ID and content hash; `StrategyConfigPayloadV1` retains its exact historical fields/hash and weekday-window semantics. Both codecs reject missing, extra, non-canonical, and non-finite values; unknown versions fail. See [calendar versioning](trading-calendar.md#来源版本与历史).
- `TrustedExperimentDatasetV1` (`mu_strategy/research/historical_data.py`) binds the exact pinned trusted generation (`run_id`, `symbol`, manifest schema v3/v4, requested/effective intervals, and per-interval canonical content SHA-256) used by historical replay (`cli`, `visualize`, `experiments/strategy_ladder`, `experiments/trend_exit`, `experiments/portfolio_research`).

These identities are orthogonal. Registry membership is not approval, a config hash is not code provenance, and a positive backtest result is not broker authorization.

## Deferred release-promotion & Stage 30-B design contract

The sections below summarize the deferred promotion and consumption boundary design; no promotion CLI, release-artifact publisher, or Stage 30-B order-intent store is currently active in the repository.

- **Approval & promotion separation:** A future release candidate must bind the rule, full versioned config, exact clean Git commit SHA, pinned trusted generation hashes, and explicit out-of-sample protocol results, separate from immutable SCM approval evidence.
- **No implicit fallback:** Runtime execution may never fall back to a strategy name, newest file, temporary artifact, or mutable current pointer, and approval does not authorize Demo or Production mutation or close [Issue #7](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/7).
- **Historical local review record:** The first locally reviewed MU candidate referenced generation `e702be27d2de4b2d92b12bf01c70d02d` (evaluated commit `b92985b2e9709bcd95effb84e77e7975f916c620`, candidate fingerprint `e9eb5a07017565a1d62f21c453c7bbfb7bfa885c92c4b340c713332ecb63f648`); no approved release is tracked in this repository.
