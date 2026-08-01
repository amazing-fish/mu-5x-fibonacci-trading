---
feature_ids: []
topics: [architecture, execution, trading-safety]
doc_kind: spec
created: 2026-06-14
---

# MU Strategy Architecture

This project is organized as a research-first trading strategy workbench. It also contains a guarded OKX Demo application layer. Production live trading is not implemented.

## Filesystem durability

Module: `mu_strategy.fs_durability`

`fsync_directory()` is the single owner of the POSIX directory-fsync and Windows directory-handle `FlushFileBuffers` implementation. Open, flush, close, and unsupported-platform failures raise `OSError`; callers may wrap that failure in their own domain error, but they may not silently claim durability.

This low-level module does not define a publication commit point or recovery protocol. Trusted-generation publication, Stage 0 observation append logs, and immutable strategy-release publication still decide independently which file and directory entries must be durable, in what order, and how a failed or ambiguous write is recovered.

Trusted-generation publication keeps atomic replacement of `current.json` as its commit point. File and directory-sync failures before that replacement still fail publication. If the replacement succeeds but syncing its parent directory fails, the new complete pointer is already visible, so the writer reports `current_pointer_directory_sync_failed` as an explicit publication warning instead of returning a false rollback/failure result. A restart after that warning may observe either the previous complete pointer or the new complete pointer; trusted readers continue to validate whichever generation the pointer names.

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
- `trusted_data/store.py`: CSV, JSON manifest, bounded JSONL run-log, and path-safe generation-retention repository with atomic per-file writes.
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
- The standalone refresh command enables bounded generation retention after `current.json` publication. It keeps the newest `--keep-generations N` directories (default `3`, minimum `1`) plus the current generation unconditionally, even when current falls outside that recency window. Each deletion is revalidated as a direct, non-symlink child of `generations/` using the canonical storage-segment validator. `--dry-run` publishes normally and reports candidates/bytes without deleting.
- Generation reclamation is deliberately fail-open only after the trusted publication commit point: delete/permission/directory-sync failures become structured warnings and run-log evidence while the already-published generation remains readable. This does not change manifest validation, freshness, coverage, hash binding, or any consumer fail-closed gate.
- Trusted publication, refresh-input snapshotting, and consumer snapshotting share one cross-process critical section keyed to the trusted store. On POSIX it locks the existing store directory; on Windows it uses a path-derived named mutex, so consumers create no lease or pin file. A writer holds the lock across generation-manifest write, `current.json` replacement, and reclamation. Before any history/incremental request, a refresh holds it while resolving the latest current manifest and copying only the requested reusable dataset bytes; a consumer holds it across the same manifest-to-byte boundary. Network calls and later parsing/validation use those immutable bytes outside the lock, so reclamation cannot invalidate an in-flight refresh or opening context and cannot break a context that has already returned. Manifestless generation directories are treated as in-progress/crash remnants and are never reclaimed by the ordinary committed-generation policy.
- Trusted refresh and trusted consumer load are separate processes. `python -m mu_strategy.commands.refresh_market_data` is the only trusted refresh entry point; backtest, visualization, walk-forward, Fibonacci experiments, and demo are cache-only consumers.
- Trusted refresh can be scoped with repeatable `--symbol` values such as `MU` or `MU-USDT-SWAP`. Explicit-symbol mode normalizes and de-dupes OKX swap symbols, skips the Top universe ticker list, and publishes only the requested subset into the same schema v3 generation contract.
- Trusted refresh may fetch up to `--max-concurrency` symbol/interval segments concurrently (CLI default `2`). Programmatic requests default to serial execution (`1`) so existing compatibility-facade callers with custom fetchers remain thread-safe unless they explicitly opt into concurrency. Dataset validation, generation CSV writes, manifest construction, and the single atomic `current.json` publication remain on the caller thread after all fetch candidates are collected.
- Trusted consumers never perform provider/network refresh, CSV writes, manifest writes, run-log appends, universe mutation, or canonical `run_id` publication. Backtest, visualization, walk-forward, and Fibonacci experiment entry points default to trusted cache-only loading and no longer accept the old data-path flags `--refresh`, `--source`, or `--trusted-data`; run `python -m mu_strategy.commands.refresh_market_data` first, then run `python -m mu_strategy.cli`, `python -m mu_strategy.visualize`, `python -m mu_strategy.walk_forward`, `python -m mu_strategy.experiments.fibonacci_pullback`, or `python -m mu_strategy.commands.okx_demo_loop`.
- The old in-process per-symbol consumer refresh APIs remain removed. Canonical subset refresh is only available through the standalone trusted refresh command and still writes the shared generation publication.
- Trusted storage is CSV + `current.json` + versioned generation manifests + JSONL run log. It does not use DB, Parquet, or a local web service.
- `refresh_runs.jsonl` retains the newest 1000 complete JSON lines. Once at the cap, append atomically replaces the file with the newest 999 records plus the current record, so continuous refresh does not grow the audit file without bound.
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

`core.market_context.build_hourly_context` owns the pure mapping from 1h regime calculations to 15m candle timestamps. A regime derived from a 1h candle close becomes visible at `open_time_ms + 1h`, never at the candle open; callers pass canonical candle timestamps and do not compensate by rewriting them. CLI, entry scanning, visualization, ordinary walk-forward, and release experiments depend on this core function; `mu_strategy.cli` re-exports it for compatibility with existing callers.

Ordinary walk-forward and monthly Fibonacci partitions are reporting boundaries, not indicator-reset boundaries: they build one causal context from the complete supplied history and select the 15m keys belonging to each report partition. The release-candidate protocol is deliberately different: `mu.baseline.walk_forward.cold_start.v1` excludes pre-window context and starts each pinned split from fresh indicator state.

## Strategies

Package: `mu_strategy.strategies`

- `registry.py`: authoritative strategy-group names, rule identities, construction, selection/default status, and compatibility aliases.
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

`experiments.walk_forward` loads `15m` and `1h` candles from the trusted `data/live` generation, fails closed when that publication is missing or unusable, runs strategy-group comparisons, and renders the Markdown and HTML component matrix reports. Walk-forward windows are independent; aggregate dashboard drawdown uses per-window drawdown rather than concatenating reset equity curves.

`experiments.fibonacci_pullback` uses the same cache-only trusted-generation boundary for 1h-12h Fibonacci lookback sweeps and renders the ranking reports used by `docs/fibonacci-preferred-parameters.md`. The trusted layer is OKX-only, so a non-OKX `AssetSpec.source` is rejected before any load rather than silently remapped or routed to a legacy provider.

## Strategy Release Provenance

Packages: `mu_strategy.research.strategy_releases`, `mu_strategy.research.strategy_artifact_publication`

R0 gives staged execution a self-contained strategy identity without changing strategy behavior. The registry owns the versioned rule ID, the v1 config payload owns every `StrategyConfig` field, and a candidate binds those values to an exact evaluated Git SHA, explicit trusted generation and interval hashes, closed experiment protocol, assumptions, windows, and canonical results. The initial release domain is explicitly the MU baseline: its rule ID/name/symbol and second-pullback/baseline-stop semantic discriminators are code-checked against the registry, while numeric parameters remain config-addressed and independently reviewable.

Candidate generation reads only an explicit historical generation and requires a clean exact-HEAD match. Every window requires the exact gap-free set of fixed-step `15m` and `1h` opens before context construction; hourly regime state becomes available at candle close to prevent missing-hour carry, pre-window carry, and future-close lookahead. The runner and candidate parser share one protocol-assumption validator: fee profile is closed, fee profile/rate must match the frozen config, explicit slippage is zero, schema-v3 effective evidence closes over `5m`/`15m`/`1h`, and every cold-start result starts from the declared equity. Result parsing also verifies return/equity/P&L/zero-trade arithmetic within the documented decimal tolerance, and nested schema versions are strict integers rather than boolean-equivalent values. Candidate generation cannot read `current.json`, refresh data, use network/private APIs, or reach Broker mutation. Candidate files are ignored local artifacts until a live SCM review binds their exact fingerprint and evaluated SHA under an explicit fingerprinted approval mode.

Promotion verifies the trusted live PR author, evaluated commit author/committer lineage, and the GraphQL review node's unedited state before capturing approval evidence. Both modes require the reviewer to differ from the PR author because GitHub does not permit PR authors to approve their own PRs. `independent_review_v1` additionally requires the reviewer to differ from the evaluated commit author/committer and remains the CLI default. `solo_maintainer_v1` must be requested explicitly; it permits the reviewer to match the evaluated commit author/committer and carries no independent-review guarantee. Its attainable single-maintainer workflow therefore uses a separately authenticated automation or bot actor as the PR author and the maintainer as the canonical approving reviewer. No other gate changes: `lastEditedAt != null` or `includesCreatedEdit=true` fails closed, as do missing mapped identities, a non-`APPROVED` decision, non-canonical statement, or mismatched coordinates. The closed mode is required in the snapshot and participates in both snapshot and release content identities. Runtime resolution is deliberately offline: `StrictStrategyReleaseResolver` requires an exact content-addressed release ID plus expected rule and symbol, optionally enforces a caller-supplied `required_approval_mode`, binds the config/dataset/supported symbol identities, and has no by-name/latest/current-pointer fallback. SCM establishes approval authenticity at promotion time; runtime rejects self-consistent snapshots that change the closed mode, GitHub provider/repository, review-record/URL coordinates, canonical approval statement, or mode-applicable reviewer/author independence, then checks every duplicated binding and content digest.

Candidate and approved-release publication use one strategy-domain durability boundary. A file-sync'd unique temporary is never authoritative. Before mutation, the low-level publisher and recovery APIs require an explicit existing durability anchor containing the output path: repository-contained candidate generation uses the repository root, ordinary promotion uses the existing parent of the release directory, and custom paths can supply `--publication-durability-anchor` without changing the requested final path. A canonical pending record binds the exact content digest and created-parent count before an atomic hard-link, no-overwrite final-path install; a failed exclusive pending write removes only its identity-matched partial inode and syncs the parent before erroring. After the pending directory is durable, every writer syncs the full directory-entry chain from the artifact parent back to that stable anchor before checking a concurrent witness or succeeding. A later observer therefore cannot return first while relying on a paused creator to make shared parents durable, and an omitted recovery anchor cannot silently downgrade that proof. The linked temporary name is mandatorily removed before the final inode is accepted; the following directory fsync proves both install and removal. Recovery validates the recorded count, then re-syncs the complete chain to the same anchor before touching the final path. After the final directory is durable, the publisher hard-links the durable record to a positive committed witness and syncs that witness entry; this is the artifact commit point. Pending remains a reader-priority barrier while the witness may be only transiently visible, closing the concurrent witness-visibility window. After acquiring pending, a writer rechecks for a concurrently committed witness and treats its exact final bytes as authoritative: matching content retires the writer's ephemeral-lineage marker and succeeds idempotently; conflicting content retires that owned marker before reporting conflict. A failed post-commit barrier transition restores pending from the witness through link or exact-copy fallbacks; if no barrier can be recreated, a second directory fsync attempts to prove the deletion durable. A second fsync failure with pending still absent is a committed outcome rather than an error paired with consumable evidence: final and witness were already durable, and restart can only preserve that state or restore the matching recovery barrier. Readers and writers use `lstat` plus opened-file identity checks and accept sidecars and final artifacts only as stable regular files; final artifacts additionally require `st_nlink == 1` throughout comparison/fsync. Symlinks, special files, hard-link aliases, path swaps, missing anchors, and anchors outside the output lineage fail closed rather than masquerading as compatibility state. Readers require an exact witness when publication sidecars exist, while retaining genuine single-link regular-file, no-sidecar legacy JSON compatibility. Identical final bytes are idempotent, different bytes conflict without overwrite even when created concurrently, and pre-commit failures or a visible pending barrier require exact-content recovery. The same-directory hard-link requirement fails closed on unsupported filesystems, with no replace fallback. This protocol is intentionally not shared with trusted-generation publication, Stage 0 append logs, or future execution ledgers, whose commit and recovery models remain separate.

The first MU candidate has passed reproducibility and independent local evidence review but is not an approved release until immutable SCM evidence exists under one recorded approval mode. Publishing that release is tracked separately by Issue #70. 30-B must receive an explicit approved release ID; approval permits only future staged dry-run intent construction and never authorizes Demo or Production Broker mutation. See [strategy-release-provenance.md](strategy-release-provenance.md).

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

