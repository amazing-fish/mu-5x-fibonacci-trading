---
name: mu-5x-fibonacci-trading
description: Use when designing, reviewing, or backtesting the MU 5x long-only strategy based on 1h structure filtering, 15m Fibonacci retest entries, RSI/MACD confirmation, US cash-session timing, and strict pyramiding risk controls.
---

# MU 5x Fibonacci Trading Strategy

This is a research workflow, not financial advice. Use it to turn discretionary MU/MUUSDT ideas into explicit, testable rules.

## Contract

- Symbol focus: `MU` / `MUUSDT`.
- Direction: long-only.
- Leverage: `5x`.
- Position plan: `20% -> 20% -> 20% -> 40%` margin steps.
- First-entry max price stop: `2%`.
- Full-size account drawdown stop: `3%–4%`.
- Daily loss stop: `4%`.
- Main execution window: US cash session only, preferably Beijing `21:45–23:30` and `02:30–03:45` during US daylight saving time.

## Decision Stack

1. `1h` decides whether long trades are allowed.
2. `15m` decides the exact entry.
3. Fibonacci levels define the retest zone.
4. `RSI` and `MACD` are filters, not standalone buy buttons.
5. Stop placement and position sizing override the thesis.

## 1h Regime Filter

- Green: `1h` structure is constructive, price is above key support/EMA, `RSI > 50`, and `MACD` histogram is not deteriorating. Full pyramiding is allowed.
- Yellow: `1h` is range-bound or mixed. First `20%` margin entry is allowed, but later adds require a breakout confirmation.
- Red: `1h RSI < 45`, price loses key support, or `MACD` is negative and worsening. No long entry.

## 15m Entry Rule

Compute the latest valid upswing from low `L` to high `H`, then monitor:

- `0.382 = H - (H-L)*0.382`
- `0.5 = H - (H-L)*0.5`
- `0.618 = H - (H-L)*0.618`

Enter only when:

- price retests one of those levels;
- the `15m` candle reclaims the level instead of closing below it;
- `RSI > 45`, preferably back above `50`;
- `MACD` histogram is not continuing to weaken;
- the next candle breaks the confirmation candle high.

## Pyramiding Rule

- First add: after the first `15m` higher high, with `RSI >= 50` and improving `MACD`.
- Second add: after a `1h` pressure breakout or clear `15m` higher-high/higher-low sequence.
- Final `40%`: only after `1h` breakout confirmation, preferably `breakout -> retest -> restart`.
- Every add must tighten the stop. Never add while widening risk.

## Stop Rule

- Initial stop: `entry * 0.98`.
- After first add: stop near first-entry cost.
- After second add: stop near blended cost.
- After final add: stop below the latest `15m` higher low.
- If full-size account drawdown reaches `3%–4%`, reduce or close.

## Required Evidence

Before treating this as usable, run:

```powershell
python -m unittest discover -s tests
python -m mu_strategy.cli --symbol MUUSDT --days 14 --refresh
python -m mu_strategy.visualize --symbol MUUSDT --days 14 --output reports/mu_backtest.html
```

Review the generated `reports/mu_backtest.md` and `reports/mu_backtest.html`. Do not infer future validity from one backtest window; extend the data window and compare with a buy-and-hold baseline before using capital.
