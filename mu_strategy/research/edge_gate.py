"""Cache-only, engine-level cross-sectional timing-edge assessment."""

from __future__ import annotations

import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Callable

from mu_strategy.backtest import run_backtest
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.market_data.trusted_data.validation import aggregate_candles
from mu_strategy.models import BacktestResult, Candle

YEAR_MS = 365 * 24 * 60 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000
STARTING_EQUITY = 10_000.0


@dataclass(frozen=True)
class SymbolEvidence:
    symbol: str
    percentile: float
    actual_return: float
    null_mean_return: float
    null_returns: tuple[float, ...]
    buy_hold_return: float
    volatility_scaled_hold_return: float
    strategy_volatility: float
    hold_volatility: float
    hold_scale: float
    funding_paid: float
    trade_count: int
    beats_buy_hold: bool
    first_bar_ms: int
    last_bar_ms: int


def stock_symbols(context) -> list[str]:
    """Use the pinned manifest universe, never a caller-curated default subset."""
    universe = context.manifest.universe_snapshot.stock_token_top
    symbols = sorted({str(item.get("inst_id", "")).upper() for item in universe})
    symbols = [symbol for symbol in symbols if symbol.endswith("-USDT-SWAP")]
    if not symbols:
        raise ValueError("trusted manifest has no stock perpetual symbols")
    for symbol in symbols:
        if (symbol, "15m") not in context.manifest.datasets or (symbol, "1h") not in context.manifest.datasets:
            raise ValueError(f"stock perpetual is not published at both intervals: {symbol}")
    return symbols


