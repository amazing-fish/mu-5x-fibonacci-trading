# MU 5x Fibonacci Trading Research

Research code for a long-only MU strategy using OKX `MU-USDT-SWAP` as the default data source.

The current baseline uses:

- 1h market-structure regime filtering.
- 15m Fibonacci retest entries.
- RSI and MACD confirmation.
- US cash-session timing.
- 5x pyramiding with staged margin.
- Strategy groups split by entry, position, exit, and filter components.
- OKX incremental cache updates that ignore unconfirmed candles and keep the requested lookback window.

This repository is a research artifact, not financial advice. Execution modules produce planning decisions only; they do not place orders.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the current package layout.

- `mu_strategy.market_data`: OKX/Binance providers and cache policy.
- `mu_strategy.strategies`: strategy group registry and component metadata.
- `mu_strategy.experiments`: walk-forward and ablation workflows.
- `mu_strategy.viz`: HTML report rendering.
- `mu_strategy.research`: current research conclusions.
- `mu_strategy.selection`: fixed-strategy candidate ranking.
- `mu_strategy.execution`: non-trading entry and risk planning.

Top-level modules such as `mu_strategy.data`, `mu_strategy.walk_forward`, and `mu_strategy.visualize` remain compatibility wrappers.

## Commands

Run tests:

```powershell
python -m unittest discover -s tests
```

Run the current OKX baseline:

```powershell
python -m mu_strategy.cli --days 180 --strategy baseline --report reports\mu_okx_backtest.md
```

Run the strategy-group experiment report and HTML matrix:

```powershell
python -m mu_strategy.walk_forward --window-days 180 --windows 1 --report reports\mu_okx_strategy_group_review.md --html-report reports\mu_okx_strategy_components.html
```

Generate the Plotly visualization:

```powershell
python -m mu_strategy.visualize --days 180 --strategy baseline --chart-interval 1h --output reports\mu_okx_baseline_backtest.html
```

Use Binance explicitly for comparison:

```powershell
python -m mu_strategy.cli --source binance --symbol MUUSDT --days 180 --strategy baseline --report reports\mu_binance_backtest.md
```

## Current Artifacts

- `reports/mu_okx_backtest.md`: current OKX baseline report.
- `reports/mu_okx_strategy_group_review.md`: strategy-group experiment table.
- `reports/mu_okx_strategy_components.html`: visual strategy component matrix.
- `reports/mu_okx_baseline_backtest.html`: interactive 1h chart with synchronized price, volume, and equity crosshair lines.
- `data/OKX_MU-USDT-SWAP_*_180d.csv`: cached confirmed OKX candles for reproducible local review.
