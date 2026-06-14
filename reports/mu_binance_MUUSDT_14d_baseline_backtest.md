# MUUSDT Backtest Report

## Config

- leverage: 5.0x
- margin steps: 20%, 20%, 20%, 40%
- initial stop: 2.00%
- fee rate: 0.0500%
- data files: data\MUUSDT_15m_14d.csv, data\MUUSDT_1h_14d.csv

## Metrics

- starting equity: 10000.00
- ending equity: 11683.11
- total return: 16.83%
- max drawdown: -8.42%
- trades: 4
- win rate: 50.00%
- profit factor: 25.65

## Trades

| entry UTC | exit UTC | entry | exit | stage | return | reason |
|---|---|---:|---:|---:|---:|---|
| 2026-05-29T14:45:00+00:00 | 2026-06-01T18:15:00+00:00 | 1008.09 | 1037.30 | 4 | 13.98% | stop |
| 2026-06-02T14:15:00+00:00 | 2026-06-03T20:45:00+00:00 | 1060.43 | 1060.43 | 3 | -0.34% | stop |
| 2026-06-10T14:00:00+00:00 | 2026-06-10T14:45:00+00:00 | 934.26 | 934.26 | 3 | -0.34% | stop |
| 2026-06-11T14:15:00+00:00 | 2026-06-11T18:00:00+00:00 | 911.26 | 940.61 | 1 | 3.53% | end_of_data |

This report is a research artifact, not financial advice.