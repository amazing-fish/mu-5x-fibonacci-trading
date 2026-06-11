# MU 5x Fibonacci Trading Research

Research code for a long-only MUUSDT strategy using:

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
python -m mu_strategy.walk_forward --symbol MUUSDT --window-days 14 --windows 2 --report reports\mu_strategy_group_review.md
```

Generate the Plotly visualization:

```powershell
python -m mu_strategy.visualize --symbol MUUSDT --days 28 --chart-interval 1h --output reports\mu_backtest.html
```

## Current Artifacts

- `reports/mu_strategy_group_review.md`: baseline vs optimized strategy-group results.
- `reports/mu_two_window_review.md`: two-window baseline review.
- `reports/mu_backtest.html`: interactive 1h chart with synchronized price, volume, and equity crosshair lines.
- `data/`: cached Binance Futures MUUSDT 15m and 1h candles used for reproducible local review.
