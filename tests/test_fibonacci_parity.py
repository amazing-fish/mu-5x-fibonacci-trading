"""Regression parity tests: migrated FibonacciPullbackStrategy on unified Simulator vs legacy run_backtest."""

from __future__ import annotations

import math
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.core.instrument import CostModel, FixedTicks, okx_swap_spec
from mu_strategy.core.market_context import build_hourly_context
from mu_strategy.engine.simulator import Simulator, SimulatorConfig
from mu_strategy.market_data.bar_series import BarSeries, load_or_build_bar_series
from mu_strategy.models import Candle
from mu_strategy.strategies.families.fibonacci_pullback import (
    FibonacciPullbackParams,
    FibonacciPullbackStrategy,
)
from mu_strategy.strategies.features import FeatureCache
from mu_strategy.strategy import StrategyConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DATA_DIR = REPO_ROOT / "data" / "live"


def _candle(index: int, open_: float, high: float, low: float, close: float, volume: float = 1000.0) -> Candle:
    return Candle(
        open_time_ms=1_735_689_600_000 + index * 900_000,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _parity_cost_model(fee_rate: float) -> CostModel:
    bps = fee_rate * 10_000.0
    return CostModel(
        maker_fee_bps=bps,
        taker_fee_bps=bps,
        half_spread_bps=0.0,
        slippage_model=FixedTicks(0.0),
        stop_slippage_multiplier=1.0,
        maintenance_margin_rate=0.0,
        liquidation_fee_bps=0.0,
    )


def _run_parity(
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    config: StrategyConfig,
    *,
    starting_equity: float = 10_000.0,
):
    hourly_ctx = build_hourly_context(candles_15m, candles_1h)
    legacy = run_backtest(candles_15m, hourly_ctx, config=config, starting_equity=starting_equity)

    inst = okx_swap_spec(config.symbol, tick_size=Decimal("0.01"), max_leverage=max(config.leverage, 10.0))
    sim = Simulator(
        instrument=inst,
        cost_model=_parity_cost_model(config.fee_rate or 0.0),
        config=SimulatorConfig(
            starting_equity=starting_equity,
            primary_interval="15m",
            warmup_bars=1,
            close_on_end=True,
            sizing_basis="settled_equity",
            allow_missing_funding_for_perp=True,
            enforce_instrument_max_leverage=False,
        ),
    )
    bars: dict[tuple[str, str], BarSeries] = {
        (config.symbol, "15m"): BarSeries.from_candles(config.symbol, "15m", candles_15m),
    }
    if candles_1h:
        bars[(config.symbol, "1h")] = BarSeries.from_candles(config.symbol, "1h", candles_1h)

    strategy = FibonacciPullbackStrategy.from_legacy_config(config, legacy_parity=True)
    unified = sim.run(strategy, bars, feature_cache=FeatureCache())
    return legacy, unified


class FibonacciParityTests(unittest.TestCase):
    def test_synthetic_break_high_initial_stop_matches_legacy_backtest(self) -> None:
        config = StrategyConfig(fee_rate=0.0, trading_windows_et=(("00:00", "23:59"),))
        candles_15m = [
            _candle(0, 99.5, 101.0, 99.0, 100.5),
            _candle(1, 100.5, 102.0, 100.2, 101.5),
            _candle(2, 101.2, 101.6, 98.5, 100.9),
            _candle(3, 101.0, 103.0, 100.7, 102.5),
            _candle(4, 102.5, 103.5, 98.6, 100.1),
        ]
        legacy, unified = _run_parity(candles_15m, [], config)

        self.assertEqual(legacy.trade_count, unified.trade_count)
        self.assertEqual(1, unified.trade_count)
        self.assertAlmostEqual(legacy.ending_equity, unified.ending_equity, places=4)
        self.assertAlmostEqual(legacy.trades[0].entry_price, unified.trades[0].entry_price, places=5)
        self.assertAlmostEqual(legacy.trades[0].exit_price, unified.trades[0].exit_price, places=5)
        self.assertAlmostEqual(legacy.trades[0].pnl, unified.trades[0].net_pnl, places=4)

    def test_synthetic_second_pullback_entry_and_fees_match_legacy_backtest(self) -> None:
        config = StrategyConfig(
            fee_rate=0.0005,
            trading_windows_et=(("00:00", "23:59"),),
            entry_execution="second_pullback",
            second_pullback_wait_bars=3,
        )
        candles_15m = [
            _candle(0, 99.5, 101.0, 99.0, 100.5),
            _candle(1, 100.5, 102.0, 100.2, 101.5),
            _candle(2, 101.2, 101.6, 100.4, 100.9),
            _candle(3, 101.1, 101.4, 101.0, 101.2),
            _candle(4, 101.0, 101.2, 100.7, 101.0),
            _candle(5, 101.0, 103.5, 100.9, 103.0),
        ]
        legacy, unified = _run_parity(candles_15m, [], config)

        self.assertEqual(legacy.trade_count, unified.trade_count)
        self.assertEqual(1, unified.trade_count)
        self.assertEqual(legacy.trades[0].entry_time_ms, unified.trades[0].entry_time_ms)
        self.assertAlmostEqual(legacy.trades[0].entry_price, unified.trades[0].entry_price, places=5)
        self.assertAlmostEqual(legacy.trades[0].exit_price, unified.trades[0].exit_price, places=5)
        self.assertAlmostEqual(legacy.trades[0].fees, unified.trades[0].fees, places=5)
        self.assertAlmostEqual(legacy.ending_equity, unified.ending_equity, places=4)

    def test_synthetic_direct_next_open_with_multi_stage_pyramid_matches_legacy(self) -> None:
        # Build rising hourly candles so 1h EMA21/RSI14/MACD regime is green for stage 3+ pyramiding.
        start_ms = 1_735_689_600_000
        candles_1h = [
            Candle(
                open_time_ms=start_ms - (40 - i) * 3_600_000,
                open=80.0 + (i * i) * 0.02,
                high=81.0 + (i * i) * 0.02,
                low=79.5 + (i * i) * 0.02,
                close=80.5 + (i * i) * 0.02,
                volume=5000.0,
            )
            for i in range(40)
        ]
        config = StrategyConfig(
            fee_rate=0.0005,
            trading_windows_et=(("00:00", "23:59"),),
            entry_execution="break_high",
        )
        candles_15m = [
            _candle(0, 99.5, 101.0, 99.0, 100.5),
            _candle(1, 100.5, 102.0, 100.2, 101.5),
            _candle(2, 101.2, 101.6, 100.4, 100.9),
            _candle(3, 101.0, 103.8, 100.8, 103.5),
            _candle(4, 103.7, 106.0, 103.0, 105.5),
            _candle(5, 105.8, 108.0, 105.0, 107.2),
        ]
        legacy, unified = _run_parity(candles_15m, candles_1h, config)

        self.assertEqual(legacy.trade_count, unified.trade_count)
        self.assertEqual(1, unified.trade_count)
        self.assertEqual(legacy.trades[0].max_stage, unified.trades[0].max_stage)
        self.assertGreaterEqual(unified.trades[0].max_stage, 3)
        self.assertAlmostEqual(legacy.trades[0].entry_price, unified.trades[0].entry_price, places=4)
        self.assertAlmostEqual(legacy.trades[0].exit_price, unified.trades[0].exit_price, places=4)
        self.assertAlmostEqual(legacy.ending_equity, unified.ending_equity, places=2)

    @unittest.skipUnless(LIVE_DATA_DIR.exists(), "data/live fixture not available")
    def test_live_mu_usdt_swap_parity_across_entry_and_stop_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            s15 = load_or_build_bar_series(LIVE_DATA_DIR, symbol="MU-USDT-SWAP", interval="15m", cache_dir=cache_dir)
            s1h = load_or_build_bar_series(LIVE_DATA_DIR, symbol="MU-USDT-SWAP", interval="1h", cache_dir=cache_dir)
        candles_15m = s15.to_candles()
        candles_1h = s1h.to_candles()

        for mode_label, cfg in (
            (
                "second_pullback_baseline",
                StrategyConfig(
                    symbol="MU-USDT-SWAP",
                    entry_execution="second_pullback",
                    fib_lookback=32,
                    fee_rate=0.0005,
                    stop_tightening="baseline",
                ),
            ),
            (
                "break_high_delayed_baseline",
                StrategyConfig(
                    symbol="MU-USDT-SWAP",
                    entry_execution="break_high",
                    fib_lookback=32,
                    fee_rate=0.0005,
                    stop_tightening="delayed_baseline",
                ),
            ),
            (
                "direct_next_open_half_protect",
                StrategyConfig(
                    symbol="MU-USDT-SWAP",
                    entry_execution="direct_next_open",
                    fib_lookback=32,
                    fee_rate=0.0005,
                    stop_tightening="half_protect_green_wide",
                ),
            ),
        ):
            with self.subTest(mode=mode_label):
                legacy, unified = _run_parity(candles_15m, candles_1h, cfg)
                self.assertEqual(
                    legacy.trade_count,
                    unified.trade_count,
                    f"trade count mismatch for {mode_label}: legacy={legacy.trade_count} vs unified={unified.trade_count}",
                )
                self.assertGreater(unified.trade_count, 0)
                for idx, (lt, ut) in enumerate(zip(legacy.trades, unified.trades)):
                    self.assertEqual(
                        lt.entry_time_ms,
                        ut.entry_time_ms,
                        f"{mode_label} trade[{idx}] entry_time_ms mismatch",
                    )
                    # Normalize unified end_of_data bar-close timestamp to legacy bar-open timestamp (CR1-28).
                    normalized_ut_exit_ms = (
                        ut.exit_time_ms - s15.interval_ms
                        if ut.reason == "end_of_data"
                        else ut.exit_time_ms
                    )
                    self.assertEqual(
                        lt.exit_time_ms,
                        normalized_ut_exit_ms,
                        f"{mode_label} trade[{idx}] exit bar mismatch (legacy={lt.exit_time_ms}, unified={ut.exit_time_ms}, reason={ut.reason})",
                    )
                    self.assertAlmostEqual(
                        lt.entry_price,
                        ut.entry_price,
                        places=4,
                        msg=f"{mode_label} trade[{idx}] entry_price mismatch",
                    )
                    self.assertAlmostEqual(
                        lt.exit_price,
                        ut.exit_price,
                        places=4,
                        msg=f"{mode_label} trade[{idx}] exit_price mismatch",
                    )
                    self.assertEqual(
                        lt.max_stage,
                        ut.max_stage,
                        f"{mode_label} trade[{idx}] max_stage mismatch",
                    )
                    self.assertAlmostEqual(
                        lt.pnl,
                        ut.net_pnl,
                        places=2,
                        msg=f"{mode_label} trade[{idx}] pnl mismatch",
                    )
                self.assertAlmostEqual(legacy.ending_equity, unified.ending_equity, places=2)
                # Exact attribution identity holds even on full live history!
                attr = unified.attribution
                self.assertAlmostEqual(
                    attr.net_pnl,
                    attr.gross_pnl - attr.fees - attr.slippage + attr.funding_pnl,
                    places=6,
                )
                self.assertAlmostEqual(
                    unified.ending_equity - unified.starting_equity,
                    attr.net_pnl,
                    places=6,
                )

    def test_improved_lab_defaults_use_non_hardcoded_lookback_and_stop_floor(self) -> None:
        strat = FibonacciPullbackStrategy()
        self.assertEqual(32, strat.params.fib_lookback)
        self.assertGreater(strat.params.min_stop_pct, 0.0)
        self.assertGreater(strat.params.atr_stop_multiplier, 0.0)
        self.assertFalse(strat.params.legacy_parity)


if __name__ == "__main__":
    unittest.main()
