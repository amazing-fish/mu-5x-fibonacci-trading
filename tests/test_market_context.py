import importlib
import importlib.util
import unittest
from unittest.mock import patch

from mu_strategy.models import Candle


class MarketContextTests(unittest.TestCase):
    def test_empty_hourly_candles_map_every_15m_candle_to_yellow(self):
        build_hourly_context = self._build_hourly_context()
        candles_15m = [_candle(0, 100), _candle(900_000, 101)]

        context = build_hourly_context(candles_15m, [])

        self.assertEqual({0: "yellow", 900_000: "yellow"}, context)

    def test_hourly_state_becomes_visible_only_after_candle_close(self):
        module = self._market_context_module()
        candles_15m = [
            _candle(0, 100),
            _candle(900_000, 101),
            _candle(3_600_000, 102),
            _candle(4_500_000, 103),
            _candle(7_200_000, 104),
        ]
        candles_1h = [_candle(0, 100), _candle(3_600_000, 102)]

        with patch.object(module, "ema", return_value=[100.0, 101.0]):
            with patch.object(module, "rsi", return_value=[50.0, 50.0]):
                with patch.object(module, "macd", return_value=([0.0, 0.0], [0.0, 0.0], [0.0, 0.0])):
                    with patch.object(module, "one_hour_regime", side_effect=["green", "red"]):
                        context = module.build_hourly_context(candles_15m, candles_1h)

        self.assertEqual(
            {
                0: "yellow",
                900_000: "yellow",
                3_600_000: "green",
                4_500_000: "green",
                7_200_000: "red",
            },
            context,
        )

    def test_15m_candles_before_first_hourly_candle_remain_yellow(self):
        module = self._market_context_module()
        candles_15m = [
            _candle(0, 100),
            _candle(900_000, 101),
            _candle(1_800_000, 102),
        ]
        candles_1h = [_candle(1_800_000, 102)]

        with patch.object(module, "ema", return_value=[102.0]):
            with patch.object(module, "rsi", return_value=[50.0]):
                with patch.object(module, "macd", return_value=([0.0], [0.0], [0.0])):
                    with patch.object(module, "one_hour_regime", return_value="green"):
                        context = module.build_hourly_context(candles_15m, candles_1h)

        self.assertEqual({0: "yellow", 900_000: "yellow", 1_800_000: "yellow"}, context)

    def test_cli_reexports_the_core_implementation(self):
        core_build_hourly_context = self._build_hourly_context()
        from mu_strategy.cli import build_hourly_context as cli_build_hourly_context

        candles_15m = [_candle(0, 100)]

        self.assertIs(core_build_hourly_context, cli_build_hourly_context)
        self.assertEqual(
            core_build_hourly_context(candles_15m, []),
            cli_build_hourly_context(candles_15m, []),
        )

    def _build_hourly_context(self):
        module = self._market_context_module()
        build_hourly_context = getattr(module, "build_hourly_context", None)
        self.assertTrue(callable(build_hourly_context), "core market_context must export build_hourly_context")
        return build_hourly_context

    def _market_context_module(self):
        module_name = "mu_strategy.core.market_context"
        self.assertIsNotNone(importlib.util.find_spec(module_name), f"{module_name} must exist")
        return importlib.import_module(module_name)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


if __name__ == "__main__":
    unittest.main()
