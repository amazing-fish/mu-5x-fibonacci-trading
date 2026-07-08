import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mu_strategy.demo_trading import DemoTradingConfig, run_once
from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.market_data.service import CandleBundle
from mu_strategy.market_data.symbols import ResolvedSymbol
from mu_strategy.market_data.trusted import DataStatus
from mu_strategy.market_data.universe import OKXSwapTicker
from mu_strategy.models import Candle


class DataHealthConsumerTests(unittest.TestCase):
    def test_live_demo_scans_fresh_shared_bundle_with_empty_statuses_as_legacy(self):
        scanned = []
        bundle = _empty_status_legacy_bundle("BTC-USDT-SWAP", last_open_time_ms=900_000)

        with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=1_800_000):
            result = run_once(
                DemoTradingConfig(
                    universe_limit=1,
                    dry_run=True,
                    max_candle_staleness_bars=1,
                    watchlist_symbols=(),
                ),
                broker=None,
                universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
                candle_loader=lambda symbol, **kwargs: bundle,
                scanner=lambda symbol, candles_15m, candles_1h, **kwargs: scanned.append(
                    (symbol, len(candles_15m), len(candles_1h))
                )
                or _entry(symbol),
            )

        self.assertEqual([("BTC-USDT-SWAP", 1, 1)], scanned)
        self.assertEqual([], result["data_errors"])
        self.assertEqual("enter", result["scans"][0]["action"])
        self.assertEqual("planned", result["orders"][0]["status"])

    def test_live_demo_blocks_stale_shared_bundle_with_empty_statuses_by_legacy_age_fallback(self):
        bundle = _empty_status_legacy_bundle("BTC-USDT-SWAP", last_open_time_ms=0)

        with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=1_800_001):
            result = run_once(
                DemoTradingConfig(
                    universe_limit=1,
                    dry_run=True,
                    max_candle_staleness_bars=1,
                    watchlist_symbols=(),
                ),
                broker=None,
                universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
                candle_loader=lambda symbol, **kwargs: bundle,
                scanner=lambda symbol, candles_15m, candles_1h, **kwargs: self.fail(
                    "stale legacy data must not be scanned"
                ),
            )

        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_stale", result["data_errors"][0]["reason"])
        self.assertEqual("stale_by_clock", result["data_errors"][0]["status_reason"])
        self.assertEqual("15m", result["data_errors"][0]["interval"])
        self.assertEqual("skip", result["scans"][0]["action"])
        self.assertEqual("market_data_stale", result["scans"][0]["reason"])

    def test_live_demo_blocks_legacy_bundle_missing_requested_1h_before_scanning(self):
        bundle = _legacy_bundle_with_intervals(
            "BTC-USDT-SWAP",
            candles_by_interval={
                "15m": [_fresh_candle()],
            },
        )

        with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=1_800_000):
            result = run_once(
                DemoTradingConfig(
                    universe_limit=1,
                    dry_run=True,
                    max_candle_staleness_bars=1,
                    watchlist_symbols=(),
                ),
                broker=None,
                universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
                candle_loader=lambda symbol, **kwargs: bundle,
                scanner=lambda symbol, candles_15m, candles_1h, **kwargs: self.fail(
                    "missing requested 1h data must not be scanned"
                ),
            )

        self.assertEqual("market_data_missing", result["data_errors"][0]["reason"])
        self.assertEqual("1h", result["data_errors"][0]["interval"])
        self.assertEqual([], result["orders"])
        self.assertEqual("skip", result["scans"][0]["action"])

    def test_live_demo_blocks_legacy_bundle_missing_requested_15m_before_scanning(self):
        bundle = _legacy_bundle_with_intervals(
            "BTC-USDT-SWAP",
            candles_by_interval={
                "1h": [_fresh_candle()],
            },
        )

        with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=1_800_000):
            result = run_once(
                DemoTradingConfig(
                    universe_limit=1,
                    dry_run=True,
                    max_candle_staleness_bars=1,
                    watchlist_symbols=(),
                ),
                broker=None,
                universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
                candle_loader=lambda symbol, **kwargs: bundle,
                scanner=lambda symbol, candles_15m, candles_1h, **kwargs: self.fail(
                    "missing requested 15m data must not be scanned"
                ),
            )

        self.assertEqual("market_data_missing", result["data_errors"][0]["reason"])
        self.assertEqual("15m", result["data_errors"][0]["interval"])
        self.assertEqual([], result["orders"])
        self.assertEqual("skip", result["scans"][0]["action"])

    def test_live_demo_blocks_empty_requested_interval_before_scanning(self):
        bundle = _legacy_bundle_with_intervals(
            "BTC-USDT-SWAP",
            candles_by_interval={
                "15m": [_fresh_candle()],
                "1h": [],
            },
        )

        with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=1_800_000):
            result = run_once(
                DemoTradingConfig(
                    universe_limit=1,
                    dry_run=True,
                    max_candle_staleness_bars=1,
                    watchlist_symbols=(),
                ),
                broker=None,
                universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
                candle_loader=lambda symbol, **kwargs: bundle,
                scanner=lambda symbol, candles_15m, candles_1h, **kwargs: self.fail(
                    "empty requested 1h data must not be scanned"
                ),
            )

        self.assertEqual("market_data_missing", result["data_errors"][0]["reason"])
        self.assertEqual("1h", result["data_errors"][0]["interval"])
        self.assertEqual([], result["orders"])
        self.assertEqual("skip", result["scans"][0]["action"])

    def test_live_demo_blocks_when_trusted_status_is_invalid(self):
        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=False, max_open_positions=3),
            broker=_Broker(),
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 101.0, 1000.0)],
            candle_loader=lambda symbol, **kwargs: _invalid_bundle(symbol),
            scanner=lambda symbol, candles_15m, candles_1h, **kwargs: self.fail("invalid data must not be scanned"),
        )

        self.assertEqual("live_demo", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual("native_empty", result["data_errors"][0]["status_reason"])
        self.assertIsNone(result["data_errors"][0]["interval"])
        self.assertEqual("skip", result["scans"][0]["action"])

    def test_dashboard_does_not_show_ok_badge_when_integrity_is_invalid_but_freshness_is_fresh(self):
        from mu_strategy.viz.data_health import render_data_health_dashboard

        html = render_data_health_dashboard(
            {
                "schema_version": 3,
                "run_id": "run-invalid-fresh",
                "attempt_status": "failed",
                "snapshot_usability": "invalid",
                "updated_at_ms": 86_400_000,
                "requested_intervals": ["5m"],
                "effective_intervals": ["5m"],
                "universes": {"crypto_top": [], "stock_token_top": []},
                "symbols": {
                    "BTC-USDT-SWAP": {
                        "source": "top",
                        "intervals": {
                            "5m": {
                                "symbol": "BTC-USDT-SWAP",
                                "interval": "5m",
                                "availability": "available",
                                "integrity": "invalid",
                                "freshness": "fresh",
                                "reason": "ohlcv_invalid",
                                "rows": 1,
                                "last_timestamp_ms": 0,
                                "source_file": "okx/BTC-USDT-SWAP/5m.csv",
                            }
                        },
                    }
                },
            }
        )

        self.assertIn('<span class="badge bad">invalid</span>', html)
        self.assertNotIn('<span class="badge ok">fresh</span>', html)

    def test_dashboard_shows_blocking_summary_before_interval_table(self):
        from mu_strategy.viz.data_health import render_data_health_dashboard

        html = render_data_health_dashboard(
            {
                "schema_version": 3,
                "run_id": "run-blocking",
                "attempt_status": "degraded",
                "snapshot_usability": "invalid",
                "updated_at_ms": 86_400_000,
                "requested_intervals": ["15m", "1h"],
                "effective_intervals": ["5m", "15m", "1h"],
                "universes": {"crypto_top": [], "stock_token_top": []},
                "symbols": {
                    "META-USDT-SWAP": {
                        "source": "stock_token",
                        "intervals": {
                            "15m": _status("META-USDT-SWAP", "15m", integrity="invalid", reason="ohlcv_mismatch"),
                            "1h": _status("META-USDT-SWAP", "1h", integrity="invalid", reason="ohlcv_mismatch"),
                        },
                    }
                },
            }
        )

        self.assertIn("Blocking issues", html)
        self.assertIn("1 blocking symbol", html)
        self.assertIn("META-USDT-SWAP", html)
        self.assertIn("15m/1h: ohlcv_mismatch", html)
        self.assertIn("likely cause: zero-volume child candle OHLC policy mismatch", html)
        self.assertLess(html.index("Blocking issues"), html.index("Intervals"))

    def test_dashboard_shows_partial_coverage_summary_before_interval_table(self):
        from mu_strategy.viz.data_health import render_data_health_dashboard

        status = _status("MU-USDT-SWAP", "5m")
        status.update(
            {
                "requested_days": 180,
                "effective_days": 117.25,
                "coverage_state": "partial_available_history",
                "first_timestamp_ms": 1772608500000,
                "last_timestamp_ms": 1782738600000,
                "warnings": ["partial_available_history:requested_days=180:effective_days=117.25"],
            }
        )
        html = render_data_health_dashboard(
            {
                "schema_version": 3,
                "run_id": "run-partial",
                "attempt_status": "success",
                "snapshot_usability": "usable",
                "updated_at_ms": 86_400_000,
                "requested_intervals": ["5m"],
                "effective_intervals": ["5m"],
                "universes": {"crypto_top": [], "stock_token_top": []},
                "symbols": {
                    "MU-USDT-SWAP": {
                        "source": "stock_token",
                        "intervals": {"5m": status},
                    }
                },
            }
        )

        self.assertIn("Partial coverage", html)
        self.assertIn("MU-USDT-SWAP", html)
        self.assertIn("requested 180d", html)
        self.assertIn("effective 117.25d", html)
        self.assertLess(html.index("Partial coverage"), html.index("Intervals"))

    def test_dashboard_renders_without_diagnostics_and_says_no_blocking_symbols(self):
        from mu_strategy.viz.data_health import render_data_health_dashboard

        html = render_data_health_dashboard(
            {
                "schema_version": 3,
                "run_id": "run-old-manifest",
                "attempt_status": "success",
                "snapshot_usability": "usable",
                "updated_at_ms": 86_400_000,
                "requested_intervals": ["5m"],
                "effective_intervals": ["5m"],
                "universes": {"crypto_top": [], "stock_token_top": []},
                "symbols": {
                    "MU-USDT-SWAP": {
                        "source": "explicit",
                        "intervals": {"5m": _status("MU-USDT-SWAP", "5m")},
                    }
                },
            }
        )

        self.assertIn("无 blocking symbols", html)
        self.assertIn("Segment diagnostics", html)
        self.assertIn("No segment diagnostics", html)

    def test_dashboard_shows_refresh_segment_diagnostics(self):
        from mu_strategy.viz.data_health import render_data_health_dashboard

        html = render_data_health_dashboard(
            {
                "schema_version": 3,
                "run_id": "run-segments",
                "attempt_status": "success",
                "snapshot_usability": "usable",
                "updated_at_ms": 86_400_000,
                "requested_intervals": ["5m"],
                "effective_intervals": ["5m"],
                "universes": {"crypto_top": [], "stock_token_top": []},
                "symbols": {
                    "MU-USDT-SWAP": {
                        "source": "explicit",
                        "intervals": {"5m": _status("MU-USDT-SWAP", "5m")},
                    }
                },
                "diagnostics": {
                    "refresh_segments": [
                        {
                            "symbol": "MU-USDT-SWAP",
                            "interval": "5m",
                            "fetch_mode": "incremental_reuse",
                            "started_at_ms": 1,
                            "completed_at_ms": 26,
                            "elapsed_ms": 25,
                            "existing_rows": 288,
                            "fetched_rows": 2,
                            "output_rows": 290,
                            "had_existing": True,
                            "reused_prior_generation": True,
                            "fetch_reason": None,
                            "health_reason": "ok",
                            "error_type": None,
                            "message": None,
                        }
                    ]
                },
            }
        )

        self.assertIn("Segment diagnostics", html)
        self.assertIn("incremental_reuse", html)
        self.assertIn("25", html)
        self.assertIn("288", html)
        self.assertIn("290", html)


class _Broker:
    def get_positions(self, *, inst_type=None, inst_id=None):
        return {"code": "0", "data": [], "msg": ""}

    def get_open_orders(self, *, inst_type=None, inst_id=None):
        return {"code": "0", "data": [], "msg": ""}


def _invalid_bundle(symbol: str) -> CandleBundle:
    last_open_time_ms = int(time.time() * 1000) - 900_000
    candles = [Candle(last_open_time_ms, 100, 101, 99, 100, 1000)]
    statuses = {
        interval: DataStatus(
            symbol=symbol,
            interval=interval,
            rows=1,
            first_timestamp_ms=last_open_time_ms,
            last_timestamp_ms=last_open_time_ms,
            updated_at_ms=last_open_time_ms,
            source_file=Path(f"data/live/okx/BTC-USDT-SWAP/{interval}.csv"),
        )
        for interval in ("5m", "1h")
    }
    statuses["15m"] = DataStatus(
        symbol=symbol,
        interval="15m",
        rows=1,
        first_timestamp_ms=last_open_time_ms,
        last_timestamp_ms=last_open_time_ms,
        updated_at_ms=last_open_time_ms,
        source_file=Path("data/live/okx/BTC-USDT-SWAP/15m.csv"),
        is_valid=False,
        is_stale=False,
        reason="native_empty",
    )
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": candles, "1h": candles},
        files_by_interval={},
        days=1,
        statuses_by_interval=statuses,
    )