The same package owns the Stage 1 `OrderIntent` v1 contract, its exact-ID strategy-release factory, the pure reuse/revision classifier, deterministic human-review rendering, and `instruments.OKXInstrumentSpec`. The instrument value object consumes already-fetched public metadata and centralizes tick/lot/contract-value floor rounding; it performs no network or account operation. `OrderIntentFactory` is Demo-only, accepts only canonical Stage 0 `READY_FOR_REVIEW` evidence, and has no broker, credential, refresh, ledger, reservation, or confirmation dependency.

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
- `okx.OKXInstrumentSpec`: compatibility re-export of the execution-domain public instrument value object; tick/lot/contract-value rounding has one implementation under `mu_strategy.execution.instruments`.
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

## Staged Execution Roadmap

This section is the design deliverable for [issue #30](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/30). It defines the target progression from observation to production execution; it does not implement or enable production orders. [Issue #7](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/7) remains the owner of Stage 4 production execution.

The labels below are normative:

- **Current** describes behavior present on `research/mu-strategy-groups` at merge commit `f887a3a`.
- **Target** is a contract that later implementation PRs must satisfy.
- **Open** is intentionally not frozen and must not be inferred from examples.

Roadmap Stage 3 is a delivery maturity stage. It is not the same concept as `EntryDecisionStage.EXECUTION` in `mu_strategy.models`.

### Evidence baseline

The following claim ledger was checked against primary GitHub and repository sources at 2026-07-11T18:43:27Z.

| Claim | Primary evidence | Verdict |
|---|---|---|
| Trusted market data is a prerequisite rather than part of this execution change. | Issue #30; `LoadTrustedBundle`, `trading_strict_policy()`, and the schema v3 generation contract; merged PRs [#21](https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/21), [#26](https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/26), and [#35](https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/35). | Use. The default canonical consumer is fail-closed; this roadmap does not change refresh or trust policy. |
| Typed entry decisions now provide the judgment vocabulary. | Merged [PR #41](https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/41); `EntryDisposition`, `EntryDecisionStage`, `EntryDecisionCode`, and `ENTRY_DECISION_CATALOG` in `mu_strategy.models`. | Use. The starting assumption that typed decisions were absent is obsolete. |
| The production execution layer is not implemented. | Issue #7 remains open; PR #41 explicitly excludes `OrderIntent`, `BrokerPort`, an execution ledger, and production trading; the current OKX mutation methods require a demo client. | Use. This is the gap owned by Stages 3-4. |
| Issue #30 is the design predecessor to issue #7. | Issue #30 keeps issue #7 focused on production execution, and issue #7 cross-references #30. | Use. This is a semantic dependency, not a GitHub parent/sub-issue relationship. |

Provenance: [primary | GitHub issues/merged PRs and repository source | observed 2026-07-11T18:43:27Z | issue #30 execution roadmap | high confidence].

### Current capability and contract gaps

| Surface | Current capability | Gap against the target contract |
|---|---|---|
| Read-only observation | `okx_cli read-only` performs public reads and, unless `--public-only` is used, balance and position reads. `--demo` selects the demo environment; without it, private reads can target production, but no order mutation method is called. | There is no stage-labelled observation record tying account context to a scan cycle. |
| Shadow observation | `okx_cli shadow-record` appends `ShadowExecutionEvent` rows to local JSONL. | It is a manually supplied observation log, not the authoritative intent-to-broker audit ledger. |
| Dry-run scanner | `DemoTradingConfig.dry_run=True` can scan trusted cache data without credentials or private broker calls and can produce mutable `planned` order dictionaries. The CLI may make a public instrument metadata GET to calculate size. | Scan results are stdout/optional dashboard output only. The versionless scan JSON deliberately omits typed decision metadata, and the plan dictionary omits `decision_code`, `run_id`, target environment, and audit correlation. |
| Typed decision | `EntryScanResult` and `ExecutionDecision` carry a typed `decision_code` and derive disposition/stage from the shared catalog. | `demo_trading.run_once` still gates order planning on compatibility `action == "enter"` and accepts legacy `UNKNOWN` results. Typed metadata does not yet reach a confirmed order. |
| Demo order guard | `okx_cli demo-order` defaults to a sanitized dry-run and needs `--confirm-demo-order` to send. `OKXRestClient` checks `confirm_demo_order=True` again. | The confirmation is not a durable record containing actor, time, expiry, intent fingerprint, or `decision_code`. |
| Demo loop guard | `okx_demo_loop` is dry-run unless `--confirm-demo-orders` is supplied. Confirmed mode can read account state, set leverage, submit demo limit buys, and cancel stale bot limit orders. | The flag is process-scoped and `run_once(dry_run=False)` hard-codes the downstream confirmation boolean. It is not a per-intent, non-bypassable authorization contract. |
| Partial idempotency | `generate_client_order_id()` deterministically derives an OKX-safe `OD<20 hex>` from symbol, signal time, and trigger price, then compares it with currently open `clOrdId` values. | There is no durable reservation, restart recovery, full intent fingerprint, key conflict handling, or ambiguous-submit reconciliation. |
| Broker boundary | `OKXRestClient` has concrete demo methods and returns raw dictionaries. `run_once` accepts `broker: Any \| None`. | There is no typed `BrokerPort`, typed success/failure/unknown result, or separate production adapter composition root. |
| Orchestration | `demo_trading.run_once` performs account reads, trusted loading, scanning, stale-order cancellation, risk checks, leverage setup, and order submission in one function. | Scan, intent creation, authorization, persistence, submission, and reconciliation do not have independently enforceable boundaries. |

The default CLI path uses the canonical trusted generation and `trading_strict_policy()`. The gate must continue to rely on the resulting `TrustDecision.allowed` rather than restating a narrower status shortcut: the current policy checks the complete manifest and dataset health lattice, and a usable degraded attempt can be valid under that exact policy. Plain/legacy custom loader compatibility is not accepted as promotion evidence and must never become a fallback for a staged execution path.

### Cross-stage rules

These rules apply to every stage:

1. A higher stage may add effects but may not weaken a lower-stage trust, decision, audit, or environment invariant.
2. Missing or untrusted canonical data prevents scanner execution and new intent creation. It is represented separately from a scanner exception.
3. A typed `WAIT` or a signal/pending-entry/execution-stage `BLOCK` result is a normal no-action outcome, not a system failure. An input-stage trust block remains `DATA_GATE_BLOCKED`, and a scanner exception is `SCAN_FAILED`. A `READY` result is only a candidate for the stage-specific next gate.
4. Free-text `reason` and broker messages are diagnostics only. Control flow uses typed decision, authorization, broker outcome, audit, and reconciliation states.
5. Default configuration never submits a production order. No stage silently changes `DEMO` to `PRODUCTION`.
6. A risk-reducing cancellation of an already-known bot order is not a new entry submission. From Stage 2 onward it requires its own lineage, idempotency key, gate, and audit events; it may not be inferred from an unrelated or unknown order.
7. Execution environment is part of business identity, not an intent-revision field. Demo and production actions may share only `signal_lineage_id` for research provenance; they have distinct `business_action_id`, `audit_correlation_id`, authorization/confirmation lineage, audit stream, `mutation_action_id`, idempotency namespace/key, `clOrdId`, adapter, and credential boundary. There is no cross-environment supersession, reuse, alias, or reservation migration.
8. An intent is content-bound to the exact trusted generation and interval hashes used to create it. A current-generation change is handled by the deterministic Stage 2 gate below; no implementation may edit the old intent or substitute a new `run_id` or hash tuple in place.

### Stage 0 — Observe/report

**Purpose and safety invariant.** Observe trusted-data health, account context, and scanner outcomes for human review. No broker mutation is possible.

**Current evidence.** `okx_cli read-only`, `shadow-record`, data-health output, scanner payloads, and the local entry dashboard cover parts of this stage. There is no single durable, versioned scan observation record.

**Allowed side effects.**

- Read the pinned trusted generation and local configuration.
- Perform explicitly selected public or private read-only GETs.
- Write stdout, local Markdown/HTML reports, and append-only observation records.
- Record manual shadow observations locally.

**Prohibited side effects.**

- No leverage change, order submission, cancellation, credential mutation, trusted-data refresh, or current-generation publication.
- No `OrderIntent` and no broker request preparation that can be replayed as authorization.

**Input and decision handling.**

- A health report may describe unusable data, but the scanner runs only after the canonical trust gate allows the required `5m/15m/1h` dependency set.
- Missing, malformed, stale, invalid, or otherwise disallowed data produces a typed input-block observation such as `MARKET_DATA_UNAVAILABLE / BLOCK / INPUT`; it does not call the scanner.
- A scanner exception produces `SCAN_FAILED` and retains exception type plus a sanitized message.
- Typed `WAIT` and signal/pending-entry/execution-stage `BLOCK` results are persisted as normal no-action outcomes. An input-stage `BLOCK` stays data-blocked. `READY` is reported but cannot create an executable intent in Stage 0.

**Persistent evidence target.** A versioned observation envelope must record `scan_id`, timestamps, symbol, trusted `run_id`, dataset content hashes, requested/effective intervals, strategy/config fingerprint, typed `decision_code`, derived disposition/stage, compatibility action/reason, and either the full scan result or a typed failure. The existing versionless dashboard JSON remains unchanged; a new contract is required.

The implemented 30-A schema, canonical fingerprint, cycle-sized JSONL commit, strict reader, and non-authorization boundary are documented in [stage0-observations.md](stage0-observations.md).

**Failure, stop, and rollback.**

- `DATA_GATE_BLOCKED`: report and stop before scanner.
- `SCAN_FAILED`: report and stop the affected symbol/cycle.
- `OBSERVATION_WRITE_FAILED`: the cycle is not promotion evidence; no later stage work may consume an unpersisted result.
- Repeated data or scanner failures keep the system in Stage 0. Local observation files can be removed without broker rollback because no mutation occurred.

**Promotion evidence.** Deterministic tests must prove zero mutation calls; every cycle outcome must be durably classifiable as data-blocked, scan-failed, normal no-action, or ready-for-review; reports must be reproducible from the recorded trusted generation and config. No fixed observation duration is currently specified.

**Ownership.** Issue #30 and Stage 0 implementation PRs. Market-data refresh performance and general report cleanup are excluded.

### Stage 1 — Dry-run scanner

**Purpose and safety invariant.** Convert an eligible typed scan result into an immutable, exact demo-targeted `OrderIntent` for manual review, while making no broker mutation.

**Current evidence.** `run_once(dry_run=True)` creates a `planned` dictionary and can size it with public instrument metadata. This is a useful prototype, not the target typed or durable intent contract.

**Allowed side effects.**

- All Stage 0 reads and local writes.
- Public instrument metadata GETs needed to freeze exact price/size strings.
- Durable creation of a demo `OrderIntent` and a manual review record.

**Prohibited side effects.**

- No private account dependency, leverage change, order submit, order cancel, or production-target intent.
- No intent from `UNKNOWN`, `WAIT`, `BLOCK`, untrusted data, missing `run_id`, missing content hashes, or a free-text action alone.

**Input and decision handling.**

- Data-blocked and scan-failed outcomes remain distinct and create no intent.
- Normal no-action `WAIT`/`BLOCK` outcomes are persisted and create no intent.
- The v1 intent factory accepts only scanner-produced `SIGNAL_CONFIRMED` or `SECOND_PULLBACK_LIMIT_READY` with `READY` disposition, complete trigger/stop/size fields, and a canonical trusted-data reference.
- `EXECUTION_ACCEPTED` is not silently mapped into the scanner intent factory; expanding that mapping needs a versioned contract decision under issue #7.

**Persistent evidence target.** Store the observation envelope, immutable intent, intent fingerprint, human review verdict, reviewer identity/time, and any field-level disagreement. The intent must be reproducible from its source references.

**Failure, stop, and rollback.**

- `INTENT_INELIGIBLE` or `INTENT_INVALID`: persist the typed reason and create no intent.
- `INSTRUMENT_SPEC_UNAVAILABLE`: do not create a partly sized intent.
- `INTENT_WRITE_FAILED` or fingerprint conflict: fail closed and stop the cycle.
- Before any mutation reservation exists anywhere in the demo-specific business action, a newer demo revision may supersede an older immutable revision within that action through the atomic business-action selection fence defined below. It is never edited in place. An environment change is not a revision and cannot supersede or reuse the demo action.

**Promotion evidence.** A candidate gate is “`N` consecutive scheduled trading days of dry-run intents agree with recorded human review.” Neither issue #30 nor the repository currently defines `N`, the minimum READY sample count, or the required reviewer set; all remain open policy parameters. Promotion also requires zero duplicate environment-specific business actions across reruns/restarts, same-environment exact-fingerprint reuse, same-environment pre-reservation supersession tests, cross-environment rejection tests, complete outcome classification, deliberate failure-injection coverage, and evidence that no broker mutation method was called.

**Ownership.** Issue #30 and Stage 1 implementation PRs.

### Stage 2 — Demo guarded order

**Purpose and safety invariant.** Submit or cancel only in OKX Demo after a non-bypassable authorization tied to one exact intent or known order lineage. Demo and production must never share a business action, audit correlation, authorization/confirmation lineage, audit stream, `mutation_action_id`, idempotency namespace/key, `clOrdId`, adapter, or credential boundary.

**Current evidence.** The direct CLI has an explicit `--confirm-demo-order` guard and demo-instrument preflight. The loop has a coarser process-level `--confirm-demo-orders` guard, stable `clOrdId`, exposure checks, demo leverage setup, limit submission, and stale-order cancellation. These capabilities do not yet satisfy the target per-intent confirmation or audit contract.

**Allowed side effects.**

- All Stage 1 effects.
- With a valid demo confirmation: demo leverage configuration, one idempotent demo order submission, typed lookup/reconciliation, and an explicitly gated cancellation of a known bot-owned demo order.
- Current risk-reducing demo cancellation during a data failure may remain only after it is separated from entry submission and bound to known order lineage, its own policy result, idempotency key, and audit chain.

**Prohibited side effects.**

- No production adapter or production credentials.
- No blanket confirmation reused for a later or changed intent.
- No submit from compatibility `action`, free text, `UNKNOWN`, expired intent, expired confirmation, missing audit reservation, or environment mismatch.
- No automatic resubmit after a timeout or transport ambiguity.
- No import, alias, supersession, or migration of a production action, confirmation, reservation, key, client order ID, or audit lineage into Demo, or the reverse.

**Confirmation contract.**

- The human confirmer acts after the exact intent is persisted and before submission.
- `confirmation.granted` requires `confirmed_by`, `confirmed_at_ms`, `confirmation_expires_at_ms`, `business_action_id`, `intent_id`, `intent_fingerprint`, `decision_code`, and `environment=DEMO` as formal fields.
- The command must display the exact symbol, side, order type, size, price, leverage, stop-planning reference, trusted `run_id`, typed decision, and expiry being confirmed.
- Before the first broker-mutation reservation for the entry action, including leverage or submit, the gate loads the canonical current-generation pointer and re-evaluates the intent's exact `trusted_run_id` and content hashes under the current strict trading policy. Reservation is permitted only when the current generation still equals `intent.trusted_run_id`, that exact generation remains policy-eligible, the fingerprint is unchanged, confirmation and intent are unexpired, and adapter, credentials, action, audit, and idempotency environments all match.
- If the current generation differs or the intent's bound generation is no longer policy-eligible before that first broker-mutation reservation, the confirmation becomes invalid. The system must rescan, create a new immutable revision under the same environment-specific business action, and obtain a new confirmation. It may not edit the old intent or replace only its `trusted_run_id` or hashes.
- Once any leverage or submit mutation is `RESERVED`, dispatching, accepted, or unknown, a newer trusted generation cannot select a second revision or create a second submit action. Only reconciliation, separately authorized lineage-bound cancellation, and risk disposition may continue; refreshing or rescanning data cannot bypass the existing reservation.
- The final generation check and first mutation reservation form one linearizable boundary through a generation-publication fence shared with current-pointer replacement. A read-then-reserve sequence without such a fence is invalid; if the store cannot provide the fence or an equivalent compare-and-reserve guarantee, no reservation is allowed. Before every leverage/submit `dispatch_started` transition and between a completed leverage action and submit reservation, the executor reacquires the fence and rechecks the pointer/policy. Drift before `dispatch_started` records a typed invalidation and makes no broker call; drift after `dispatch_started` leaves the operation potentially sent and permits only reconciliation, separately authorized cancellation, and risk disposition.
- Cancellation, denial, timeout, or any same-environment field change invalidates the confirmation and requires a new fingerprint and confirmation. An environment change instead creates a new environment-specific business action and authorization lineage; it never supersedes or reuses the Demo action.

**Persistent evidence target.** The audit ledger must reconstruct trusted data -> scan decision -> intent -> gate -> actor confirmation -> idempotency reservation -> broker request -> broker response/reconciliation -> terminal outcome. Raw credentials and secrets are never stored.

**Failure, stop, and rollback.**

- `NOT_AUTHORIZED`, `CONFIRMATION_CANCELLED`, and `CONFIRMATION_EXPIRED` are distinct authorization outcomes and make no broker call.
- `BROKER_REJECTED` is an explicit business rejection and is not a transport failure.
- `BROKER_NOT_SENT` means the adapter can prove no request reached the broker; a retry may use the same key only while intent and confirmation remain valid.
- `BROKER_OUTCOME_UNKNOWN` means the request may have reached the broker; halt submission and reconcile by client order ID before any retry.
- `AUDIT_PRE_MUTATION_FAILED` prevents the affected broker call. `AUDIT_POST_MUTATION_FAILED` halts the executor and makes the affected leverage/submit/cancel result operationally unknown until reconciliation.
- Any production-environment evidence, audit gap, duplicate, unresolved unknown result, or environment mismatch forces a stop and remains in Stage 2.

**Promotion evidence.** Recorded demo exercises and deterministic fault tests must cover confirmation grant/cancel/expiry, concurrent generation publication at the pre-reservation/pre-dispatch fences, generation drift between leverage and submit, explicit rejection, proven not-sent failure, timeout-after-send ambiguity, process restart, duplicate confirmation, cross-environment rejection, idempotency corruption, pre/post-mutation audit failure, leverage reconciliation, lookup reconciliation, and gated cancellation. Every demo broker mutation and accepted order must be traceable end to end with no duplicate environment-specific business action. Stage 3 Design Frozen can be reached before these trials finish; Stage 3 Conformance Accepted and the Stage 2 promotion packet cannot.

**Ownership.** Issue #30 for the demo-stage contract and implementation PRs. No production side effect is part of issue #30.

### Stage 3 — Production execution design

**Purpose and safety invariant.** Freeze the domain, port, idempotency, audit, authorization, broker-configuration, order-lifecycle, fill/account-reconciliation, risk/control, and failure contracts that production implementation must obey. This stage has no production broker side effects.

**Current evidence.** PR #41 supplies the typed decision vocabulary and explicitly leaves `OrderIntent`, `BrokerPort`, the `run_once` split, and an execution ledger for a separate phase. The current concrete demo client and shadow ledger are inputs to the design, not the target abstractions.

**Allowed side effects.** Documentation changes, pure contract code in later PRs, local persistence tests, fake-adapter tests, and demo-only validation after the relevant Stage 2 PR.

**Prohibited side effects.** No production adapter activation, live credential use, production order/cancel request, or weakening of trusted-data and typed-decision gates.

**Input and decision handling.** The frozen contract below requires a canonical allowed `TrustDecision`, a non-`UNKNOWN` typed decision from the explicit v1 allowlist, a complete immutable intent, a matching environment-specific adapter, a valid authorization, and a durable audit/idempotency reservation. Absence of any item fails closed.

**Persistent evidence target.** Contract conformance tests, all orthogonal state-transition tests, audit round trips, restart recovery, partial/final fill and account-divergence matrices, risk/control transitions, and fake broker fault matrices. The reviewed architecture is the issue #30 design deliverable; implementation evidence is a later and separate milestone.

**Failure, stop, and rollback.** Any unresolved design ambiguity in authorization, broker outcome, audit ordering, idempotency, environment separation, reconciliation, or risk policy blocks Stage 4. Documentation can be reverted without broker rollback because production execution is not implemented.

**Promotion evidence.** Stage 3 Conformance Accepted requires all Stage 0-2 evidence; accepted 30-A through 30-F implementation evidence; contract tests for every invariant and state transition; fault injection; restart recovery; demo reconciliation; a complete promotion packet; production-disabled and credential-isolation proofs; and explicit operator approval of the open policy parameters. Merging this architecture PR alone is not that evidence. Conformance may hand off to 7-A, but production entry enablement remains blocked until issue #7 separately freezes and validates the protective-order/exit lifecycle described under Stage 4.

**Ownership.** Issue #30 freezes the contract. Issue #7 owns its production implementation.

**Stage 3 milestones.** These names are not interchangeable:

- **Stage 3 Design Frozen** means PR #42's architecture contract has been reviewed and merged. It freezes the design only; it does not claim that execution code, the audit ledger, `BrokerPort`, adapters, recovery, or reconciliation exists.
- **Stage 3 Conformance Accepted** means the relevant 30-A through 30-F implementations and their acceptance evidence have been accepted, including contract tests, fault injection, restart recovery, Demo reconciliation, and the promotion packet. Only this milestone, together with the accepted Stage 2 packet, can be a prerequisite for 7-A or Stage 4 implementation.

### Stage 4 — Production gated execution

**Purpose and safety invariant.** Execute a production order lifecycle only after Stage 3 Conformance Accepted and every lower-stage contract is satisfied. The default remains disabled and fail-closed. A reviewed `initial_stop` value is not broker-side protection.

**Current evidence.** Not implemented. Issue #7 remains open and owns production live order boundaries, lifecycle, fills, positions, balances, retries, cancellation, reconciliation, risk circuit breakers, and the final enablement gate.

**Allowed side effects.** An environment-isolated production process may read production account state while disabled. New production entry submission is reachable only after issue #7 has separately frozen and validated a protective-order/exit lifecycle, the production control is explicitly enabled, and the exact production intent passes its production-only confirmation and every other gate. Every side effect must use the applicable frozen entry or protective port, idempotency, and audit contracts.

**Prohibited side effects.**

- No production mutation from default config, demo config, a dry-run artifact, a missing/expired confirmation, shared demo credentials, or a consumer fallback.
- No broker adapter decision about data trust, strategy, entry eligibility, confirmation policy, or risk policy.
- No blind retry, untracked manual mutation, or continuation after a kill-switch/audit/reconciliation failure.
- No new production entry while the separately audited protective-order/exit lifecycle is absent or unaccepted. If Production v1 omits that lifecycle, Production v1 must explicitly prohibit new entry submission and remain read/reconcile-only.
- No claim that `initial_stop` created an exchange stop, attached order, or any other broker-side protection.

**Input and decision handling.** Re-run the same positive gates used in Stage 2 with `environment=PRODUCTION`, production-specific configuration and credentials, production risk limits, current account snapshot, and an operator confirmation that cannot be reused from demo. A production action derived from a signal previously reviewed in Demo may share only `signal_lineage_id`; it receives a new production-specific business action, audit correlation, authorization/confirmation lineage, audit stream, `mutation_action_id`, reservation/key/`clOrdId`, adapter, and credential boundary.

**Persistent evidence target.** Complete entry and protective-order lifecycle events, broker acknowledgements, fills, cancellations, positions, balances, realized/unrealized exposure, reconciliation results, risk-limit evaluations, operator actions, and termination/kill-switch events. The protective lifecycle must cover reduce-only semantics; protection quantity changes after partial entry fills; stop submit/amend/cancel/replace; independent stop authorization, idempotency, and audit; timeout/unknown outcomes; stop versus fill/cancel races; position reconciliation; and detection of manual/external protective orders.

**Failure, stop, and rollback.** Fail closed on any missing gate. Explicit rejection is terminal for that attempt; proven not-sent failures follow the frozen retry policy; unknown results force reconciliation; audit/reconciliation/risk failures halt new submissions. Rollback means disable production execution and reconcile existing broker state; it never assumes an accepted order can be undone locally.

**Promotion evidence.** There is no automatic promotion into Stage 4 and Stage 3 Design Frozen is insufficient. Issue #7 must define and approve the operator, configuration, credential, Demo evidence, fault-test, reconciliation, protective-lifecycle, and risk-control release packet. Until the protective lifecycle is frozen and its conformance evidence accepted, production entry stays `DISABLED`; the only permissible Production v1 alternative is to prohibit new entry submission explicitly.

**Ownership.** Issue #7 only.

## Stage 3 Core Design Contract

### Safety invariants

| ID | Invariant | Verification requirement |
|---|---|---|
| INV-1 | A new intent requires canonical trusted data allowed by the existing strict trading policy; no legacy/plain fallback is accepted. | Contract tests for missing, malformed, invalid, stale, failed, and allowed manifest/dataset combinations. |
| INV-2 | Control flow uses a non-`UNKNOWN` `EntryDecisionCode` from the explicit v1 scanner allowlist; `reason` and compatibility `action` never authorize execution. | Mismatch tests where text/action says enter but the typed code is ineligible. |
| INV-3 | `OrderIntent` is immutable and content-bound to data, strategy, decision, exact broker fields, environment, and expiry. | Frozen dataclass tests, canonical serialization round trip, and fingerprint mutation matrix. |
| INV-4 | Demo and production share only signal provenance: each environment has a distinct business action, audit correlation, authorization/confirmation lineage, audit stream, `mutation_action_id`, idempotency namespace/key, `clOrdId`, adapter, and credential boundary. Any cross-environment composition fails before reservation or network access. | Construction-time/runtime environment-mismatch tests plus cross-environment artifact-injection tests for every listed identity and boundary. |
| INV-5 | One environment-specific business action has one durable submit key across same-environment intent/schema revisions, retries, duplicate confirmation, deployment, and restart. Supersession is allowed only between same-environment revisions of that same action and only before any mutation reservation; supersession and first reservation serialize on one atomic action-selection fence. Environment change creates another action and never supersedes the first. | Same-environment exact-reuse/pre-reservation-supersession, supersession-versus-reservation race, cross-environment non-supersession, cross-schema identity, unique-reservation, restart, collision, and concurrent-submit tests. |
| INV-6 | Intent, gate, confirmation, and the action-specific leverage/submit/cancel reservation are durable before each broker mutation. | Ordered-call and injected pre/post-mutation audit failure tests. |
| INV-7 | A result that may have reached the broker is `UNKNOWN` until lookup/reconciliation proves otherwise; it is never blindly retried. | Timeout-after-send, restart, lookup, and duplicate-broker-response tests. |
| INV-8 | Broker adapters translate typed commands/results only; they do not reimplement data trust, strategy, confirmation, or risk policy. | Dependency-boundary and fake-adapter conformance tests. |
| INV-9 | Secrets never enter intents, logs, reports, exceptions, or audit events. | Redaction and forbidden-field tests. |
| INV-10 | A risk-reducing cancellation is tied to known order/intent lineage, uses an independent `cancel_action_id`/gate/key/event chain, and keeps command acknowledgement separate from order effect. | Unknown-order rejection, one-nonterminal-action, rejected-new-action, duplicate cancel, data-failure cancel, fill/expiry race, and ambiguity tests. |
| INV-11 | Broker acceptance is not a fill; order, unique fills, position, and balance advance only from authoritative reconciled evidence. | Partial/final fill, cancel-fill race, duplicate trade ID, restart, and divergence tests. |
| INV-12 | Execution control is disabled by default, and any unknown lifecycle/account state or active kill switch blocks new entries. | Disabled/armed/enabled/halted transition and fail-closed risk-gate tests. |
| INV-13 | Before the first broker-mutation reservation for an entry action, the intent's exact trusted generation must still be current and allowed by the current strict policy; a generation-publication fence makes that check and reservation linearizable. After any leverage/submit reservation, a new generation cannot authorize submit dispatch or a second submit action. | Concurrent pointer publication and policy-change tests at first reservation, pre-dispatch, and the leverage-to-submit boundary. |
| INV-14 | `initial_stop` is review provenance, not broker-side protection. Production entry remains disabled until issue #7 validates an independently authorized, idempotent, audited, reconciled protective-order/exit lifecycle. | Default-disabled reachability tests and protective submit/amend/cancel/replace, partial-fill sizing, ambiguity, race, reconciliation, and external-order tests. |

### `OrderIntent`

`OrderIntent` is the immutable domain statement “submit this exact long-entry limit order in this exact environment because this exact trusted scan decision is eligible.” It is not an authorization, broker request/response, fill, position, retry record, credential container, or attached-stop implementation.

The v1 shape is deliberately limited to the current long-only scanner path:

```python
class ExecutionEnvironment(str, Enum):
    DEMO = "demo"
    PRODUCTION = "production"


@dataclass(frozen=True)
class OrderIntent:
    schema_version: int
    signal_lineage_id: str
    business_action_id: str
    intent_id: str
    audit_correlation_id: str
    environment: ExecutionEnvironment
    symbol: str
    side: Literal["buy"]
    order_type: Literal["limit"]
    size: str
    limit_price: str
    td_mode: Literal["isolated"]
    pos_side: Literal["long"]
    leverage: int
    initial_stop: str
    source_observation_id: str
    signal_time_ms: int
    decision_code: EntryDecisionCode
    trusted_run_id: str
    observed_at_ms: int
    data_content_sha256: tuple[tuple[str, str], ...]
    strategy_release_id: str
    strategy_name: str
    strategy_config_sha256: str
    second_pullback_wait_bars: int
    created_at_ms: int
    expires_at_ms: int
```

Field semantics:

- `schema_version` freezes canonical serialization and fingerprint rules. Readers reject unknown versions; writers emit only the current version.
- `signal_lineage_id` is created when a strategy first accepts a signal from symbol, strategy/rule identity, the accepting config hash, and `signal_time_ms`. That persisted lineage is reused by later scans and decision maturation; data-generation, intent-schema, process, and deployment changes cannot mint a new lineage for the same accepted signal.
- `business_action_id` is the stable identity of “submit the entry for this accepted signal lineage in this environment.” V1 derives it from `environment | SUBMIT_ENTRY | signal_lineage_id`. Schema version, exact price, size, data generation, decision maturation, expiry, and creation time are deliberately excluded so same-environment rescans, deployments, or intent revisions cannot create a second action. The same signal in Demo and Production creates two different business actions.
- `intent_id` is `oi1_` plus the deterministic SHA-256 fingerprint of every v1 control field: schema, signal/action identity, environment, exact order/risk fields, source observation, decision, trusted generation and hashes, observed time, exact strategy release/config identity, and expiry. `intent_id` itself, derived `audit_correlation_id`, `created_at_ms`, and presentation-only diagnostics are excluded. Recreating the same exact revision returns the existing persisted intent rather than a new ID.
- `audit_correlation_id` must equal the environment-specific `business_action_id` in v1 and remains stable only across same-action intent revisions, gates, confirmations, retries, broker operations, reconciliation, cancellation, and terminal events. It is never reused across environments.
- `environment` is part of both business identity and the fingerprint/idempotency namespace. Stage 1-2 factories permit `DEMO` only; Stage 4 adds `PRODUCTION` through issue #7. Cross-environment intents may share `signal_lineage_id` for source correlation, but they have no supersession, reuse, alias, or reservation-migration relationship.
- `symbol` uses the resolved OKX `instId`. V1 fixes `side=buy`, `order_type=limit`, `td_mode=isolated`, `pos_side=long`, and a non-reduce-only entry. Market, short, arbitrary sell, and exit intents require a later version.
- `size`, `limit_price`, and `initial_stop` are canonical decimal strings, not binary floats. The exact rounded size/price must exist before confirmation. `initial_stop` is review provenance only: it does not submit, represent, or prove any broker-side protective order. Issue #7 must separately freeze and validate the protective-order/exit lifecycle before production entry can be enabled.
- `decision_code` must be `SIGNAL_CONFIRMED` or `SECOND_PULLBACK_LIMIT_READY` and must derive `READY`. `UNKNOWN` and text-derived classification are invalid.
- `source_observation_id` binds the exact canonical Stage 0 record. Compatibility action/reason text never authorizes construction; the factory revalidates the observation's canonical result fingerprint and strict typed outcome before using it.
- `trusted_run_id` is required and identifies the exact 32-hex generation used to create the intent. `data_content_sha256` is a unique sorted interval/hash tuple covering every effective interval and at least `5m/15m/1h`. Missing hashes or a plain legacy bundle cannot create an intent. Neither field may be replaced in place when the current generation advances.
- `strategy_release_id` is mandatory and fingerprinted. The factory calls `StrictStrategyReleaseResolver` with the exact ID, MU v1 rule, and exact symbol; there is no by-name/newest/current-pointer or missing-approval fallback.
- `strategy_name` is the registered strategy identity (currently `baseline`); `strategy_config_sha256` is the schema-wrapped approved-release config hash. Stage 0's earlier `canonical_payload_sha256(StrategyConfig)` evidence uses a different canonical encoding, so the factory explicitly decodes the release config, recomputes that Stage 0 identity, and requires an exact match before storing the release-native hash in the intent.
- `second_pullback_wait_bars` is copied from the exact approved configuration and fingerprinted so a reader can independently validate the lifecycle rather than trusting factory history.
- `expires_at_ms` is derived only by the factory from the approved release's scanner lifecycle: `signal_time_ms + second_pullback_wait_bars * 15 minutes`. The boundary is exclusive, so construction at or after that millisecond fails. Strict readers recompute and require the same formula. Expiry is fingerprinted and never changes `business_action_id`; callers do not supply a freshness duration or expiry.

After creation, every field is immutable. Within one environment-specific business action, a change to any fingerprinted control field creates a new intent revision. An environment change is not a revision; 30-B rejects it and constructs Demo only. The pure 30-B classifier returns create, exact reuse, pre-reservation supersede, or post-reservation conflict; exact reuse returns the existing object and therefore preserves its first creation time. It has no storage or reservation side effect. 30-C must execute supersession and first reservation through one durable atomic business-action selection fence. Once a leverage or submit mutation is reserved or observed, another revision cannot acquire a new submit key; replacement requires an explicitly designed cancel/replace transition.

Trusted-generation drift follows the same immutable boundary with an additional fail-closed gate. Before the first mutation reservation, a current-generation change or loss of strict-policy eligibility invalidates the old confirmation and requires a rescan, a new same-environment revision, and a new confirmation. After a leverage/submit reservation or observation, no refreshed generation or revision may create a second submit action; only reconciliation, separately authorized cancellation, and risk disposition may proceed.

### `BrokerPort`

The port translates an already-authorized exact intent into environment-specific broker operations. It mirrors the smallest current/required entry surface: leverage configuration, submit, cancel, order lookup, account snapshot, and fills for reconciliation. It does not claim to model protective stop/exit submit or amendment; issue #7 must separately freeze the protective command/port extension and its conformance evidence before production entry enablement.

```python
class NotSentFailureCode(str, Enum):
    LOCAL_VALIDATION_FAILED = "local_validation_failed"
    TRANSPORT_NOT_SENT = "transport_not_sent"


class UnknownFailureCode(str, Enum):
    TRANSPORT_OUTCOME_UNKNOWN = "transport_outcome_unknown"
    MALFORMED_RESPONSE = "malformed_response"
    EMPTY_RESPONSE = "empty_response"


@dataclass(frozen=True)
class SubmitAccepted:
    client_order_id: str
    broker_order_id: str
    accepted_at_ms: int


@dataclass(frozen=True)
class SubmitRejected:
    client_order_id: str
    broker_code: str
    rejected_at_ms: int
    safe_message: str


@dataclass(frozen=True)
class SubmitNotSent:
    client_order_id: str
    failure_code: NotSentFailureCode
    occurred_at_ms: int


@dataclass(frozen=True)
class SubmitUnknown:
    client_order_id: str
    failure_code: UnknownFailureCode
    occurred_at_ms: int


BrokerSubmitResult = SubmitAccepted | SubmitRejected | SubmitNotSent | SubmitUnknown


class BrokerPort(Protocol):
    environment: ExecutionEnvironment

    def configure_leverage(
        self,
        intent: OrderIntent,
        *,
        idempotency_key: str,
    ) -> BrokerLeverageResult: ...

    def submit_order(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
    ) -> BrokerSubmitResult: ...

    def cancel_order(self, command: BrokerCancelCommand) -> BrokerCancelResult: ...
    def get_order(self, client_order_id: str) -> BrokerLookupResult: ...
    def get_account_snapshot(self) -> BrokerAccountSnapshot: ...
    def list_fills(self, *, since_ms: int) -> tuple[BrokerFill, ...]: ...
```

Result variants are discriminated types, not one optional-field bag:

- `SubmitAccepted` requires both broker and client order IDs.
- `SubmitRejected` means the broker explicitly rejected the business request. Its code drives policy; the sanitized text is diagnostic.
- `SubmitNotSent` accepts only `NotSentFailureCode.LOCAL_VALIDATION_FAILED` or `TRANSPORT_NOT_SENT` and is allowed only when the adapter can prove the request did not reach the broker. If that proof is unavailable, the result is `SubmitUnknown`.
- `SubmitUnknown` accepts only `UnknownFailureCode.TRANSPORT_OUTCOME_UNKNOWN`, `MALFORMED_RESPONSE`, or `EMPTY_RESPONSE` and always enters reconciliation. A timeout, dropped response, malformed success response, empty broker data, or post-mutation audit failure cannot be called rejection or success.
- Cancel and lookup results use equivalent typed accepted/rejected/not-sent/unknown or found/not-found/failed variants; they may not collapse ambiguity into a Boolean.

Supporting port records are also typed:

- `BrokerCancelCommand` requires the originating `business_action_id` and `intent_id`, known order lineage/client order ID, a stable `cancel_action_id`, cancel idempotency key, and a closed cancellation reason/policy version such as signal expiry, an explicitly designed cancel/replace transition, operator request, or risk stop.
- `BrokerLeverageResult` uses already-configured, configured, explicit-rejected, proven-not-sent, and unknown variants. Leverage configuration is a separate authorized mutation; `submit_order()` may not hide it.
- `BrokerCancelResult` uses accepted, explicit-rejected, proven-not-sent, and unknown variants equivalent to submission.
- `BrokerLookupResult` uses found, definitive-not-found, and lookup-failed/unknown variants. Eventual-consistency uncertainty is not definitive absence.
- `BrokerAccountSnapshot` requires environment, observation time, balances, positions, and open orders.
- `BrokerFill` requires broker trade/order IDs, client order ID, side, size, price, fee, and broker timestamp.

Environment isolation is structural:

- `DemoBrokerAdapter` and `ProductionBrokerAdapter` are different composition roots with distinct configuration and credential providers.
- Each adapter exposes one constant `environment` and rejects a mismatched intent before network access.
- A Demo business action, audit correlation, confirmation/reservation lineage, mutation action, idempotency key, or `clOrdId` is invalid in Production even if every order field and `signal_lineage_id` matches; the reverse is equally invalid.
- Cross-environment artifacts fail with `ENVIRONMENT_MISMATCH` before reservation or network access. No adapter aliases or migrates them into its own environment.
- `ProductionBrokerAdapter` belongs only to issue #7 and remains absent/disabled during issue #30 implementation.

Adapters may validate wire encoding, instrument constraints, signatures, and broker responses. They may not decide whether data is trusted, whether a strategy should enter, whether a human authorized the intent, or whether a risk policy permits it.

### Idempotency contract

**Business meaning.** Every broker mutation has a stable, environment-specific `mutation_action_id`. For `SUBMIT_ENTRY` it is the environment-specific `business_action_id`; for `CONFIGURE_LEVERAGE` it binds that action plus exact leverage/margin mode; for `CANCEL_ORDER` it is `cancel_action_id`, derived from business action, known order lineage, typed cancellation reason, and policy version. One semantic mutation keeps one key across retries within its environment. Leverage, submit, cancel, Demo, and Production never share a key.

**Generation.**

1. Derive the operation-specific `mutation_action_id` above, then canonically serialize `environment | operation | mutation_action_id`. Intent/audit schema versions never enter the business key.
2. Store the full SHA-256 digest as the domain `idempotency_key`.
3. For OKX `SUBMIT_ENTRY`, derive `clOrdId = "OD" + first_20_upper_hex(SHA256(idempotency_key))`. This preserves the current 22-character bot-order pattern while expanding the domain key beyond the current symbol/time/price hash. Leverage and cancel keep their full domain keys in the ledger and use broker-native references rather than inventing an order ID.
4. Persist the full key, `clOrdId` when applicable, operation, environment, `mutation_action_id`, `business_action_id`, and the selected intent fingerprint together. A truncated `clOrdId` collision with a different full key is `IDEMPOTENCY_CONFLICT` and stops execution; the adapter never invents a replacement silently.

The v1 signal-lineage and business-action derivations are cross-schema contracts. Within one environment, a future algorithm version must first migrate/alias old and new schema identities under one uniqueness boundary and prove that existing accepted/unknown broker lineages reuse their original reservation and `clOrdId`. Such migration never aliases Demo and Production identities. Until a same-environment migration is durable and verified, the executor fails closed rather than computing a new identity.

**Storage and atomicity.** Before a broker call, a durable store must atomically:

- insert or verify the immutable intent;
- lock or compare-and-swap one business-action selection fence keyed by `(environment, business_action_id)`, containing the selected `intent_id`/fingerprint, a monotonic selection version, and whether any broker mutation has been reserved;
- reserve a unique `(environment, operation, mutation_action_id)` row containing the idempotency key, business action/order lineage, and selected intent fingerprint, while atomically flipping that same action-selection fence to mutation-reserved;
- append the gate, confirmation, and action-specific `broker.<operation>.requested` audit event.

Before reservation, `intent.superseded` uses that same fence in one transaction: compare the expected selection version, prove the entire action remains mutation-unreserved, update the selected intent/fingerprint, increment the version, and append the supersession event. The first leverage/submit reservation compares the same version and selected fingerprint, flips the fence to mutation-reserved, creates the operation reservation, and appends the request event in one transaction. A supersession/reservation race has exactly one winner; the loser reloads the durable action state and fails closed.

The 30-C implementation uses Python's standard-library `sqlite3` with schema version 1. Callers must provide an explicit database path whose parent already exists; the store does not create directories or fall back to JSONL. Every connection requires WAL mode, `synchronous=FULL`, foreign-key enforcement, and a bounded busy timeout. Mutations use `BEGIN IMMEDIATE`, so immutable intent persistence, the action-selection compare-and-swap, unique mutation reservation, and ordered audit append share one commit or rollback together. Startup rejects unknown versions, missing or extra tables, and schema-definition drift. The current plain `ShadowExecutionLedger` JSONL remains a separate observation log and does not satisfy this contract.

Reservation lifecycle state is persisted as a closed compare-and-swap transition set. `idempotency.reservation_state_changed` records the operation, mutation action, full key, and old/new states in the same transaction as the reservation update. This is local execution-control evidence only: 30-C imports no Broker or application layer and does not interpret transport responses.

**Retry and restart.**

- Duplicate creation loads an existing intent only when environment, business action, and full fingerprint are identical. Duplicate confirmation or submit requests load only the reservation and outcome of that same environment-specific action.
- A new same-environment intent revision may supersede the selected unreserved revision under the same business action and future submit key only through the action-selection fence. If the competing first reservation wins, or a leverage/submit mutation is already reserved or observed for any revision, supersession fails and the mismatch is terminal until an explicit cancel/replace contract resolves it; a new trusted generation does not reopen this boundary.
- An environment change creates a different business action, mutation action, key, and `clOrdId`; it has no supersession or reservation-migration path. Any attempt to compose old-environment artifacts with the new environment fails as `ENVIRONMENT_MISMATCH` before network access.
- An accepted or rejected attempt is not submitted again.
- `SubmitNotSent` may retry with the same key only if policy allows and the same intent/confirmation remains valid.
- `SubmitUnknown` never retries before `get_order(clOrdId)` reconciliation. Process restart resumes from the persisted reservation and state.
- If lookup finds the order, append the reconciled broker identity and continue from accepted. If lookup cannot prove absence, remain unknown. A retry after a proven absence still uses the same key.
- After reservation, the same key with a different fingerprint or action is a terminal conflict. If a persisted key or `clOrdId` is discovered to map to facts from another environment, that is idempotency corruption requiring `IDEMPOTENCY_CONFLICT` investigation in addition to the immediate `ENVIRONMENT_MISMATCH` stop. Before reservation, audited supersession may replace only a same-environment revision of the same business action while preserving that action and future key.
- At most one cancel action for an order lineage may be nonterminal (`RESERVED`, `DISPATCHING`, `ACKNOWLEDGED`, or `UNKNOWN`). `NOT_SENT_FAILED` retries the same `cancel_action_id`/key. After an explicit `REJECTED` and proof the order remains live, a newly gated reason/policy version may create a new `cancel_action_id` and reservation; an acknowledged or unknown cancel blocks any parallel/new cancel action.

### Audit ledger contract

The execution audit ledger is append-only and authoritative for execution control. It is separate from both the trusted refresh run log and the current manual shadow observation ledger.

Every event has a typed universal header:

| Field | Requirement |
|---|---|
| `schema_version` | Required integer; unknown versions fail closed. |
| `event_id` | Globally unique and immutable. |
| `event_type` | Closed enum; free text cannot select transitions. |
| `sequence` | Strictly increasing within `audit_correlation_id`. |
| `occurred_at_ms` | Required event time from an injected clock. |
| `audit_correlation_id` | Required root correlation selected by the closed event-header family below. |
| `actor_kind` / `actor_id` | Required typed system/operator identity. Confirmation uses the authenticated operator identity. |

Correlation fields use a discriminated header union rather than optional common fields:

| Header family | Applies to | Required identity fields |
|---|---|---|
| `ScanEventHeader` | data gate, scan completed/failed, and no-action observations | `scan_id`, typed observation scope, and `audit_correlation_id=scan_id`; it has no business action, execution environment, or intent field |
| `ControlEventHeader` | environment/account snapshots, execution enablement, kill switch, system audit failure, and control-run termination | `control_run_id`, `environment`, and `audit_correlation_id=control_run_id`; it has no fabricated scan, action, or intent lineage |
| `ActionEventHeader` | accepted signal lineage and business-action events | `source_scan_id`, `signal_lineage_id`, `business_action_id`, `environment`, and `audit_correlation_id=business_action_id` |
| `IntentEventHeader` | an exact immutable intent revision, gate, and confirmation | every `ActionEventHeader` field plus `intent_id`, `intent_fingerprint`, and `decision_code` |
| `BrokerMutationEventHeader` | leverage, submit, cancel, lookup, lifecycle, fill, and action reconciliation | every `IntentEventHeader` field plus operation, `mutation_action_id`, idempotency key, and order-lineage fields |

`intent.created` is the explicit link from `source_scan_id` to `business_action_id`. This preserves a complete data -> scan -> action chain without fabricating an action ID for data-blocked, failed, `WAIT`, or signal-stage `BLOCK` observations. Event-specific typed payloads avoid a large optional-field schema:

| Event type | Required payload |
|---|---|
| `data.gate_evaluated` | trusted `run_id`, interval hashes, policy name, allowed flag, typed health reason |
| `scan.completed` | scan ID, strategy/config hash, `decision_code`, disposition/stage, result fingerprint |
| `scan.failed` | scan ID, typed failure code, sanitized exception type/message |
| `intent.created` | source scan ID, signal lineage ID, business action ID, complete intent fingerprint, and immutable execution/provenance fields |
| `intent.reused` | environment, business action ID, existing intent ID/full fingerprint, and duplicate source scan ID; all identity and fingerprint fields must match exactly |
| `intent.superseded` | environment, one business action ID, old/new intent IDs, action-selection version, and proof that both are same-environment revisions and the event/update atomically won the shared fence while no broker-mutation reservation existed anywhere in that action |
| `gate.evaluated` | gate code, allow/deny, intent/confirmation expiry snapshot, account/risk snapshot reference |
| `gate.invalidated` | typed invalidation code, expected/observed generation IDs and pointer tokens, policy result, reservation/dispatch phase, and proof no later mutation dispatch occurred |
| `confirmation.granted` | `confirmed_by`, `confirmed_at_ms`, `confirmation_expires_at_ms`, `decision_code`, intent fingerprint |
| `confirmation.denied` | operator, time, typed denial code |
| `confirmation.cancelled` | operator, time, typed cancellation code |
| `confirmation.expired` | expiry and observation time |
| `idempotency.reserved` | operation, mutation action ID, business action/order lineage, full key, broker client ID when applicable, selected intent fingerprint |
| `idempotency.reservation_state_changed` | operation, mutation action ID, full key, and the closed old/new reservation states |
| `broker.leverage.requested` | leverage key, symbol, leverage, margin mode, adapter/environment |
| `broker.leverage.configured` | leverage key, configured/already-configured result, observed time |
| `broker.leverage.rejected` | leverage key, broker code, sanitized response fingerprint |
| `broker.leverage.not_sent` | leverage key, typed not-sent code, retry eligibility |
| `broker.leverage.unknown` | leverage key, typed ambiguity code, reconciliation required |
| `broker.submit.requested` | full key, sanitized request fingerprint, adapter/environment |
| `broker.mutation.dispatch_started` | operation, full key, attempt number, atomic transition from reserved to in-flight immediately before transport |
| `broker.submit.accepted` | client ID, broker order ID, accepted time, sanitized response fingerprint |
| `broker.submit.rejected` | client ID, broker code, sanitized message/response fingerprint |
| `broker.submit.not_sent` | client ID, typed failure code, retry eligibility |
| `broker.submit.unknown` | client ID, typed ambiguity code, reconciliation required |
| `broker.lookup.observed` | client ID, typed lookup result, broker order state/reference |
| `broker.cancel.*` | target order lineage, `cancel_action_id`, cancel key, typed command state and effect state |
| `broker.order.observed` | client/broker order IDs, typed lifecycle state and transition origin (including audited `EXTERNAL_CANCEL`), cumulative size, observation time |
| `broker.fill.observed` | unique trade ID, order lineage, size, price, fee, broker time |
| `account.snapshot_observed` | control run ID, snapshot ID, environment, balances, positions, open orders, observation time |
| `fills.reconciled` | expected/observed fill totals, sync state, divergence code |
| `position.reconciled` | expected/observed position, sync state, divergence code |
| `balance.reconciled` | expected/observed balance impact, sync state, divergence code |
| `risk.action_gate_evaluated` | business action/intent, policy/config hash, account snapshot ID, risk state, typed reasons |
| `risk.control_gate_evaluated` | control run ID, environment, policy/config hash, account snapshot ID, risk state, typed reasons |
| `execution_control.changed` | control run ID, old/new disabled/armed/enabled/halted state, actor, typed reason |
| `audit.action_write_failed` | action/intent lineage, pre/post-side-effect phase, typed storage failure |
| `audit.control_write_failed` | control run ID, environment, typed storage failure and emergency-journal reference |
| `action.terminated` | business action/intent, typed terminal reason, last known broker/reconciliation state |
| `control.run_terminated` | control run ID, environment, typed stop reason, last safe account snapshot |

`intent.reused` never means “equivalent enough”: it records a duplicate creation of the same full fingerprint in the same environment-specific business action. `intent.superseded` can connect only two revisions of one environment-specific business action while the entire action has no broker-mutation reservation, and its selection update/event must atomically win the same fence used by first reservation; checking only the old revision or racing an unlocked reservation is insufficient. Neither event can connect Demo and Production; cross-environment source correlation uses `signal_lineage_id` outside the supersession/reuse relationship.

Credentials, signatures, secret headers, passphrases, and unsanitized private payloads are forbidden. Raw broker responses may be retained only in an access-controlled sanitized artifact referenced by hash; the ledger stores the typed outcome and safe provenance needed for replay.

**Safety ordering.**

1. Persist `intent.created` and the current account/risk snapshot references.
2. Persist the positive gates and exact confirmation.
3. Under the generation-publication fence, revalidate the exact generation/policy and, if leverage is not already proven correct, atomically reserve `CONFIGURE_LEVERAGE` and persist `broker.leverage.requested`. Reacquire the fence immediately before transport; only if the generation still matches may the executor atomically mark the attempt in-flight, append `broker.mutation.dispatch_started`, release the fence, call the adapter, and persist its typed result. A rejected or unknown leverage outcome prevents submission, and an unknown result must be reconciled from account state.
4. After leverage is durably known correct, reacquire the generation-publication fence and recheck generation/policy before atomically reserving `SUBMIT_ENTRY`. If drift occurred after leverage reservation or execution, record the typed invalidation, do not create/dispatch a submit reservation, and permit only leverage/account reconciliation, separately authorized cancellation, or risk disposition. Otherwise persist `broker.submit.requested` under the same fence.
5. Reacquire the generation-publication fence immediately before submit transport. Only if the exact generation/policy still passes may the executor atomically transition the submit attempt from reserved to `SUBMITTING`, append `broker.mutation.dispatch_started`, release the fence, call `submit_order()`, and persist its typed result. Drift before dispatch makes no broker call; drift after dispatch makes the outcome reconciliation-only.
6. Apply the same write-ahead rule to cancellation and every other broker mutation.
7. On restart, a reserved attempt with no dispatch-started event is known not to have entered transport and may resume only after current gates pass. Any in-flight/`SUBMITTING` attempt without a terminal result is `UNKNOWN` and must reconcile before retry.
8. If a required prewrite fails, make no corresponding broker call. If a result write fails after a call, stop all new submissions, classify `AUDIT_POST_MUTATION_FAILED` plus the affected operation's unknown state, and reconcile using the prewritten key/client ID. Never “roll back” by forgetting the possible mutation.

### Orthogonal state and failure model

Authorization, broker configuration, submission, cancellation command, post-acceptance lifecycle, fill/account synchronization, risk, execution control, audit, and reconciliation are separate state dimensions:

```text
AuthorizationState = PENDING | GRANTED | DENIED | CANCELLED | EXPIRED
LeverageState      = NOT_REQUIRED | PENDING | CONFIGURING | CONFIGURED
                     | INVALIDATED | REJECTED | NOT_SENT_FAILED | UNKNOWN
SubmissionState    = NOT_STARTED | RESERVED | SUBMITTING | ACCEPTED
                     | INVALIDATED | REJECTED | NOT_SENT_FAILED | UNKNOWN
CancelCommandState = NOT_REQUESTED | RESERVED | DISPATCHING | ACKNOWLEDGED
                     | REJECTED | NOT_SENT_FAILED | UNKNOWN
CancelEffectState  = NONE | PENDING | CANCELLED | SUPERSEDED_BY_FILL
                     | SUPERSEDED_BY_EXPIRY | UNKNOWN
OrderLifecycleState = NOT_OBSERVED | OPEN | PARTIALLY_FILLED | FILLED
                      | CANCEL_PENDING | CANCELLED | EXPIRED | UNKNOWN
FillSyncState       = NOT_REQUIRED | PENDING | IN_SYNC | DIVERGED | UNKNOWN
PositionSyncState   = NOT_REQUIRED | PENDING | IN_SYNC | DIVERGED | UNKNOWN
BalanceSyncState    = NOT_REQUIRED | PENDING | IN_SYNC | DIVERGED | UNKNOWN
RiskState           = CLEAR | BLOCK_NEW_ENTRIES | CANCEL_OPEN_ENTRIES | HALT
ExecutionControlState = DISABLED | ARMED | ENABLED | HALTED
AuditState         = READY | PRE_MUTATION_FAILED | POST_MUTATION_FAILED
ReconcileState     = NOT_REQUIRED | PENDING | RESOLVED | FAILED
```

Authorization transitions:

| From | Event/guard | To | Broker effect |
|---|---|---|---|
| `PENDING` | valid grant for exact unexpired intent | `GRANTED` | None |
| `PENDING` | operator denies | `DENIED` | None |
| `PENDING` or `GRANTED` | operator cancels before submit | `CANCELLED` | None |
| `PENDING` or `GRANTED` | confirmation or intent expires | `EXPIRED` | None |
| Any terminal authorization state | duplicate/late grant | unchanged; audit duplicate | None |

Submission transitions:

| From | Event/guard | To | Required action |
|---|---|---|---|
| `NOT_STARTED` | valid gates plus atomic reservation | `RESERVED` | Persist request event |
| `RESERVED` | generation/policy/authorization invalidates before `dispatch_started` | `INVALIDATED` | Persist `gate.invalidated`; make no submit call; retain reservation and terminate the action for new-entry purposes |
| `RESERVED` | atomic `dispatch_started` prewrite succeeds | `SUBMITTING` | Persist in-flight state before transport; no second caller may submit |
| `SUBMITTING` | typed acceptance | `ACCEPTED` | Persist broker IDs |
| `SUBMITTING` | explicit broker rejection | `REJECTED` | Terminal for this attempt |
| `SUBMITTING` | proven no-send failure | `NOT_SENT_FAILED` | Same-key retry policy only |
| `SUBMITTING` | any delivery/result ambiguity | `UNKNOWN` | Set reconciliation pending |
| `UNKNOWN` | lookup proves order exists | `ACCEPTED` | Persist reconciled identity |
| `UNKNOWN` | authoritative lookup after the broker consistency window proves absence | `NOT_SENT_FAILED` | Persist definitive absence; same-key retry policy may apply |
| `UNKNOWN` | lookup cannot prove outcome | `UNKNOWN` | Halt; do not retry |
| `NOT_SENT_FAILED` | same intent/confirmation still valid and retry policy allows | `RESERVED` | Keep the same key, increment attempt number, and append retry-scheduled evidence |

Leverage configuration follows `PENDING` (with a durable reservation) -> configuring -> configured/invalidated/rejected/not-sent/unknown. A pre-dispatch generation/policy invalidation moves the pending reserved leverage action to `INVALIDATED` with no broker call. If drift is observed after leverage dispatch or configuration, reconcile leverage/account state but do not reserve or dispatch submit. `submit_order()` is reachable only from `LeverageState.CONFIGURED` or `NOT_REQUIRED` with a current account snapshot, an unchanged generation fence, and every other current gate proving the requested leverage and margin mode already hold. `INVALIDATED` is terminal for new-entry progression and never retries under the reserved action.

Each `cancel_action_id` owns one `CancelCommandState` and one `CancelEffectState`. Command delivery/acknowledgement is orthogonal to the order effect; writing a request event alone never changes `OrderLifecycleState`:

| Command from | Event/guard | Command to | Effect state / required action |
|---|---|---|---|
| `NOT_REQUESTED` | valid lineage/gate plus atomic cancel reservation | `RESERVED` | Persist `broker.cancel.requested`; order lifecycle is unchanged |
| `RESERVED` | atomic `dispatch_started` prewrite succeeds | `DISPATCHING` | Persist in-flight state before cancel transport |
| `DISPATCHING` | broker acknowledges the cancel command | `ACKNOWLEDGED` | `PENDING` until order/fill evidence proves the effect |
| `DISPATCHING` | broker explicitly rejects the cancel command | `REJECTED` | `NONE`; keep authoritative live/fill lifecycle |
| `DISPATCHING` | proven no-send failure | `NOT_SENT_FAILED` | `NONE`; same-key retry policy may apply |
| `DISPATCHING` | delivery/result ambiguity | `UNKNOWN` | `UNKNOWN`; set order lifecycle unknown and never blind retry |
| `NOT_SENT_FAILED` | same command/gates remain valid and retry policy allows | `RESERVED` | Keep the same cancel key and increment attempt number |
| `ACKNOWLEDGED` | broker still reports cancellation pending | `ACKNOWLEDGED` | `PENDING`; lifecycle may be `CANCEL_PENDING` |
| `ACKNOWLEDGED` | broker confirms cancelled and final raced fills are below intent size | `ACKNOWLEDGED` | `CANCELLED`; lifecycle becomes `CANCELLED` |
| `ACKNOWLEDGED` | authoritative fills reach intent size before cancel effect | `ACKNOWLEDGED` | `SUPERSEDED_BY_FILL`; lifecycle becomes `FILLED` |
| `ACKNOWLEDGED` | broker order expires before cancel effect | `ACKNOWLEDGED` | `SUPERSEDED_BY_EXPIRY`; lifecycle becomes `EXPIRED` |
| `ACKNOWLEDGED` | command/effect evidence becomes contradictory or disappears | `UNKNOWN` | `UNKNOWN`; halt and reconcile |
| `UNKNOWN` | broker cancel history proves acknowledgement and order remains cancel-pending | `ACKNOWLEDGED` | `PENDING`; resume lifecycle reconciliation |
| `UNKNOWN` | broker history proves acknowledgement plus cancelled/fill/expiry effect | `ACKNOWLEDGED` | Set `CANCELLED`, `SUPERSEDED_BY_FILL`, or `SUPERSEDED_BY_EXPIRY` from authoritative evidence |
| `UNKNOWN` | authoritative history proves command was rejected | `REJECTED` | `NONE`; restore lifecycle from order/fill evidence |
| `UNKNOWN` | authoritative history proves command was not sent | `NOT_SENT_FAILED` | `NONE`; same-key retry policy may apply |
| `UNKNOWN` | history cannot prove command/effect | `UNKNOWN` | `UNKNOWN`; halt new entries and keep reconciling |

`REJECTED` means only an explicit broker rejection. A fill or expiry racing an acknowledged cancel is represented by `CancelEffectState.SUPERSEDED_BY_FILL` or `SUPERSEDED_BY_EXPIRY`, never by `REJECTED`. Those two names describe a cancel-effect race, not intent supersession, and never select another intent. A later cancel after rejection is a new, separately gated `cancel_action_id`; it is not a retry or mutation of the rejected command.

Post-acceptance order lifecycle:

Lifecycle guards are mutually exclusive and evaluated in this order: full fill -> one authoritative terminal broker state (`CANCELLED` or `EXPIRED`) -> cancellation pending -> nonterminal live order with partial/no fill. Conflicting terminal broker states or fill totals are `UNKNOWN` rather than resolved by table order.

| From | Broker evidence/event | To | Required action |
|---|---|---|---|
| `NOT_OBSERVED` | authoritative unique fills total equals intent size | `FILLED` | Persist order/fills and reconcile final position/balance |
| `NOT_OBSERVED` | broker reports only `cancelled`, final raced fills are known and below intent size, and origin is `CancelEffectState.CANCELLED` or an audited `EXTERNAL_CANCEL` | `CANCELLED` | Persist terminal order/fills and reconcile position/balance |
| `NOT_OBSERVED` | broker reports only `expired`, final fills are known and below intent size, and cancel effect is `NONE` (no active cancel) or `SUPERSEDED_BY_EXPIRY` | `EXPIRED` | Persist terminal order/fills and reconcile position/balance |
| `NOT_OBSERVED` | `CancelCommandState.ACKNOWLEDGED` plus `CancelEffectState.PENDING` and fill total is below intent size | `CANCEL_PENDING` | Resume cancel/fill reconciliation |
| `NOT_OBSERVED` | broker reports nonterminal live/open and unique fills total is greater than zero but below intent size | `PARTIALLY_FILLED` | Persist order/fills and reconcile position/balance |
| `NOT_OBSERVED` | broker reports nonterminal live/open with no fills | `OPEN` | Persist the broker order snapshot |
| `OPEN`, `PARTIALLY_FILLED`, or `CANCEL_PENDING` | unique fills total equals intent size | `FILLED` | Reconcile final position and balance |
| `OPEN` | broker remains nonterminal live and unique fills total is greater than zero but below intent size | `PARTIALLY_FILLED` | Reconcile fills, position, and balance |
| `OPEN` or `PARTIALLY_FILLED` | `CancelCommandState.ACKNOWLEDGED` plus `CancelEffectState.PENDING` and fill total is below intent size | `CANCEL_PENDING` | Keep reconciling fills during cancel |
| `OPEN` or `PARTIALLY_FILLED` | broker remains nonterminal live, fill total is below intent size, and command is `REJECTED` or `NOT_SENT_FAILED` with effect `NONE` | unchanged | Keep authoritative live/fill lifecycle; retry/new-action policy belongs to the cancel command |
| `CANCEL_PENDING` | broker remains nonterminal live, command is `REJECTED` with effect `NONE`, and there are no fills | `OPEN` | Persist cancel rejection and live order evidence |
| `CANCEL_PENDING` | broker remains nonterminal live, command is `REJECTED` with effect `NONE`, and fill is partial | `PARTIALLY_FILLED` | Persist cancel rejection and reconcile fills/account |
| `OPEN`, `PARTIALLY_FILLED`, or `CANCEL_PENDING` | cancel command or effect state is `UNKNOWN` | `UNKNOWN` | Halt new entries and reconcile cancel plus order state |
| `OPEN`, `PARTIALLY_FILLED`, or `CANCEL_PENDING` | broker confirms only `cancelled`, final fills are known and below intent size, and origin is `CancelEffectState.CANCELLED` or an audited `EXTERNAL_CANCEL` | `CANCELLED` | Reconcile terminal position and balance |
| `OPEN`, `PARTIALLY_FILLED`, or `CANCEL_PENDING` | broker confirms only `expired`, final fills are known and below intent size, and cancel effect is `NONE` (no active cancel) or `SUPERSEDED_BY_EXPIRY` | `EXPIRED` | Reconcile terminal position and balance |
| Any nonterminal lifecycle state | broker evidence is missing, contradictory, or ambiguous | `UNKNOWN` | Halt new entries and reconcile |
| `UNKNOWN` | authoritative order/fill evidence establishes one state | established state | Append reconciliation evidence; never rewrite history |

`SubmissionState.ACCEPTED` means only that the broker accepted the request. It never implies `OPEN`, `PARTIALLY_FILLED`, or `FILLED`. Cancellation is not complete until the broker confirms it and fills that raced with the cancellation are reconciled.

Fill, position, and balance synchronization:

- Deduplicate fills by immutable broker trade ID. Duplicate IDs with different content are `DIVERGED`; cumulative fill size may never decrease or exceed the intent size.
- After acceptance, every fill/cancel transition, process restart, and the configured periodic interval, capture one `BrokerAccountSnapshot` plus fills since the durable cursor.
- Reconcile order -> unique fills -> expected position -> observed position -> expected balance impact -> observed balance. Each layer has its own sync state; one in-sync layer cannot hide another divergence.
- Any `DIVERGED` or `UNKNOWN` fill/position/balance state sets reconciliation pending and at least `RiskState.BLOCK_NEW_ENTRIES`. A material or unresolved divergence sets `RiskState.HALT` and `ExecutionControlState.HALTED`.

Risk and execution control:

- `ExecutionControlState.DISABLED` is the default in every environment. `ARMED` means configuration/credentials are valid but mutations remain disabled. `ENABLED` means the stage-specific environment switch is explicitly enabled; it never substitutes for the separate per-intent authorization state. Any kill switch moves to `HALTED`; only an explicit, audited operator recovery after reconciliation can leave it.
- Evaluate risk from a versioned policy/config hash and current account snapshot before confirmation, immediately before leverage/submission, after every fill/cancel/account reconciliation, and on the periodic control loop.
- Only `RiskState.CLEAR` permits a new entry. `BLOCK_NEW_ENTRIES` prevents leverage/submit. `CANCEL_OPEN_ENTRIES` permits only separately gated, lineage-bound risk-reducing cancellation. `HALT` prevents new entries and allows only issue #7's separately authorized emergency risk-reduction procedure.
- Exact exposure/loss/order-rate thresholds and reconciliation cadence are open configuration parameters, but the states, transition evidence, and fail-closed behavior above are frozen by Stage 3.

Failure classes and retry rules:

| Failure code | Dimension | Meaning | Default action |
|---|---|---|---|
| `DATA_GATE_BLOCKED` | Input | Canonical trust policy did not allow the data. | No scan or intent. |
| `SCAN_FAILED` | Scanner | Strategy/scanner raised or returned an invalid typed result. | No intent; record failure. |
| `DECISION_NOT_READY` | Decision | Typed `WAIT` or signal/pending-entry/execution-stage `BLOCK` normal no-action result. | Record no action; not a retry failure. |
| `DECISION_UNKNOWN` | Decision | `UNKNOWN` or missing typed code. | Fail closed. |
| `INTENT_INVALID` | Intent | Missing/invalid exact fields, provenance, or expiry. | No confirmation or submit. |
| `EXECUTION_GATE_INVALIDATED` | Gate | Generation, strict-policy eligibility, authorization, or another required gate changed after reservation but before mutation dispatch. | Persist invalidation, make no affected broker call, retain the reservation, and terminate new-entry progression for that action. |
| `ENVIRONMENT_MISMATCH` | Environment | Intent, business action, audit/authorization lineage, adapter, credentials, confirmation, reservation, mutation action, key namespace, key, or client ID belongs to another environment. | Reject the composition and halt before reservation or network access. |
| `NOT_AUTHORIZED` | Authorization | No valid grant exists. | No broker call. |
| `CONFIRMATION_CANCELLED` | Authorization | Operator cancelled the exact intent. | Terminal for that grant. |
| `CONFIRMATION_EXPIRED` | Authorization | Grant or intent expired. | New grant required; unchanged intent only. |
| `IDEMPOTENCY_CONFLICT` | Idempotency | A persisted key/client ID maps to different business facts, including discovered cross-environment mapping corruption. | Halt and investigate; never use this code to accept or normalize an ordinary environment mismatch. |
| `AUDIT_PRE_MUTATION_FAILED` | Audit | Required durable write-ahead event/reservation failed. | No affected broker call. |
| `LEVERAGE_REJECTED` | Broker configuration | Broker explicitly rejected leverage/margin mode. | No submit; no automatic retry. |
| `LEVERAGE_OUTCOME_UNKNOWN` | Broker configuration | Leverage mutation may have occurred but is not durably known. | Reconcile account state; no submit. |
| `BROKER_REJECTED` | Broker | Explicit business rejection. | No automatic retry. |
| `BROKER_NOT_SENT` | Broker | Adapter proves request never reached broker. | Same-key policy retry only. |
| `BROKER_OUTCOME_UNKNOWN` | Broker | Request may have reached broker or response is inconclusive. | Reconcile; never blind retry. |
| `AUDIT_POST_MUTATION_FAILED` | Audit | Result could not be durably recorded after leverage/submit/cancel side effect. | Halt and reconcile the affected operation as unknown. |
| `CANCEL_REJECTED` | Cancellation | Broker explicitly rejected a lineage-bound cancel command. | Keep/reconcile the live order; no blind retry. |
| `CANCEL_OUTCOME_UNKNOWN` | Cancellation | Cancel may have reached the broker or its terminal effect is unknown. | Mark lifecycle unknown; reconcile before another cancel/submit. |
| `RECONCILIATION_FAILED` | Reconciliation | Broker state cannot be made authoritative. | Halt new submissions. |
| `ORDER_LIFECYCLE_UNKNOWN` | Lifecycle | Accepted order cannot be placed in one authoritative lifecycle state. | Halt new entries and reconcile. |
| `FILL_DIVERGED` | Fill sync | Fill IDs/content/totals conflict. | Halt new entries; reconcile from broker. |
| `POSITION_DIVERGED` | Position sync | Expected and observed position disagree. | Halt according to risk policy. |
| `BALANCE_DIVERGED` | Balance sync | Expected and observed balance impact disagree. | Halt according to risk policy. |
| `RISK_GATE_BLOCKED` | Risk | Exposure/account/kill-switch policy denied action. | No new submit; separately gate risk-reducing cancel. |
| `KILL_SWITCH_ACTIVE` | Execution control | Control state is disabled or halted. | No new broker entry mutation. |

Environment validation precedes idempotency lookup and broker dispatch. An ordinary attempt to combine artifacts from different environments is `ENVIRONMENT_MISMATCH`; `IDEMPOTENCY_CONFLICT` is additionally raised only when durable evidence shows that a supposedly unique key or client ID already maps to cross-environment or otherwise different business facts.

## Independent Implementation PR Roadmap

Each PR must preserve the current default of no production order and must keep refresh, cleanup, and unrelated refactors out of scope. `30-F` and `7-B` are umbrella delivery gates, not instructions to combine every state dimension in one diff. If an implementation would combine order lifecycle, cancellation races, fill synchronization, position/balance reconciliation, and risk/control, it must be split into independently reviewable sub-PRs under the same owner ID. Every sub-PR defaults to no Production mutation and carries its own rollback/stop evidence.

| PR | Goal and stage | Scope / explicit non-goals | Dependency | Acceptance evidence | Broker side effect | Stop/rollback |
|---|---|---|---|---|---|---|
| 30-A | Versioned observation envelope, Stage 0 | Add typed persisted scan/data-gate outcomes beside, not inside, the legacy versionless dashboard JSON. No intent or broker changes. | PR #41 merged. | Round trips; four outcome classes; strict default loader; restart/read tests; legacy JSON exact-key tests unchanged. | None; public/read-only calls only in explicit integration tests. | Persistence failure invalidates the cycle; remove new local artifact to roll back. |
| 30-B | Immutable `OrderIntent` and factory, Stage 1/3 | Add v1 signal-lineage/Demo-business-action identity, intent revision/fingerprint, exact approved-release/typed-decision allowlist, trusted/config binding, frozen scanner-lifecycle expiry, pure revision classification, and dry-run review rendering. No storage fence, adapter, or confirmation. | 30-A; #56 supplies the first real approved release packet. | Full control-field mutation matrix; exact reuse preserving first creation time; pre-reservation supersession; cross-action/environment rejection; trusted-generation and release rollover; post-reservation revision conflict; missing/legacy data rejection; `UNKNOWN` rejection; decimal canonicalization; no application-layer or mutation reachability. | None. | Missing approved release, unresolved identity/evidence, incomplete sizing, or expired evidence blocks construction; 30-C owns durable selection/reservation. |
| 30-C | Audit ledger plus idempotency reservation, Stage 1/3 | Implement append-only typed events, the atomic business-action selection fence, environment-specific operation/mutation uniqueness, restart recovery, revision conflict handling, and leverage/submit/sequential-cancel action namespaces. Do not call a broker. | 30-B. | Event round trips, ordering, concurrent reservation, supersession-versus-first-reservation race with exactly one winner, crash windows, truncated client-ID collision, exact `intent.reused`, same-environment pre-reservation `intent.superseded`, three-revision action-wide reservation conflict (A unreserved, B reserved, C cannot supersede A), cross-environment mismatch before reservation, discovered cross-environment key corruption, post-reservation generation/revision conflict, same-key cancel retry, rejected-cancel/new-action, duplicate/restart tests. | None. | If storage cannot meet action-fence, atomic reservation/append, or environment-isolation semantics, stop and revise the design; do not emulate it with loose JSONL. |
| 30-D | `BrokerPort` and typed fake/demo adapters, Stage 2/3 | Extract current OKX Demo leverage, submit, cancel, lookup/account/fill operations behind the port; make proven-not-sent and unknown failure codes disjoint. No production adapter and no strategy/trust policy in adapter. | 30-B and 30-C. | Port conformance, leverage ordering/reconciliation, raw response matrix including empty/malformed responses, illegal result-construction checks, environment mismatch, timeout-after-send, redaction, dependency-boundary tests. | Automated tests use fakes only; optional explicitly approved demo smoke is separate evidence. | Any unclassifiable mutation becomes `UNKNOWN`; retain old default dry-run path until conformance passes. |
| 30-E | Per-intent demo confirmation and orchestration split, Stage 2 | Split scan -> intent -> gate -> confirm -> submit/reconcile; replace blanket authority with durable exact-intent grant while keeping compatibility CLI as a boundary adapter. No production config. | 30-A through 30-D. | Grant/deny/cancel/expiry, concurrent generation publication around reservation/dispatch, leverage-to-submit drift with no submit, duplicate confirmation, changed same-environment intent, cross-environment artifact injection, audit ordering, same-key retry, process restart, demo-only construction tests. | Demo only and only in an explicitly invoked smoke; default tests/config have none. | Missing audit/confirmation/environment/generation match or an unavailable publication fence disables submit and leaves existing dry-run usable. |
| 30-F | Demo lifecycle/cancellation/reconciliation and promotion packet umbrella, Stage 2 | Deliver, through separate sub-PRs where needed, the frozen open/partial/fill/cancel/expire, cancel-command/effect, fill/position/balance sync, risk, and execution-control states for Demo; put stale-order cancellation on known lineage with its own gate/key/events. No production implementation. | 30-E. | Per-sub-PR rollback/stop evidence plus data-failure cancel policy, unknown-order rejection, acknowledged-pending recovery, reject/not-sent/new-action rules, fill/expiry races, cancel ambiguity, partial/final fill dedupe, position/balance divergence, kill-switch states, no-duplicate Demo evidence, and human review agreement report. | Explicitly confirmed Demo submit/cancel may occur only in a controlled validation run; Production mutation remains unreachable. | Any unknown result, audit gap, duplicate/nonterminal cancel action, sync divergence, or policy disagreement keeps Stage 2 and disables further Demo mutations. |
| 7-A | Production-disabled composition and credential boundary, Stage 4 | Under issue #7, add separate production config/credential provider and adapter construction that cannot mutate while disabled. No order lifecycle yet. | Stage 3 Conformance Accepted and the accepted Stage 2 promotion packet. | Default-disabled proof; distinct Demo/Production business actions, audit correlations/streams, authorization/confirmation lineages, `mutation_action_id`, reservations/keys/`clOrdId`; adapter/credential isolation; shared `signal_lineage_id` only; environment-mismatch tests. | None. | Remove/disable production composition if isolation cannot be proven. |
| 7-B | Production order/protective lifecycle and reconciliation umbrella, Stage 4 | Under issue #7, use separate sub-PRs to implement the frozen leverage/submit/cancel/order/fill/position/balance contracts and a separately audited reduce-only protective-order/exit lifecycle against fakes and a production-disabled adapter. No strategy changes and no enablement path. | 7-A. | Per-sub-PR rollback/stop evidence; broker fake contract suite; protective submit/amend/cancel/replace and partial-fill sizing; independent stop authorization/idempotency/audit; timeout/unknown and stop/fill/cancel races; external protective-order detection; lifecycle/fill/account fault matrix; restart reconciliation; proof Production mutation remains unreachable. | None; production control remains `DISABLED` and the adapter cannot dispatch. | Remove/disable the adapter if any mutation path is reachable before 7-C or protective conformance is incomplete. |
| 7-C | Production risk controls and gated enablement, Stage 4 | Under issue #7, configure the frozen risk/control state machine with approved exposure/loss/order-rate thresholds, anomaly stop, operational runbook, and final enablement decision. No trusted-data relaxation and no new state semantics hidden in implementation. | Accepted 7-B protective/lifecycle conformance plus accepted Stage 2 promotion evidence. | Deterministic transition/threshold tests, operational drills, protective coverage, audit/reconciliation completeness, production-disabled rehearsal, and explicit operator approval. | Production effects become possible only after 7-C itself is accepted, the protective lifecycle is accepted, and a separate per-intent authorization satisfies every frozen gate. | Any violated invariant or failed drill leaves production `DISABLED` or `HALTED`; a v1 without accepted protection prohibits new entries. |

## Open Decisions

These values are not current repository requirements:

1. `N` consecutive dry-run days, minimum READY intent sample count, acceptable human disagreement rate, and required reviewer roles.
2. Confirmation expiry formula. Intent expiry is already frozen to the scanner's exclusive `second_pullback_wait_bars * 15m` lifecycle boundary; confirmation lifetime remains a later policy decision.
3. The authenticated operator identity mechanism and whether a second approver is required for production.
4. The operational ledger location and local filesystem provisioning policy. The repository implementation selects local SQLite schema v1 and requires an explicit path, but deployment ownership remains open.
5. Exact retry limits/backoff for proven not-sent failures. Unknown outcomes never use that retry path.
6. Production credential provider, configuration key names, release approver, and kill-switch operator.
7. Whether and how `ExecutionDecision.EXECUTION_ACCEPTED` enters a future intent contract. V1 deliberately covers the existing scanner-generated limit-entry path only.
8. The policy for risk-reducing cancellation when current market data is unavailable. The frozen minimum is known lineage, a separate typed gate, idempotency, and complete audit; it may never authorize a new entry.
9. Retention, encryption, access control, and external backup requirements for sanitized broker artifacts and the authoritative ledger.
10. Exact exposure, loss, order-rate, divergence-materiality, broker-consistency-window, and reconciliation-cadence values. Stage 3 freezes their typed states and fail-closed transitions, not these operator-owned thresholds.
11. Protective-order type, trigger source, exchange capability mapping, quantity tolerance, amendment cadence, and emergency operator procedure. These implementation policies remain open, but the separately authorized/idempotent/audited lifecycle and production-entry blocker are mandatory.
12. The storage/coordinator mechanism for the generation-publication fence. Its linearizable check/reserve and pre-dispatch semantics are mandatory; this architecture does not select a lock, lease, or compare-and-reserve implementation and does not change the current trusted refresh code.

## Non-goals and Issue Boundary

- No production live order implementation, activation, credential use, deployment, or broker call is part of issue #30.
- No `ProductionBrokerAdapter` implementation belongs in an issue #30 PR.
- This roadmap does not change trusted market-data refresh, strict trust policy, typed entry-decision mappings, strategy rules, backtest behavior, dashboard compatibility JSON, dependencies, or general documentation cleanup.
- There is no consumer-side fallback from missing/untrusted canonical data.
- Existing local `ShadowExecutionLedger` and trusted refresh logs remain separate until an implementation PR deliberately migrates or adapts them; this document does not pretend they already satisfy the execution ledger.
- `initial_stop` remains planning/review provenance and does not claim an implemented protective order or exit engine.
- Issue #30 owns the staged roadmap and Stage 3 contract. Issue #7 remains open and owns all Stage 4 production execution work.

## Issue #30 Acceptance Mapping

Current GitHub acceptance criteria:

| Issue #30 criterion | Document section |
|---|---|
| Issue #7 remains open and focused on production execution. | `Stage 4 — Production gated execution`; `Non-goals and Issue Boundary`; `7-A` through `7-C`. |
| Trusted-data is a prerequisite without refresh performance or docs cleanup work. | `Evidence baseline`; `Cross-stage rules`; `INV-1`; `Non-goals and Issue Boundary`. |
| The design can be split into implementation PRs without combining unrelated work. | `Independent Implementation PR Roadmap`. |

Delivery-specific checks:

| Check | Document section |
|---|---|
| Stages 0-4 define purpose, invariants, effects, inputs, decisions, evidence, failures, stop/promotion, and ownership. | The five `Stage` sections. |
| Stage 0-1 distinguish data failure, scanner failure, normal no-action, persistence, and promotion evidence. | `Stage 0 — Observe/report` and `Stage 1 — Dry-run scanner`. |
| Existing read-only, shadow, dry-run, demo, and confirmation behavior is mapped accurately. | `Current capability and contract gaps` and Stage 0-2 current evidence. |
| `OrderIntent`, `BrokerPort`, idempotency, and audit event shapes are reviewable. | `Stage 3 Core Design Contract`. |
| Production lifecycle, fills, cancellation, positions, balances, reconciliation, and risk/kill-switch boundaries are frozen before issue #7 implementation. | `BrokerPort` supporting records; `Audit ledger contract`; `Orthogonal state and failure model`; `7-A` through `7-C`. |
| Authorization, timeout/cancel, broker rejection, not-sent failure, unknown result, and audit failure are orthogonal. | `Orthogonal state and failure model`. |
| Demo and production cannot be silently mixed; production is disabled by default. | `INV-4`, `BrokerPort` environment isolation, Stage 2 and Stage 4. |
| Environment change creates a new business action and cannot reuse or supersede Demo/Production identity, authorization, audit, or idempotency artifacts. | `Cross-stage rules`; `INV-4`/`INV-5`; `OrderIntent`; `Idempotency contract`; `7-A`. |
| Trusted-generation changes invalidate pre-reservation confirmation and cannot bypass a post-reservation submit lineage. | Stage 2 confirmation contract; `INV-13`; `OrderIntent`; `30-B`, `30-C`, and `30-E`. |
| `initial_stop` is not broker-side protection, and production entry is blocked on accepted protective lifecycle conformance. | Stage 4; `INV-14`; `OrderIntent`; `7-B` and `7-C`. |
| Stage 3 Design Frozen is distinct from Stage 3 Conformance Accepted. | Stage 3 milestones and promotion evidence; `7-A` dependency. |
| Default behavior cannot submit a production order. | `Cross-stage rules`, Stage 4 prohibited effects, `Non-goals and Issue Boundary`. |
| Open parameters are explicit rather than presented as existing requirements. | `Open Decisions`. |

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
