from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
import math
import unittest

from mu_strategy.core.instrument import (
    AtrBps,
    CostModel,
    FixedTicks,
    InstrumentSpec,
    SqrtVolumeImpact,
    binance_perp_spec,
    okx_swap_spec,
    slippage_model_from_dict,
    spot_spec,
)


class InstrumentSpecTests(unittest.TestCase):
    def test_helper_constructors_and_properties(self) -> None:
        okx = okx_swap_spec(
            "MU-USDT-SWAP",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.01"),
            min_size=Decimal("0.1"),
            contract_value=Decimal("0.01"),
            max_leverage=5.0,
            calendar_id="US_EQUITIES",
            funding_interval_hours=4,
        )
        self.assertEqual("okx:MU-USDT-SWAP", okx.instrument_id)
        self.assertEqual("okx", okx.venue)
        self.assertEqual("MU-USDT-SWAP", okx.symbol)
        self.assertEqual("perp", okx.asset_class)
        self.assertTrue(okx.is_perp)
        self.assertEqual(4, okx.funding_interval_hours)
        self.assertEqual(4 * 3_600_000, okx.funding_interval_ms)
        self.assertEqual(Decimal("0.01"), okx.tick_size)
        self.assertEqual(Decimal("0.1"), okx.min_size)
        self.assertEqual(0.01, okx.tick_size_float)
        self.assertEqual(0.01, okx.contract_value_float)

        binance = binance_perp_spec("BTCUSDT", funding_interval_hours=8, max_leverage=20.0)
        self.assertEqual("binance:BTCUSDT", binance.instrument_id)
        self.assertEqual("binance", binance.venue)
        self.assertEqual("perp", binance.asset_class)
        self.assertEqual(8 * 3_600_000, binance.funding_interval_ms)

        # Per-contract configurable funding intervals (1h, 2h, 4h, 8h)
        for hours in (1, 2, 4, 8):
            spec = binance_perp_spec("ETHUSDT", funding_interval_hours=hours)
            self.assertEqual(hours, spec.funding_interval_hours)

        spot = spot_spec("BTCUSDT", venue="binance")
        self.assertEqual("binance:BTCUSDT", spot.instrument_id)
        self.assertEqual("spot", spot.asset_class)
        self.assertFalse(spot.is_perp)
        self.assertIsNone(spot.funding_interval_hours)
        self.assertIsNone(spot.funding_interval_ms)
        self.assertEqual(1.0, spot.max_leverage)

        equity = spot_spec("MU", venue="nasdaq", asset_class="equity", calendar_id="US_EQUITIES")
        self.assertEqual("nasdaq:MU", equity.instrument_id)
        self.assertEqual("equity", equity.asset_class)
        self.assertIsNone(equity.funding_interval_hours)
        self.assertEqual("US_EQUITIES", equity.calendar_id)

    def test_immutability_and_serialization_roundtrip(self) -> None:
        spec = okx_swap_spec("MU-USDT-SWAP", contract_value=Decimal("0.1"), funding_interval_hours=8)
        with self.assertRaises(FrozenInstanceError):
            spec.max_leverage = 10.0  # type: ignore[misc]
        roundtripped = InstrumentSpec.from_dict(spec.to_dict())
        self.assertEqual(spec, roundtripped)

        spot = spot_spec("MU", venue="nasdaq", asset_class="equity")
        self.assertEqual(spot, InstrumentSpec.from_dict(spot.to_dict()))

    def test_decimal_rounding_and_contract_sizing(self) -> None:
        spec = okx_swap_spec(
            "MU-USDT-SWAP",
            tick_size=Decimal("0.05"),
            lot_size=Decimal("0.1"),
            min_size=Decimal("0.5"),
            contract_value=Decimal("2"),
        )
        self.assertEqual(Decimal("100.10"), spec.round_price("100.14", rounding="down"))
        self.assertEqual(Decimal("100.15"), spec.round_price("100.11", rounding="up"))
        self.assertEqual(Decimal("100.15"), spec.round_price("100.13", rounding="nearest"))

        self.assertEqual(Decimal("0.3"), spec.round_size("0.39", rounding="down", enforce_min=False))
        self.assertEqual(Decimal("0"), spec.round_size("0.39", rounding="down", enforce_min=True))
        self.assertEqual(Decimal("0.5"), spec.round_size("0.59", rounding="down", enforce_min=True))

        # notional = 500, price = 100, contract_value = 2 => raw contracts = 2.5
        contracts = spec.contracts_for_notional(500, price=100, enforce_min=True)
        self.assertEqual(Decimal("2.5"), contracts)
        self.assertEqual(Decimal("5.0"), spec.base_qty_for_contracts(contracts))
        self.assertEqual(Decimal("500.0"), spec.notional_for_contracts(contracts, price=100))

    def test_validation_rejects_invalid_inputs(self) -> None:
        base = dict(
            instrument_id="okx:MU-USDT-SWAP",
            venue="okx",
            symbol="MU-USDT-SWAP",
            asset_class="perp",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.01"),
            min_size=Decimal("0.01"),
            contract_value=Decimal("1"),
            max_leverage=5.0,
        )
        # Empty / whitespace strings
        for field in ("instrument_id", "venue", "symbol"):
            with self.assertRaises(ValueError):
                InstrumentSpec(**{**base, field: ""})
            with self.assertRaises(ValueError):
                InstrumentSpec(**{**base, field: " bad "})

        # Invalid asset class
        with self.assertRaises(ValueError):
            InstrumentSpec(**{**base, "asset_class": "future"})  # type: ignore[arg-type]

        # Non-positive / non-finite / bool decimals & leverage
        for field in ("tick_size", "lot_size", "min_size", "contract_value"):
            for bad in (Decimal("0"), Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity"), True):
                with self.assertRaises(ValueError):
                    InstrumentSpec(**{**base, field: bad})  # type: ignore[arg-type]

        for bad_lev in (0.0, -2.0, math.nan, math.inf, True):
            with self.assertRaises(ValueError):
                InstrumentSpec(**{**base, "max_leverage": bad_lev})  # type: ignore[arg-type]

        # Funding interval rules
        for asset in ("spot", "equity"):
            with self.assertRaises(ValueError):
                InstrumentSpec(**{**base, "asset_class": asset, "funding_interval_hours": 8})  # type: ignore[arg-type]

        for bad_hours in (0, -4, True, 4.5):
            with self.assertRaises(ValueError):
                InstrumentSpec(**{**base, "funding_interval_hours": bad_hours})  # type: ignore[arg-type]


class SlippageAndCostModelTests(unittest.TestCase):
    def test_fixed_ticks_slippage(self) -> None:
        model = FixedTicks(ticks=2.5)
        self.assertAlmostEqual(0.025, model.slippage_price(100.0, tick_size=0.01))
        self.assertEqual(0.0, FixedTicks(0.0).slippage_price(100.0, tick_size=0.01))
        self.assertEqual(FixedTicks(5.0), model.scaled(2.0))
        self.assertEqual(model, slippage_model_from_dict(model.to_dict()))
        with self.assertRaises(ValueError):
            FixedTicks(-1.0)
        with self.assertRaises(ValueError):
            FixedTicks(math.nan)
        with self.assertRaises(ValueError):
            FixedTicks(True)  # type: ignore[arg-type]

    def test_atr_bps_slippage(self) -> None:
        model = AtrBps(bps_per_atr=500.0)  # 5% of ATR
        self.assertAlmostEqual(0.10, model.slippage_price(100.0, tick_size=0.01, atr=2.0))
        self.assertEqual(0.0, model.slippage_price(100.0, tick_size=0.01, atr=None))
        self.assertEqual(0.0, model.slippage_price(100.0, tick_size=0.01, atr=0.0))
        self.assertEqual(0.0, model.slippage_price(100.0, tick_size=0.01, atr=math.nan))
        self.assertEqual(AtrBps(1000.0), model.scaled(2.0))
        self.assertEqual(model, slippage_model_from_dict(model.to_dict()))
        with self.assertRaises(ValueError):
            AtrBps(-5.0)

    def test_sqrt_volume_impact_slippage(self) -> None:
        model = SqrtVolumeImpact(impact_coeff=20.0, max_bps=100.0)
        # participation = 10_000 / 1_000_000 = 0.01 => sqrt = 0.1 => 2.0 bps => 0.02% of $100 = $0.02
        slip = model.slippage_price(100.0, tick_size=0.01, order_notional=10_000.0, bar_usd_volume=1_000_000.0)
        self.assertAlmostEqual(0.02, slip, places=12)

        # Larger order -> larger slippage
        slip_big = model.slippage_price(100.0, tick_size=0.01, order_notional=40_000.0, bar_usd_volume=1_000_000.0)
        self.assertAlmostEqual(0.04, slip_big, places=12)
        self.assertGreater(slip_big, slip)

        # Thin/zero weekend volume -> conservatively caps at max_bps (100 bps = 1% = $1.00)
        slip_zero_vol = model.slippage_price(100.0, tick_size=0.01, order_notional=10_000.0, bar_usd_volume=0.0)
        self.assertAlmostEqual(1.00, slip_zero_vol, places=12)

        # Huge participation capped at max_bps
        slip_capped = model.slippage_price(100.0, tick_size=0.01, order_notional=1_000_000.0, bar_usd_volume=100.0)
        self.assertAlmostEqual(1.00, slip_capped, places=12)

        # Zero order notional -> 0 slippage
        self.assertEqual(0.0, model.slippage_price(100.0, tick_size=0.01, order_notional=0.0, bar_usd_volume=0.0))
        self.assertEqual(model, slippage_model_from_dict(model.to_dict()))

    def test_cost_model_rates_stop_multiplier_and_validation(self) -> None:
        cost = CostModel(
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            half_spread_bps=1.5,
            slippage_model=FixedTicks(2.0),
            stop_slippage_multiplier=1.5,
            maintenance_margin_rate=0.01,
            liquidation_fee_bps=5.0,
            missing_funding_apr_band=(0.0, 0.15, 0.30),
        )
        self.assertAlmostEqual(0.0002, cost.maker_fee_rate)
        self.assertAlmostEqual(0.0005, cost.taker_fee_rate)
        self.assertAlmostEqual(0.00015, cost.half_spread_rate)
        self.assertAlmostEqual(0.0005, cost.liquidation_fee_rate)
        self.assertAlmostEqual(0.015, cost.half_spread_price(100.0))

        base_slip = cost.estimate_slippage_price(100.0, tick_size=0.01, is_stop=False)
        stop_slip = cost.estimate_slippage_price(100.0, tick_size=0.01, is_stop=True)
        self.assertAlmostEqual(0.02, base_slip)
        self.assertAlmostEqual(0.03, stop_slip)

        doubled = cost.scaled(2.0)
        self.assertAlmostEqual(4.0, doubled.maker_fee_bps)
        self.assertAlmostEqual(10.0, doubled.taker_fee_bps)
        self.assertAlmostEqual(3.0, doubled.half_spread_bps)
        self.assertEqual(FixedTicks(4.0), doubled.slippage_model)

        self.assertEqual(cost, CostModel.from_dict(cost.to_dict()))

        zero = CostModel.zero_cost()
        self.assertEqual(0.0, zero.maker_fee_bps)
        self.assertEqual(0.0, zero.taker_fee_bps)
        self.assertEqual(0.0, zero.half_spread_bps)
        self.assertEqual(0.0, zero.maintenance_margin_rate)
        self.assertEqual(0.0, zero.liquidation_fee_bps)

        # Negative zero normalized to +0.0
        norm = CostModel(maker_fee_bps=-0.0, taker_fee_bps=-0.0, half_spread_bps=-0.0)
        self.assertEqual(1.0, math.copysign(1.0, norm.maker_fee_bps))

        # Validation failures
        with self.assertRaises(ValueError):
            CostModel(maker_fee_bps=-1.0)
        with self.assertRaises(ValueError):
            CostModel(maintenance_margin_rate=1.0)
        with self.assertRaises(ValueError):
            CostModel(missing_funding_apr_band=())
        with self.assertRaises(ValueError):
            CostModel(missing_funding_apr_band=(0.0, math.nan))
        with self.assertRaises(TypeError):
            CostModel(slippage_model="fixed")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
