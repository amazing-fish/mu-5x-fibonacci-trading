import unittest

from mu_strategy.indicators import ema, macd, rsi


class IndicatorTests(unittest.TestCase):
    def test_ema_reacts_to_recent_prices(self):
        values = [10, 10, 10, 20]

        result = ema(values, 3)

        self.assertEqual(len(result), 4)
        self.assertGreater(result[-1], result[-2])
        self.assertLess(result[-1], 20)

    def test_rsi_bounds_and_direction(self):
        rising = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114]
        falling = list(reversed(rising))

        self.assertGreater(rsi(rising, 14)[-1], 70)
        self.assertLess(rsi(falling, 14)[-1], 30)

    def test_macd_histogram_is_returned_for_each_bar(self):
        values = [100 + i for i in range(40)]

        line, signal, hist = macd(values)

        self.assertEqual(len(line), len(values))
        self.assertEqual(len(signal), len(values))
        self.assertEqual(len(hist), len(values))
        self.assertGreater(hist[-1], -1)


if __name__ == "__main__":
    unittest.main()
