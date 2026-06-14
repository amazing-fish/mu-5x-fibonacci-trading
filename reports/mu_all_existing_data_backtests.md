# MU All Existing Local Data Backtests

- strategy: baseline
- source policy: local CSV only; no refresh/network fetch during this aggregate run
- generated at UTC: 2026-06-14T05:37:14+00:00
- datasets: 5
- data quality thresholds: 15m open-close warning > 5%, 15m high-low warning > 5%, prev close -> next open warning > 1%

## Summary

| source | symbol | nominal days | true 15m duration | true 1h duration | 15m bars | 1h bars | 15m coverage UTC | ending equity | return | max DD | trades | win rate | profit factor | audited events | price range anomalies | OHLC invalid | dup ts | 15m gaps | open-close warnings | high-low warnings | max O-C | max H-L | max prevC-open | report | chart |
|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| binance | MUUSDT | 14 | 14d 0h 0m | 14d 0h 0m | 1344 | 336 | 2026-05-28T18:15:00+00:00 to 2026-06-11T18:00:00+00:00 | 11683.11 | 16.83% | -8.42% | 4 | 50.00% | 25.65 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 3.26% | 4.79% | 0.04% | reports/mu_binance_MUUSDT_14d_baseline_backtest.md | reports/mu_binance_MUUSDT_14d_baseline_backtest.html |
| binance | MUUSDT | 28 | 28d 0h 0m | 28d 0h 0m | 2688 | 672 | 2026-05-14T19:00:00+00:00 to 2026-06-11T18:45:00+00:00 | 12365.07 | 23.65% | -11.30% | 8 | 37.50% | 4.10 | 30 | 0 | 0 | 0 | 0 | 0 | 1 | 3.93% | 5.45% | 0.08% | reports/mu_binance_MUUSDT_28d_baseline_backtest.md | reports/mu_binance_MUUSDT_28d_baseline_backtest.html |
| binance | MUUSDT | 180 | 65d 14h 15m | 65d 15h 0m | 6297 | 1575 | 2026-04-07T13:15:00+00:00 to 2026-06-12T03:15:00+00:00 | 16519.32 | 65.19% | -23.91% | 24 | 25.00% | 2.37 | 76 | 0 | 0 | 0 | 0 | 0 | 2 | 4.02% | 5.45% | 0.79% | reports/mu_binance_MUUSDT_180d_baseline_backtest.md | reports/mu_binance_MUUSDT_180d_baseline_backtest.html |
| okx | MU-USDT-SWAP | 5 | 4d 23h 45m | 4d 23h 0m | 479 | 119 | 2026-06-09T05:15:00+00:00 to 2026-06-14T04:45:00+00:00 | 11757.88 | 17.58% | -10.20% | 3 | 33.33% | 4.70 | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 3.07% | 4.82% | 0.06% | reports/mu_okx_MU_USDT_SWAP_5d_baseline_backtest.md | reports/mu_okx_MU_USDT_SWAP_5d_baseline_backtest.html |
| okx | MU-USDT-SWAP | 180 | 101d 21h 45m | 101d 22h 0m | 9783 | 2446 | 2026-03-04T07:15:00+00:00 to 2026-06-14T04:45:00+00:00 | 20580.12 | 105.80% | -26.02% | 31 | 22.58% | 2.23 | 99 | 0 | 0 | 0 | 0 | 1 | 6 | 7.01% | 7.92% | 0.63% | reports/mu_okx_MU_USDT_SWAP_180d_baseline_backtest.md | reports/mu_okx_MU_USDT_SWAP_180d_baseline_backtest.html |

## Price-Range Audit

- PASS: every fill and exit price is inside its corresponding 15m candle and corresponding 1h chart candle for all datasets.

## Data Quality Review

- binance MUUSDT nominal 28d: issues=1, open-close warnings=0, high-low warnings=1, prevC-open warnings=0, invalid OHLC=0, dup ts=0, gaps=0.
  Top 15m open-close moves:
  - 2026-05-27T13:30:00+00:00: 3.93%, O=955.9500, H=956.2500, L=913.0000, C=918.3700
  - 2026-05-26T13:30:00+00:00: 3.90%, O=821.0000, H=856.2000, L=820.7900, C=853.0500
  - 2026-06-02T14:15:00+00:00: 3.26%, O=1067.7200, H=1068.5500, L=1028.0000, C=1032.9200
- binance MUUSDT nominal 180d: issues=2, open-close warnings=0, high-low warnings=2, prevC-open warnings=0, invalid OHLC=0, dup ts=0, gaps=0.
  Top 15m open-close moves:
  - 2026-05-11T00:15:00+00:00: 4.02%, O=772.9700, H=814.0500, L=772.9700, C=804.0500
  - 2026-05-27T13:30:00+00:00: 3.93%, O=955.9500, H=956.2500, L=913.0000, C=918.3700
  - 2026-05-26T13:30:00+00:00: 3.90%, O=821.0000, H=856.2000, L=820.7900, C=853.0500
- okx MU-USDT-SWAP nominal 180d: issues=7, open-close warnings=1, high-low warnings=6, prevC-open warnings=0, invalid OHLC=0, dup ts=0, gaps=0.
  Top 15m open-close moves:
  - 2026-03-23T11:00:00+00:00: 7.01%, O=406.8900, H=439.1300, L=406.8900, C=435.4300
  - 2026-04-06T00:00:00+00:00: 4.97%, O=361.8900, H=382.7400, L=361.8900, C=379.8800
  - 2026-03-30T13:30:00+00:00: 4.56%, O=363.6800, H=363.6800, L=343.2800, C=347.0800

This report is a research artifact, not financial advice.