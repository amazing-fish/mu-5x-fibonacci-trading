# MU 5x Fibonacci Trading Research

Research code for a long-only MU strategy. The default backtest data source is now OKX `MU-USDT-SWAP`,
with Binance `MUUSDT` still available as a secondary comparison source.

- 1h market-structure regime filtering.
- 15m Fibonacci retest entries.
- RSI and MACD confirmation.
- US cash-session timing.
- 5x pyramiding with staged margin.
- Two independent 14-day walk-forward windows.
- Strategy-group comparison between the baseline and optimized variants.

This repository is a research artifact, not financial advice.

## Commands

Run tests:

```powershell
python -m unittest discover -s tests
```

Run the strategy-group walk-forward report:

```powershell
python -m mu_strategy.walk_forward --window-days 180 --windows 1 --report reports\mu_okx_strategy_group_review.md --html-report reports\mu_okx_strategy_components.html
```

Generate the Plotly visualization:

```powershell
python -m mu_strategy.visualize --days 180 --chart-interval 1h --output reports\mu_okx_baseline_backtest.html
```

Use Binance explicitly when needed:

```powershell
python -m mu_strategy.walk_forward --source binance --symbol MUUSDT --window-days 14 --windows 2 --report reports\mu_strategy_group_review.md --html-report reports\mu_strategy_components.html
```

## Current Artifacts

- `reports/mu_okx_strategy_group_review.md`: OKX strategy-group walk-forward report with component matrix.
- `reports/mu_okx_strategy_components.html`: HTML strategy component dashboard.
- `reports/mu_strategy_group_review.md`: baseline vs optimized strategy-group results.
- `reports/mu_two_window_review.md`: two-window baseline review.
- `reports/mu_backtest.html`: interactive 1h chart with synchronized price, volume, and equity crosshair lines.
- `data/`: cached OKX and Binance 15m/1h candles used for reproducible local review.

OKX cache refreshes incrementally by default. The newest cached candle is reprocessed on each query, and
OKX rows with `confirm != 1` are not written to the cache because the latest K line may still be incomplete.
