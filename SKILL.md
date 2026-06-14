---
name: mu-5x-fibonacci-trading
description: >
  Use when designing, reviewing, or backtesting the MU 5x long-only strategy based on 1h structure filtering, 15m Fibonacci retest entries, RSI/MACD confirmation, US cash-session timing, and strict pyramiding risk controls.
  Not for live trading, broker automation, short strategies, or generic market commentary.
  Output: explicit MU strategy rules, validation commands, and local Markdown/HTML backtest artifact paths.
---

# MU 5x Fibonacci Trading Strategy

This is a research workflow, not financial advice. Use it to turn discretionary MU/MUUSDT ideas into explicit, testable rules.

## Contract

- Symbol focus: `MU` / OKX `MU-USDT-SWAP` by default. Use Binance `MUUSDT` only when explicitly requested.
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

## Project Boundaries

- Data lives in `mu_strategy.market_data`; OKX candles must ignore unconfirmed rows and keep cache windows bounded by `days`.
- Strategy groups live in `mu_strategy.strategies`; keep entry, position, exit, and filter components inspectable.
- Experiments live in `mu_strategy.experiments`; walk-forward windows are independent and must not concatenate reset equity curves for drawdown.
- Visualization lives in `mu_strategy.viz`; return local report paths instead of pasting generated HTML.
- Execution planning lives in `mu_strategy.execution`; it may return `allow`, `wait`, or `block`, margin steps, and initial stop, but it must not place broker orders.

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
python -m mu_strategy.cli --days 180 --strategy baseline --report reports\mu_okx_backtest.md
python -m mu_strategy.walk_forward --window-days 180 --windows 1 --report reports\mu_okx_strategy_group_review.md --html-report reports\mu_okx_strategy_components.html
python -m mu_strategy.visualize --days 180 --strategy baseline --chart-interval 1h --output reports\mu_okx_baseline_backtest.html
```

Review the generated Markdown and HTML reports. Do not infer future validity from one backtest window; extend the data window and compare with a buy-and-hold baseline before using capital.

## Common Mistakes

- Using Binance as the default after OKX `MU-USDT-SWAP` became the baseline source.
- Including the latest OKX candle when it is not confirmed.
- Concatenating independent walk-forward equity curves before calculating drawdown.
- Treating execution planning output as permission to place broker orders.
- Reading one profitable backtest window as future validity.