def synthetic_path(candles: list[Candle], rng: random.Random) -> list[Candle]:
    """Sample whole UTC trading-day blocks, then center log close returns."""
    if len(candles) < 4:
        raise ValueError("at least four 15m candles are required")
    blocks: dict[int, list[int]] = defaultdict(list)
    log_returns = []
    for index in range(1, len(candles)):
        blocks[candles[index].open_time_ms // DAY_MS].append(index)
        log_returns.append(math.log(candles[index].close / candles[index - 1].close))
    day_blocks = list(blocks.values())
    centered_drift = statistics.fmean(log_returns)
    sampled: list[int] = []
    while len(sampled) < len(candles) - 1:
        sampled.extend(rng.choice(day_blocks))
    sampled = sampled[:len(candles) - 1]

    output = [candles[0]]
    previous_close = candles[0].close
    for target, source_index in zip(candles[1:], sampled):
        source = candles[source_index]
        prior = candles[source_index - 1]
        opened = previous_close * source.open / prior.close
        closed = previous_close * math.exp(math.log(source.close / prior.close) - centered_drift)
        high = max(opened, closed, opened * source.high / source.open)
        low = min(opened, closed, opened * source.low / source.open)
        output.append(Candle(target.open_time_ms, opened, high, low, closed, source.volume))
        previous_close = closed
    return output


def _funding_accrual(result: BacktestResult, candles: list[Candle], annual: float) -> dict[int, float]:
    if not candles or annual == 0:
        return {c.open_time_ms: 0.0 for c in candles}
    events: dict[int, float] = defaultdict(float)
    for trade in result.trades:
        for fill in trade.fills:
            events[fill.time_ms] += fill.units
            events[trade.exit_time_ms] -= fill.units
    units = 0.0
    accrued = 0.0
    cumulative: dict[int, float] = {}
    for index, candle in enumerate(candles):
        units += events[candle.open_time_ms]
        cumulative[candle.open_time_ms] = accrued
        if index + 1 < len(candles):
            elapsed = candles[index + 1].open_time_ms - candle.open_time_ms
            accrued += max(0.0, units) * candle.close * elapsed / YEAR_MS * annual
    cumulative[-1] = accrued
    return cumulative


def funding_cost(result: BacktestResult, candles: list[Candle], annual: float) -> float:
    if annual < 0 or not math.isfinite(annual):
        raise ValueError("funding_annual must be finite and non-negative")
    return _funding_accrual(result, candles, annual).get(-1, 0.0)


def _strategy_volatility(result: BacktestResult, candles: list[Candle], annual: float) -> float:
    funding = _funding_accrual(result, candles, annual)
    last_by_time = {time_ms: value for time_ms, value in result.equity_curve}
    values = [last_by_time.get(bar.open_time_ms, STARTING_EQUITY) - funding[bar.open_time_ms]
              for bar in candles if bar.open_time_ms in last_by_time]
    returns = [right / left - 1 for left, right in zip(values, values[1:]) if left > 0]
    return statistics.pstdev(returns) if len(returns) > 1 else 0.0


def _hold_metrics(candles: list[Candle], strategy_volatility: float,
                  funding_annual: float, fee_rate: float) -> tuple[float, float, float, float]:
    prices = [bar.close for bar in candles]
    returns = [right / left - 1 for left, right in zip(prices, prices[1:])]
    hold_volatility = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    scale = strategy_volatility / hold_volatility if hold_volatility > 0 else 0.0
    # One unit of buy-and-hold is opened at the first bar and settled at the
    # last bar. Both benchmarks carry the same taker fees and long funding.
    exposure_years = sum((next_bar.open_time_ms - bar.open_time_ms) / YEAR_MS * bar.close / prices[0]
                         for bar, next_bar in zip(candles, candles[1:]))
    price_return = prices[-1] / prices[0] - 1
    one_x = price_return - fee_rate * (1 + prices[-1] / prices[0]) - funding_annual * exposure_years
    scaled = scale * one_x
    return one_x, scaled, hold_volatility, scale


def assess_symbol(
    symbol: str,
    candles: list[Candle],
    config,
    *,
    simulations: int,
    seed: int,
    funding_annual: float,
    hourly_candles: list[Candle] | None = None,
    backtest_fn: Callable = run_backtest,
) -> SymbolEvidence:
    if simulations < 1:
        raise ValueError("simulations must be positive")
    if len(candles) < 4:
        raise ValueError(f"{symbol}: too few 15m candles")
    hourly = hourly_candles if hourly_candles is not None else aggregate_candles(
        candles, interval="1h", base_interval="15m")
    actual = backtest_fn(candles, build_hourly_context(candles, hourly), config=config)
    actual_funding = funding_cost(actual, candles, funding_annual)
    actual_return = (actual.ending_equity - actual_funding) / actual.starting_equity - 1
    rng = random.Random(seed)
    null_returns = []
    for _ in range(simulations):
        synthetic = synthetic_path(candles, rng)
        synthetic_hourly = aggregate_candles(synthetic, interval="1h", base_interval="15m")
        result = backtest_fn(synthetic, build_hourly_context(synthetic, synthetic_hourly), config=config)
        null_returns.append((result.ending_equity - funding_cost(result, synthetic, funding_annual))
                            / result.starting_equity - 1)
    # Midrank makes all-tie (e.g. no trades) evidence neutral at 50.
    below = sum(value < actual_return - 1e-12 for value in null_returns)
    ties = sum(abs(value - actual_return) <= 1e-12 for value in null_returns)
    percentile = 100 * (below + ties / 2) / simulations
    strategy_vol = _strategy_volatility(actual, candles, funding_annual)
    one_x, scaled, hold_vol, scale = _hold_metrics(
        candles, strategy_vol, funding_annual, getattr(config, "fee_rate", 0.0005))
    return SymbolEvidence(
        symbol=symbol, percentile=percentile, actual_return=actual_return,
        null_mean_return=statistics.fmean(null_returns), null_returns=tuple(null_returns),
        buy_hold_return=one_x, volatility_scaled_hold_return=scaled,
        strategy_volatility=strategy_vol, hold_volatility=hold_vol, hold_scale=scale,
        funding_paid=actual_funding, trade_count=actual.trade_count,
        beats_buy_hold=actual_return > one_x, first_bar_ms=candles[0].open_time_ms,
        last_bar_ms=candles[-1].open_time_ms,
    )


def summarize_gate(rows: list[SymbolEvidence], *, z_threshold: float) -> dict:
    if not rows:
        raise ValueError("no stock perpetual evidence")
    if not math.isfinite(z_threshold) or z_threshold < 0:
        raise ValueError("z_threshold must be finite and non-negative")
    values = [row.percentile for row in rows]
    mean = statistics.fmean(values)
    # Each symbol is one observation. The uniform-null floor avoids false
    # certainty when observed cross-sectional dispersion happens to be tiny.
    empirical_se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    null_se = 100 / math.sqrt(12 * len(values))
    se = max(empirical_se, null_se)
    z = (mean - 50) / se
    beat_count = sum(row.beats_buy_hold for row in rows)
    failures = []
    if z < z_threshold:
        failures.append(f"mean percentile z={z:.4f} < {z_threshold:g}")
    if beat_count * 2 <= len(rows):
        failures.append(f"beat 1x hold on {beat_count}/{len(rows)} symbols (requires >50%)")
    return {
        "verdict": "FAIL" if failures else "PASS", "failed_checks": failures,
        "mean_percentile": mean, "standard_error": se, "z_score": z,
        "above_90_count": sum(value > 90 for value in values),
        "above_90_expected": len(rows) * 0.1, "beats_1x_count": beat_count,
        "symbol_count": len(rows), "symbols": [asdict(row) for row in rows],
    }


def render_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def render_markdown(payload: dict) -> str:
    lines = ["# Edge gate", "", f"Verdict: **{payload['verdict']}**", "",
             f"Commit: `{payload['commit']}`; generation: `{payload['generation_id']}`; seed: `{payload['seed']}`", "",
             f"Parameters: `{json.dumps(payload['parameters'], sort_keys=True)}`", "",
             f"Mean percentile: {payload['mean_percentile']:.2f} (SE {payload['standard_error']:.2f}; z {payload['z_score']:.2f})", "",
             f">90th percentile: {payload['above_90_count']} / {payload['symbol_count']} (expected {payload['above_90_expected']:.1f})", "",
             f"Beat 1x hold: {payload['beats_1x_count']} / {payload['symbol_count']}", ""]
    if payload['failed_checks']:
        lines.extend(["## Failed checks", ""] + [f"- {reason}" for reason in payload['failed_checks']] + [""])
    lines.extend(["## Symbols", "", "| Symbol | Percentile | Net return | Null mean | 1x hold | Vol-scaled hold | Strategy vol | Hold vol | Funding paid | Trades | Beat 1x |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|"])
    for row in payload['symbols']:
        lines.append(f"| {row['symbol']} | {row['percentile']:.2f} | {row['actual_return']:.2%} | "
                     f"{row['null_mean_return']:.2%} | {row['buy_hold_return']:.2%} | "
                     f"{row['volatility_scaled_hold_return']:.2%} | {row['strategy_volatility']:.4%} | "
                     f"{row['hold_volatility']:.4%} | {row['funding_paid']:.2f} | {row['trade_count']} | "
                     f"{'yes' if row['beats_buy_hold'] else 'no'} |")
    lines.append("")
    return "\n".join(lines)
