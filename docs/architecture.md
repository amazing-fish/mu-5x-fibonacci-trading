# MU Strategy Architecture

This project is organized as a research-first trading strategy workbench. It does not implement live trading.

## Data

Package: `mu_strategy.market_data`

- `providers/okx.py`: OKX public candle fetching for `MU-USDT-SWAP`.
- `providers/binance.py`: Binance futures candle fetching for explicit comparison runs.
- `cache.py`: CSV cache paths, reads/writes, incremental merge, and lookback pruning.

Rules:

- OKX is the default source for MU baseline work.
- Unconfirmed OKX candles are ignored.
- Existing OKX caches are incrementally updated and pruned to the requested `days` window.
- If an incremental OKX refresh fails, existing cached data is still usable.

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

This layer returns non-trading decisions:

- `allow`: current fixed strategy permits an entry plan.
- `wait`: signal is incomplete.
- `block`: risk filter blocks entry.

It may return margin steps and initial stop planning. It must not place orders or call broker APIs.

## OKX API Execution Preparation

Package: `mu_strategy.live`

This layer is isolated from the backtest engine and the execution-planning package. It exists to prepare and measure future API-driven execution without changing research results.

- `okx.OKXRestClient`: minimal OKX REST client with official HMAC signing, demo header support, read-only account endpoints, and guarded demo-order methods.
- `okx.ShadowExecutionLedger`: append-only JSONL ledger for paper/shadow execution observations.
- `okx_cli`: command-line entry point for read-only checks, shadow event recording, and OKX demo trading dry-runs.

Safety boundaries:

- Production live order placement is not implemented.
- The strategy engine does not call this package to place orders automatically.
- Demo orders require `--confirm-demo-order`; without it, the CLI returns a sanitized dry-run request.
- Confirmed demo orders preflight the demo instrument endpoint first and block unsupported instruments before sending an order request.
- Secrets are read from environment variables and redacted from dry-run output.
- Shadow execution writes local audit rows only and never calls OKX.
- v1 does not implement production order lifecycle, cancel/retry handling, fills, position reconciliation, risk kill-switches, or execution idempotency.

## Visualization

Package: `mu_strategy.viz`

`viz.backtest` renders the interactive Plotly backtest dashboard. The top-level `mu_strategy.visualize` module remains a compatibility wrapper.

## Compatibility Wrappers

These old entry points remain valid during migration:

- `mu_strategy.data`
- `mu_strategy.strategy`
- `mu_strategy.walk_forward`
- `mu_strategy.visualize`
- `mu_strategy.cli`
