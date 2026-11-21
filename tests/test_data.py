import http.client
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.data import prune_candles_to_window, read_csv, write_csv
from mu_strategy.market_data.http_pool import close_pooled_connections
from mu_strategy.market_data.providers.binance import fetch_klines
from mu_strategy.market_data.providers.okx import (
    fetch_okx_candles,
    fetch_okx_historical,
    okx_row_to_candle,
)
from mu_strategy.market_data.trusted_data.windowing import assess_requested_coverage
from mu_strategy.market_data.universe import fetch_okx_swap_tickers
from mu_strategy.market_data.utils import DAY_MS, dedupe_candles, interval_to_ms
from mu_strategy.models import Candle


class _FakeHTTPResponse:
    def __init__(self, body: bytes, *, status: int = 200, reason: str = "OK", will_close: bool = False):
        self._body = body
        self.status = status
        self.reason = reason
        self.will_close = will_close
        self.headers = {}

    def read(self) -> bytes:
        return self._body


class _FakeHTTPSConnection:
    def __init__(self, host, port, timeout, responses):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._responses = responses
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, target: str, headers: dict[str, str] | None = None):
        self.requests.append((method, target, dict(headers or {})))

    def getresponse(self):
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


class DataTests(unittest.TestCase):
    def tearDown(self):
        close_pooled_connections()

    def test_okx_row_to_candle_ignores_unconfirmed_candle(self):
        row = ["1781291700000", "990.91", "992.44", "990.42", "991.16", "68.9", "68.9", "68292.7062", "0"]

        self.assertIsNone(okx_row_to_candle(row))

    def test_okx_row_to_candle_parses_confirmed_candle(self):
        row = ["1781290800000", "992.03", "996.15", "989.97", "991.48", "703.05", "703.05", "698201.1617", "1"]

        candle = okx_row_to_candle(row)

        self.assertIsNotNone(candle)
        self.assertEqual(1781290800000, candle.open_time_ms)
        self.assertAlmostEqual(992.03, candle.open)
        self.assertAlmostEqual(703.05, candle.volume)

    def test_read_and_write_csv_round_trip(self):
        candles = [_candle(0, 100.25), _candle(900_000, 101.5)]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candles.csv"
            write_csv(candles, path)
            loaded = read_csv(path)
        self.assertEqual(candles, loaded)

    def test_prune_candles_to_window_keeps_requested_days_only(self):
        candles = [_candle(0, 100), _candle(86_400_000, 101), _candle(2 * 86_400_000, 102)]

        pruned = prune_candles_to_window(candles, days=1, end_time_ms=2 * 86_400_000)

        self.assertEqual([86_400_000, 2 * 86_400_000], [bar.open_time_ms for bar in pruned])

    def test_dedupe_candles_handles_ordered_and_unordered_inputs(self):
        ordered = [_candle(0, 100), _candle(900_000, 101), _candle(1_800_000, 102)]
        self.assertEqual(ordered, dedupe_candles(ordered))
        unordered = [_candle(900_000, 101), _candle(0, 100), _candle(900_000, 101.5)]
        deduped = dedupe_candles(unordered)
        self.assertEqual([0, 900_000], [bar.open_time_ms for bar in deduped])
        self.assertAlmostEqual(101.5, deduped[1].close)

    def test_okx_fetch_uses_browser_user_agent_and_keep_alive_pool(self):
        created: list[_FakeHTTPSConnection] = []
        responses = [
            _FakeHTTPResponse(b'{"code":"0","msg":"","data":[]}'),
            _FakeHTTPResponse(b'{"code":"0","msg":"","data":[]}'),
        ]

        def fake_new_connection(scheme, host, port, timeout):
            conn = _FakeHTTPSConnection(host, port, timeout, responses)
            created.append(conn)
            return conn

        with patch("mu_strategy.market_data.http_pool._new_connection", side_effect=fake_new_connection):
            fetch_okx_candles("MU-USDT-SWAP", "15m")
            fetch_okx_swap_tickers()

        self.assertEqual(1, len(created))
        self.assertEqual(2, len(created[0].requests))
        _, _, first_headers = created[0].requests[0]
        self.assertIn("Mozilla", first_headers["User-Agent"])
        self.assertEqual("application/json", first_headers["Accept"])
        self.assertEqual("keep-alive", first_headers["Connection"])

    def test_http_pool_reconnects_transparently_on_remote_disconnected(self):
        created: list[_FakeHTTPSConnection] = []
        per_conn_responses = [
            [
                _FakeHTTPResponse(b'{"code":"0","msg":"","data":[]}'),
                http.client.RemoteDisconnected("peer closed idle keep-alive socket"),
            ],
            [
                _FakeHTTPResponse(b'{"code":"0","msg":"","data":[]}'),
            ],
        ]

        def fake_new_connection(scheme, host, port, timeout):
            conn = _FakeHTTPSConnection(host, port, timeout, per_conn_responses.pop(0))
            created.append(conn)
            return conn

        with patch("mu_strategy.market_data.http_pool._new_connection", side_effect=fake_new_connection):
            with patch("mu_strategy.market_data.http_pool.time.sleep") as sleep_mock:
                self.assertEqual([], fetch_okx_candles("MU-USDT-SWAP", "15m"))
                self.assertEqual([], fetch_okx_candles("MU-USDT-SWAP", "15m"))

        self.assertEqual(2, len(created))
        self.assertTrue(created[0].closed)
        sleep_mock.assert_not_called()

    def test_okx_fetch_retries_transient_errors_with_bounded_backoff(self):
        created: list[_FakeHTTPSConnection] = []
        per_conn_responses = [
            [OSError("transient eof")],
            [_FakeHTTPResponse(b'{"code":"0","msg":"","data":[]}')],
        ]

        def fake_new_connection(scheme, host, port, timeout):
            conn = _FakeHTTPSConnection(host, port, timeout, per_conn_responses.pop(0))
            created.append(conn)
            return conn

        sleep_calls: list[float] = []
        with patch("mu_strategy.market_data.http_pool._new_connection", side_effect=fake_new_connection):
            with patch("mu_strategy.market_data.http_pool.time.sleep", side_effect=sleep_calls.append):
                candles = fetch_okx_candles("MU-USDT-SWAP", "15m")

        self.assertEqual([], candles)
        self.assertEqual(2, len(created))
        self.assertEqual([0.25], sleep_calls)

    def test_binance_fetch_klines_uses_keep_alive_pool(self):
        created: list[_FakeHTTPSConnection] = []
        responses = [
            _FakeHTTPResponse(b'[[0,"100","101","99","100.5","10"]]'),
            _FakeHTTPResponse(b'[[900000,"100.5","102","100","101","20"]]'),
        ]

        def fake_new_connection(scheme, host, port, timeout):
            conn = _FakeHTTPSConnection(host, port, timeout, responses)
            created.append(conn)
            return conn

        with patch("mu_strategy.market_data.http_pool._new_connection", side_effect=fake_new_connection):
            first = fetch_klines("BTCUSDT", "15m")
            second = fetch_klines("BTCUSDT", "15m", start_time_ms=900_000)

        self.assertEqual(1, len(created))
        self.assertEqual(2, len(created[0].requests))
        self.assertEqual(100.5, first[0].close)
        self.assertEqual(101.0, second[0].close)

    def test_okx_historical_starts_pagination_at_requested_end_time(self):
        end_time_ms = 2 * 86_400_000
        requested_after_values = []

        def fake_fetch(symbol, interval, *, after=None):
            requested_after_values.append(after)
            return [_candle(86_400_000, 100)]

        with patch("mu_strategy.market_data.providers.okx.fetch_okx_candles", side_effect=fake_fetch):
            candles = fetch_okx_historical("MU-USDT-SWAP", "15m", days=1, end_time_ms=end_time_ms)

        self.assertEqual(end_time_ms, requested_after_values[0])
        self.assertEqual([86_400_000], [candle.open_time_ms for candle in candles])

    def test_okx_historical_paginates_to_latest_confirmed_requested_window(self):
        days = 14
        cases = (
            ("5m", 15 * 60_000),
            ("15m", 30 * 60_000),
            ("1h", 120 * 60_000),
        )
        for interval, confirmed_lag_ms in cases:
            with self.subTest(interval=interval):
                step_ms = interval_to_ms(interval)
                latest_confirmed_ms = days * DAY_MS
                end_time_ms = latest_confirmed_ms + confirmed_lag_ms
                wall_clock_start_ms = end_time_ms - (days * DAY_MS)
                required_start_ms = latest_confirmed_ms - (days * DAY_MS)
                first_page = _range_interval_candles(wall_clock_start_ms, latest_confirmed_ms, step_ms)
                older_page = _range_interval_candles(required_start_ms, wall_clock_start_ms - step_ms, step_ms)
                requested_after_values = []

                def fake_fetch(symbol, candidate_interval, *, after=None):
                    self.assertEqual("MU-USDT-SWAP", symbol)
                    self.assertEqual(interval, candidate_interval)
                    requested_after_values.append(after)
                    if after == end_time_ms:
                        return first_page
                    if after == wall_clock_start_ms:
                        return older_page
                    return []

                with patch("mu_strategy.market_data.providers.okx.fetch_okx_candles", side_effect=fake_fetch):
                    candles = fetch_okx_historical("MU-USDT-SWAP", interval, days=days, end_time_ms=end_time_ms)

                coverage = assess_requested_coverage(
                    candles,
                    interval=interval,
                    requested_days=days,
                    window_end_time_ms=candles[-1].open_time_ms if candles else None,
                )
                self.assertEqual([end_time_ms, wall_clock_start_ms], requested_after_values)
                self.assertTrue(coverage.covered, coverage.message)
                self.assertLessEqual(candles[0].open_time_ms, required_start_ms + step_ms)

    def test_okx_historical_does_not_hide_short_history(self):
        days = 14
        interval = "15m"
        step_ms = interval_to_ms(interval)
        latest_confirmed_ms = days * DAY_MS
        end_time_ms = latest_confirmed_ms + (30 * 60_000)
        short_start_ms = DAY_MS
        first_page = _range_interval_candles(short_start_ms, latest_confirmed_ms, step_ms)
        requested_after_values = []

        def fake_fetch(symbol, candidate_interval, *, after=None):
            self.assertEqual("MU-USDT-SWAP", symbol)
            self.assertEqual(interval, candidate_interval)
            requested_after_values.append(after)
            if after == end_time_ms:
                return first_page
            return []

        with patch("mu_strategy.market_data.providers.okx.fetch_okx_candles", side_effect=fake_fetch):
            candles = fetch_okx_historical("MU-USDT-SWAP", interval, days=days, end_time_ms=end_time_ms)

        coverage = assess_requested_coverage(
            candles,
            interval=interval,
            requested_days=days,
            window_end_time_ms=candles[-1].open_time_ms if candles else None,
        )
        self.assertEqual([end_time_ms, short_start_ms], requested_after_values)
        self.assertFalse(coverage.covered)
        self.assertIn("partial_available_history", coverage.message)


def _candle(open_time_ms: int, close: float) -> Candle:
    return Candle(open_time_ms, close - 1, close + 1, close - 2, close, 1000)


def _range_interval_candles(start_ms: int, end_ms: int, step_ms: int) -> list[Candle]:
    rows: list[Candle] = []
    timestamp = start_ms
    while timestamp <= end_ms:
        rows.append(_candle(timestamp, 100 + len(rows)))
        timestamp += step_ms
    return rows


if __name__ == "__main__":
    unittest.main()
