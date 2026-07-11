# MU Strategy Architecture

This project is organized as a research-first trading strategy workbench. It also contains a guarded OKX Demo application layer. Production live trading is not implemented.

## Data

Package: `mu_strategy.market_data`

- `providers/okx.py`: OKX public candle fetching for `MU-USDT-SWAP`.
- `providers/binance.py`: Binance futures candle fetching for explicit comparison runs.
- `cache.py`: CSV cache paths, reads/writes, incremental merge, and lookback pruning.
- `symbols.py`: OKX swap symbol normalization and aliases.
- `universe.py`: dynamic OKX Top USDT-SWAP universe selection.
- `trusted_data/contracts.py`: dataclass/Enum contracts for dataset health, validation reports, refresh runs, trust decisions, trusted bundles, and universe snapshots.
- `trusted_data/evaluate.py`: shared publication health classification plus refresh/load candle evaluation for windowing, normalization, freshness, built/native validation, and requested-days coverage.
- `trusted_data/policy.py`: interval dependency planning, freshness policy, and trading/research/observe trust policies.
- `trusted_data/validation.py`: in-memory candle normalization plus `5m -> 15m/1h` built/native validation.
- `trusted_data/store.py`: CSV, JSON manifest, and JSONL run-log repository with atomic per-file writes.
- `trusted_data/refresh.py`: the canonical trusted refresh use case; it owns OKX provider calls, ticker universe fetch, CSV writes, manifest writes, and run-log appends.
- `trusted_data/load.py`: the only trusted cache-only load use case; it never accesses the network and never writes CSV, manifest, or run-log files.
- `trusted.py`: compatibility facade for old public imports; implementation delegates to `trusted_data`.
- `service.py`: thin application facade that adapts legacy `CandleBundle` callers to `trusted_data` refresh/load use cases.

Rules:

- OKX is the default source for MU baseline work.
- Unconfirmed OKX candles are ignored.
- Existing OKX caches are incrementally updated and pruned to the requested `days` window.
- Adjacent candle continuity is gated by `previous close -> next open`; gaps above 2% raise `DataQualityError`.
- If an incremental OKX refresh fails, existing cached data is still usable.
- `data/live/current.json` is the atomic pointer to the current trusted generation. Each generation lives under `data/live/generations/<run_id>/` with its schema v3 manifest and matching canonical CSV set. The global refresh command/use case is the only writer for the current pointer and generation directories.
- Trusted refresh and trusted consumer load are separate processes. `python -m mu_strategy.commands.refresh_market_data` is the only trusted refresh entry point; backtest, visualization, and demo are cache-only consumers.
- Trusted refresh can be scoped with repeatable `--symbol` values such as `MU` or `MU-USDT-SWAP`. Explicit-symbol mode normalizes and de-dupes OKX swap symbols, skips the Top universe ticker list, and publishes only the requested subset into the same schema v3 generation contract.
- Trusted refresh may fetch up to `--max-concurrency` symbol/interval segments concurrently (CLI default `2`). Programmatic requests default to serial execution (`1`) so existing compatibility-facade callers with custom fetchers remain thread-safe unless they explicitly opt into concurrency. Dataset validation, generation CSV writes, manifest construction, and the single atomic `current.json` publication remain on the caller thread after all fetch candidates are collected.
- Trusted consumers never perform provider/network refresh, CSV writes, manifest writes, run-log appends, universe mutation, or canonical `run_id` publication. Backtest and visualization default to trusted cache-only loading and no longer accept the old data-path flags `--refresh`, `--source`, or `--trusted-data`; run `python -m mu_strategy.commands.refresh_market_data` first, then run `python -m mu_strategy.cli`, `python -m mu_strategy.visualize`, or `python -m mu_strategy.commands.okx_demo_loop`.
- The old in-process per-symbol consumer refresh APIs remain removed. Canonical subset refresh is only available through the standalone trusted refresh command and still writes the shared generation publication.
- Trusted storage is CSV + `current.json` + versioned generation manifests + JSONL run log. It does not use DB, Parquet, or a local web service.
- Generated backtest, visualization, data-health, and scanner reports are local artifacts. Write them under ignored paths such as `reports/live/`; do not treat tracked report files as the authoritative baseline.
- Manifest schema v3 records `run_id`, `attempt_status` (`RefreshAttemptStatus`), `snapshot_usability` (`SnapshotUsability`), requested/effective intervals, universe snapshot, provider failures, warnings, cycle-level error, and dataset health for every `symbol/interval`. It may also carry optional `diagnostics.refresh_segments` with per-symbol/per-interval fetch mode, elapsed time, rows, reuse, and failure reasons; strict consumers ignore this optional diagnostics payload and continue to derive trust only from dataset health.
- `RefreshAttemptStatus` is refresh-attempt health (`success`, `degraded`, `failed`). Zero usable datasets always classify the attempt as `failed`, regardless of whether the cause was provider failure, cache read failure, validation failure, requested-days coverage, or content hash mismatch.
- `SnapshotUsability` is published snapshot health (`usable`, `stale`, `invalid`) derived from DatasetHealth availability/integrity/freshness. Zero usable snapshots fail closed to `invalid`; mixed usable/unusable snapshots keep the stricter derived dataset state.
- Dataset health is per-cache health: availability, integrity, freshness, reasons, row count, time range, source file, content hash, and validation report.
- Interval dependencies are planned once: `15m` and `1h` consumers automatically include `5m` because built/native validation depends on the base interval.
- Freshness is calculated from clock time, interval length, max staleness bars, and the last confirmed candle timestamp.
- Missing or malformed manifest is fail-closed for trading strict policy. Legacy flat `data/live/manifest.json` formats are no longer trusted input for consumers or incremental refresh reuse; without `current.json`, consumers fail closed and the next refresh performs a full-history publication into a new schema v3 generation.