def _empty_status_legacy_bundle(symbol: str, *, last_open_time_ms: int) -> CandleBundle:
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={
            "15m": [Candle(last_open_time_ms, 100.0, 101.0, 99.0, 100.0, 1000.0)],
            "1h": [Candle(last_open_time_ms, 100.0, 101.0, 99.0, 100.0, 1000.0)],
        },
        files_by_interval={},
        days=28,
    )


def _legacy_bundle_with_intervals(symbol: str, *, candles_by_interval: dict[str, list[Candle]]) -> CandleBundle:
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval=candles_by_interval,
        files_by_interval={},
        days=28,
        statuses_by_interval={},
        trust_decision=None,
    )


def _fresh_candle() -> Candle:
    return Candle(900_000, 100.0, 101.0, 99.0, 100.0, 1000.0)


def _status(
    symbol: str,
    interval: str,
    *,
    integrity: str = "valid",
    freshness: str = "fresh",
    reason: str = "ok",
) -> dict:
    return {
        "symbol": symbol,
        "interval": interval,
        "availability": "available",
        "integrity": integrity,
        "freshness": freshness,
        "reason": reason,
        "reasons": [reason],
        "rows": 1,
        "first_timestamp_ms": 0,
        "last_timestamp_ms": 0,
        "updated_at_ms": 0,
        "source_file": f"okx/{symbol}/{interval}.csv",
        "warnings": [],
    }


def _entry(symbol: str) -> EntryScanResult:
    return EntryScanResult(
        symbol=symbol,
        action="enter",
        reason="entry",
        last_close=100,
        regime_1h="green",
        rsi14=55,
        macd_hist=0.1,
        macd_hist_prev=0,
        trigger_price=100,
    )


if __name__ == "__main__":
    unittest.main()