## Core

Package: `mu_strategy.core`

`core.market_context.build_hourly_context` owns the pure mapping from 1h regime calculations to 15m candle timestamps. CLI, entry scanning, and visualization depend on this core function; `mu_strategy.cli` re-exports it for compatibility with existing callers.

## Strategies

Package: `mu_strategy.strategies`

- `registry.py`: named strategy groups.
- `components.py`: entry, position, exit, and filter labels.
- `presets/mu.py`: MU strategy preset names.
- `presets/fibonacci.py`: preferred Fibonacci lookback records by symbol.

Current fixed research baseline: `baseline`. MU uses the 2h Fibonacci lookback record (`fib_lookback=8`); unknown symbols fall back to the legacy 8h default (`fib_lookback=32`).

Fee realism:

- Default backtests use the `market/taker` cost profile at `0.0500%`.
- `limit/maker` at `0.0200%` is available as an explicit sensitivity run.
- The baseline entry signal may be limit-style, but the research engine does not model order-book queue priority, spread crossing, partial fills, or missed maker fills. Use the default market/taker cost unless those execution details are modeled separately.

## Research

Package: `mu_strategy.research`

Use this layer to state the current best-known strategy and supporting notes. It should read experiment outputs, not implement backtest mechanics.

## Experiments

Package: `mu_strategy.experiments`

`experiments.walk_forward` runs strategy-group comparisons and renders the Markdown and HTML component matrix reports. Walk-forward windows are independent; aggregate dashboard drawdown uses per-window drawdown rather than concatenating reset equity curves.

`experiments.fibonacci_pullback` runs 1h-12h Fibonacci lookback sweeps for one or more assets and renders the ranking reports used by `docs/fibonacci-preferred-parameters.md`.

## Selection

Package: `mu_strategy.selection`

Use this layer to apply a fixed strategy across candidate rows and rank them without network access. It is the planned home for broader symbol selection.

## Execution Planning

Package: `mu_strategy.execution`

The shared typed entry-decision vocabulary lives in `mu_strategy.models`, the lowest common dependency of strategy primitives, entry scanning, and execution planning. `EntryDecisionCode` maps through one catalog to an `EntryDisposition` and `EntryDecisionStage`; result objects expose the stable code plus derived disposition and stage while retaining their compatibility action and reason fields.

This layer returns non-trading decisions with a fixed disposition mapping:

- `READY -> allow`: current fixed strategy permits an entry plan.
- `WAIT -> wait`: signal or execution timing is incomplete.
- `BLOCK -> block`: a signal or execution risk filter blocks entry.

It may return margin steps and initial stop planning. It must not place orders or call broker APIs.
Execution actions are projected from typed disposition; reason text is compatibility display data and never classifies `wait` versus `block`. Legacy direct constructors may omit the code and receive `UNKNOWN`, but `execution_decision()` and strategy production paths never produce `UNKNOWN`.

## Entry Scanning

Package: `mu_strategy.entry`

This layer turns existing strategy primitives into a reusable scanner result for application code:

- `scanner.EntryScanResult`: action, reason, Fibonacci distance, RSI/MACD values, 1h regime, trigger price, and initial stop.
- `scanner.scan_entry`: consumes `15m/1h` candles and a fixed `StrategyConfig`.

The scanner calls existing strategy functions; it does not tune parameters or own broker behavior.
Scanner actions use the same disposition with a scanner-specific projection: `READY -> enter`, `WAIT -> wait`, and `BLOCK -> skip`. The public strings and compatibility reason messages remain unchanged.

Second-pullback has two deliberately different stages. Execution planning returns `WAITING_SECOND_PULLBACK / WAIT / PENDING_ENTRY -> wait` immediately after signal confirmation. An active, unfilled scanner pending signal returns `SECOND_PULLBACK_LIMIT_READY / READY / PENDING_ENTRY -> enter`; here `enter` means a resting Fibonacci limit plan can be created, not that a fill occurred or a market chase is allowed. The pending scanner path continues to apply the current trading-window gate without reapplying current-bar regime, RSI, or MACD filters.

## OKX API Execution Preparation

Package: `mu_strategy.live`

This layer is isolated from the backtest engine, entry scanner, and execution-planning package. It exists to prepare and measure future API-driven execution without changing research results.

- `okx.OKXRestClient`: minimal OKX REST client with official HMAC signing, demo header support, read-only account endpoints, open-order queries, leverage setup, and guarded demo-order methods.
- `okx.OKXInstrumentSpec`: tick/lot/contract-value rounding for limit prices and contract sizing.
- `okx.ShadowExecutionLedger`: append-only JSONL ledger for paper/shadow execution observations.
- `okx_cli`: command-line entry point for read-only checks, shadow event recording, and OKX demo trading dry-runs.

Safety boundaries:

- Production live order placement is not implemented.
- The strategy engine does not call this package to place orders automatically.
- Demo orders require `--confirm-demo-order`; without it, the CLI returns a sanitized dry-run request.
- Confirmed demo orders preflight the demo instrument endpoint first and block unsupported instruments before sending an order request.
- Secrets are read from environment variables and redacted from dry-run output.
- Shadow execution writes local audit rows only and never calls OKX.

## OKX Demo Application

Package: `mu_strategy.demo_trading`

This is the five-minute demo automation layer. It consumes the fixed research baseline and application services:

1. Trusted manifest universe snapshot plus fixed watchlist symbols; `MU-USDT-SWAP` is included by default.
2. Cache-only trusted `15m/1h` candle bundle load with `5m` dependency validation.
3. `entry.scan_entry` result.
4. Open exposure risk cap.
5. Stable alphanumeric `clOrdId` for idempotency.
6. OKX Demo isolated `5x` limit-buy placement near the Fibonacci trigger.

Defaults:

- `10 USDT` notional per order.
- Maximum `3` open orders/positions.
- Dry-run unless `--confirm-demo-orders` is supplied.
- `data/live` trusted data directory.
- `300` second loop interval in `python -m mu_strategy.commands.okx_demo_loop`.
- Optional `--dashboard-output` writes an auto-refreshing local HTML dashboard after each scan cycle.

Boundaries:

- The demo layer cannot tune strategy parameters during execution.
- The broker adapter only executes the fixed plan; it does not own signal logic.
- Missing credentials are acceptable in dry-run and fail before order submission in confirmed mode.
- Dry-run and confirmed demo use the same trusted gate. Invalid, stale, missing, malformed, or failed-run trusted data blocks scanner calls and order generation.
- A scan cycle carries the trusted `run_id` from the loaded manifest into scan payloads when available.
- Scan JSON keeps its existing versionless compatibility fields and does not expose `decision_code`, disposition, stage, or Enum representations. Adding structured typed metadata to JSON requires a separate versioned contract change.
- Dynamic universe limit semantics are explicit: in the default trusted-manifest mode, `limit > 0` returns up to that many crypto universe symbols plus up to that many stock-token universe symbols, `limit == 0` means watchlist-only, and `limit < 0` is rejected.
- v1 does not implement production order lifecycle, cancel/retry handling, fills, position reconciliation, or risk kill-switches.
- If no `planned` order exists, the dashboard explicitly reports no order suggestion and no cancel target.

## Visualization

Package: `mu_strategy.viz`

`viz.backtest` renders the interactive Plotly backtest dashboard. The top-level `mu_strategy.visualize` module remains a compatibility wrapper. The recommended output location is a local ignored file such as `reports/live/mu_okx_MU_USDT_SWAP_180d_baseline_backtest.html`.

`viz.entry_dashboard` renders the latest OKX scan payload as a compact manual-order review dashboard. It shows concrete planned limit details, cancel targets bound to each planned order, scan blockers, and data errors from the already-produced JSON payload. Its HTML output is also a local artifact, normally under `reports/live/`.

`viz.data_health` renders the trusted manifest as a static HTML dashboard. It displays `RefreshAttemptStatus`, `SnapshotUsability`, run_id, requested/effective intervals, warnings, cycle-level error, and dataset health fields from the manifest. Row badges are derived from availability/integrity/freshness together: missing or invalid integrity is invalid, stale freshness is stale, and only available+valid+fresh is ok. The dashboard is review evidence, not a source-controlled baseline.

## Compatibility Wrappers

These compatibility entry points remain valid during migration:

- `mu_strategy.data`
- `mu_strategy.strategy`
- `mu_strategy.walk_forward`
- `mu_strategy.visualize`
- `mu_strategy.cli`

`mu_strategy.cli` and `mu_strategy.visualize` are compatibility entry points by import path only; their market-data behavior is the current trusted cache-only contract.

