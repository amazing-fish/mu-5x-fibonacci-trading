import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.models import Candle


class TrustedDataPolicyTests(unittest.TestCase):
    def test_interval_dependency_planner_adds_5m_for_native_intervals(self):
        from mu_strategy.market_data.trusted_data.policy import IntervalDependencyPlanner

        planner = IntervalDependencyPlanner()

        cases = [
            (("5m",), ("5m",)),
            (("15m",), ("5m", "15m")),
            (("1h",), ("5m", "1h")),
            (("15m", "1h"), ("5m", "15m", "1h")),
            (("1h", "15m", "1h"), ("5m", "1h", "15m")),
        ]
        for requested, effective in cases:
            with self.subTest(requested=requested):
                plan = planner.plan(requested)
                self.assertEqual(tuple(dict.fromkeys(requested)), plan.requested_intervals)
                self.assertEqual(effective, plan.effective_intervals)

    def test_freshness_policy_uses_clock_interval_and_confirmed_candle(self):
        from mu_strategy.market_data.trusted_data.contracts import FreshnessState
        from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy

        policy = FreshnessPolicy(max_staleness_bars=2)

        fresh = policy.assess(now_ms=29 * 60_000, interval="15m", last_confirmed_open_time_ms=0)
        stale = policy.assess(now_ms=31 * 60_000, interval="15m", last_confirmed_open_time_ms=0)

        self.assertEqual(FreshnessState.FRESH, fresh.state)
        self.assertEqual(FreshnessState.STALE, stale.state)
        self.assertEqual("stale_by_clock", stale.reason.value)


class TrustedDataStoreLoadTests(unittest.TestCase):
    def test_load_missing_and_malformed_manifest_fail_closed_with_distinct_reasons(self):
        from mu_strategy.market_data.trusted_data.contracts import HealthReason
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            loader = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir))

            missing = loader.execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1, now_ms=0),
                trading_strict_policy(),
            )
            self.assertFalse(missing.trust_decision.allowed)
            self.assertEqual(HealthReason.MANIFEST_MISSING, missing.trust_decision.reason)

            (data_dir / "manifest.json").write_text("{not-json", encoding="utf-8")
            malformed = loader.execute(
                LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m",), days=1, now_ms=0),
                trading_strict_policy(),
            )
            self.assertFalse(malformed.trust_decision.allowed)
            self.assertEqual(HealthReason.MALFORMED_MANIFEST, malformed.trust_decision.reason)

    def test_load_path_never_calls_network_or_writes(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
        from mu_strategy.market_data.trusted_data.policy import observe_only_policy
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_manifest_and_caches(data_dir, symbol="MU-USDT-SWAP", days=2)
            store = TrustedDataStore(data_dir=data_dir)
            loader = LoadTrustedBundle(store)

            with patch("mu_strategy.market_data.providers.okx.fetch_okx_historical", side_effect=AssertionError("network")):
                with patch("mu_strategy.market_data.providers.okx.fetch_okx_incremental", side_effect=AssertionError("network")):
                    with patch.object(store, "write_csv", side_effect=AssertionError("write_csv")):
                        with patch.object(store, "write_manifest", side_effect=AssertionError("write_manifest")):
                            with patch.object(store, "append_run_log", side_effect=AssertionError("append_run_log")):
                                bundle = loader.execute(
                                    LoadTrustedBundleQuery("MU-USDT-SWAP", intervals=("15m", "1h"), days=1, now_ms=86_400_000),
                                    observe_only_policy(),
                                )

        self.assertEqual(("5m", "15m", "1h"), tuple(bundle.health_by_interval))
        self.assertTrue(bundle.trust_decision.allowed)
        self.assertLess(len(bundle.candles_by_interval["15m"]), 192)

    def test_atomic_write_failure_does_not_leave_partial_target(self):
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = TrustedDataStore(data_dir=data_dir)
            target = data_dir / "manifest.json"

            with patch("mu_strategy.market_data.trusted_data.store.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.write_manifest({"schema_version": 2, "status": "ok"})

            self.assertFalse(target.exists())


class TrustedDataRefreshTests(unittest.TestCase):
    def test_refresh_records_requested_and_effective_intervals(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshRunOutcome
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _Provider(ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}])
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("15m", "1h"),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )
            manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(RefreshRunOutcome.SUCCESS, run.outcome)
        self.assertEqual(["15m", "1h"], manifest["requested_intervals"])
        self.assertEqual(["5m", "15m", "1h"], manifest["effective_intervals"])

    def test_refresh_uses_history_then_incremental_provider(self):
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _Provider(ticker_rows=[{"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"}])
        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            use_case = RefreshTrustedMarketData(store, provider)
            request = RefreshTrustedMarketDataRequest(
                requested_intervals=("5m",),
                days=1,
                limit=1,
                stock_token_inst_ids=set(),
                now_ms=3_600_000,
            )

            use_case.execute(request)
            use_case.execute(request)

        self.assertEqual([("BTC-USDT-SWAP", "5m", 1)], provider.history_calls)
        self.assertEqual([("BTC-USDT-SWAP", "5m", 3_000_000)], provider.incremental_calls)

    def test_ticker_timeout_produces_failed_run_log(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshRunOutcome
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=data_dir), _TickerFailureProvider()).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m",),
                    days=1,
                    limit=1,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )
            log_rows = [json.loads(line) for line in (data_dir / "refresh_runs.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual(RefreshRunOutcome.FAILED, run.outcome)
        self.assertEqual("failed", log_rows[-1]["outcome"])
        self.assertEqual("TimeoutError", log_rows[-1]["cycle_error"]["error_type"])

    def test_single_interval_failure_is_partial_and_other_intervals_continue(self):
        from mu_strategy.market_data.trusted_data.contracts import RefreshRunOutcome
        from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        provider = _Provider(
            ticker_rows=[
                {"instId": "BTC-USDT-SWAP", "last": "100", "volCcy24h": "10"},
                {"instId": "ETH-USDT-SWAP", "last": "50", "volCcy24h": "9"},
            ],
            fail_history={("BTC-USDT-SWAP", "15m")},
        )
        with TemporaryDirectory() as tmp:
            run = RefreshTrustedMarketData(TrustedDataStore(data_dir=Path(tmp)), provider).execute(
                RefreshTrustedMarketDataRequest(
                    requested_intervals=("5m", "15m"),
                    days=1,
                    limit=2,
                    stock_token_inst_ids=set(),
                    now_ms=3_600_000,
                )
            )

        self.assertEqual(RefreshRunOutcome.PARTIAL, run.outcome)
        self.assertFalse(run.datasets[("BTC-USDT-SWAP", "15m")].is_usable)
        self.assertTrue(run.datasets[("ETH-USDT-SWAP", "15m")].is_usable)


class TrustedDemoConsumerTests(unittest.TestCase):
    def test_demo_defaults_to_live_trusted_store_and_cache_only_loader(self):
        from mu_strategy.demo_trading import DemoTradingConfig

        config = DemoTradingConfig()

        self.assertEqual(Path("data/live"), config.data_dir)
        self.assertFalse(config.refresh)

    def test_demo_default_universe_comes_from_trusted_manifest_snapshot(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-abc",
                        "universes": {
                            "crypto_top": [
                                {"inst_id": "BTC-USDT-SWAP", "last": 100, "volume_ccy_24h": 10, "source": "top"}
                            ],
                            "stock_token_top": [
                                {"inst_id": "MU-USDT-SWAP", "last": 5, "volume_ccy_24h": 8, "source": "stock_token"}
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = run_once(
                DemoTradingConfig(universe_limit=2, dry_run=True, data_dir=data_dir, watchlist_symbols=()),
                broker=None,
                candle_loader=lambda symbol, **kwargs: _valid_bundle(symbol, run_id="run-abc"),
                scanner=lambda symbol, candles_15m, candles_1h, **kwargs: _wait(symbol),
            )

        self.assertEqual(["BTC-USDT-SWAP", "MU-USDT-SWAP"], [item["inst_id"] for item in result["universe"]])
        self.assertEqual("run-abc", result["run_id"])
        self.assertTrue(all(scan["run_id"] == "run-abc" for scan in result["scans"]))

    def test_demo_dry_run_blocks_invalid_trusted_data_before_scanner(self):
        from mu_strategy.demo_trading import DemoTradingConfig, run_once
        from mu_strategy.market_data.universe import OKXSwapTicker

        result = run_once(
            DemoTradingConfig(universe_limit=1, dry_run=True, watchlist_symbols=()),
            broker=None,
            universe_provider=lambda limit: [OKXSwapTicker("BTC-USDT-SWAP", 100.0, 1.0)],
            candle_loader=lambda symbol, **kwargs: _invalid_bundle(symbol),
            scanner=lambda *args, **kwargs: self.fail("scanner must not run for invalid trusted data"),
        )

        self.assertEqual("dry_run", result["mode"])
        self.assertEqual([], result["orders"])
        self.assertEqual("market_data_invalid", result["data_errors"][0]["reason"])
        self.assertEqual("skip", result["scans"][0]["action"])

    def test_okx_demo_loop_cli_defaults_to_live_data_dir(self):
        from mu_strategy.commands.okx_demo_loop import main

        captured = {}

        def runner(config, broker):
            captured["data_dir"] = config.data_dir
            return {"mode": "dry_run"}

        main(["--once", "--dry-run"], stdout=_Sink(), runner=runner)

        self.assertEqual(Path("data/live"), captured["data_dir"])


def _write_manifest_and_caches(data_dir: Path, *, symbol: str, days: int) -> None:
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles
    from mu_strategy.market_data.utils import DAY_MS

    store = TrustedDataStore(data_dir=data_dir)
    five = [
        Candle(index * 300_000, 100.0 + index, 101.0 + index, 99.0 + index, 100.0 + index, 1000.0)
        for index in range(days * DAY_MS // 300_000)
    ]
    by_interval = {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }
    symbols = {symbol: {"intervals": {}}}
    for interval, candles in by_interval.items():
        path = store.cache_path(symbol, interval)
        store.write_csv(candles, path)
        symbols[symbol]["intervals"][interval] = {
            "availability": "available",
            "integrity": "valid",
            "freshness": "fresh",
            "reasons": ["ok"],
            "rows": len(candles),
            "first_timestamp_ms": candles[0].open_time_ms,
            "last_timestamp_ms": candles[-1].open_time_ms,
            "source_file": str(path),
            "validation": {"ok": True, "reason": "ok"},
        }
    store.write_manifest(
        {
            "schema_version": 2,
            "run_id": "run-1",
            "outcome": "success",
            "status": "ok",
            "started_at_ms": 0,
            "completed_at_ms": 0,
            "requested_intervals": ["15m", "1h"],
            "effective_intervals": ["5m", "15m", "1h"],
            "universes": {"crypto_top": [], "stock_token_top": []},
            "symbols": symbols,
            "warnings": [],
            "cycle_error": None,
        }
    )


def _invalid_bundle(symbol):
    from mu_strategy.market_data.service import CandleBundle
    from mu_strategy.market_data.symbols import ResolvedSymbol
    from mu_strategy.market_data.trusted import DataStatus
    from mu_strategy.models import Candle

    candle = Candle(0, 100.0, 101.0, 99.0, 100.0, 1.0)
    status = DataStatus(
        symbol=symbol,
        interval="15m",
        rows=1,
        first_timestamp_ms=0,
        last_timestamp_ms=0,
        updated_at_ms=0,
        source_file=Path("data/live/okx/MU-USDT-SWAP/15m.csv"),
        is_valid=False,
        is_stale=False,
        reason="native_empty",
    )
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": [candle], "1h": [candle]},
        files_by_interval={},
        days=1,
        statuses_by_interval={"15m": status},
    )


def _valid_bundle(symbol, *, run_id=None):
    from mu_strategy.market_data.service import CandleBundle
    from mu_strategy.market_data.symbols import ResolvedSymbol
    from mu_strategy.market_data.trusted import DataStatus

    candle = Candle(0, 100.0, 101.0, 99.0, 100.0, 1.0)
    statuses = {
        interval: DataStatus(
            symbol=symbol,
            interval=interval,
            rows=1,
            first_timestamp_ms=0,
            last_timestamp_ms=0,
            updated_at_ms=0,
            source_file=Path(f"data/live/okx/{symbol}/{interval}.csv"),
            is_valid=True,
            is_stale=False,
            reason="ok",
        )
        for interval in ("5m", "15m", "1h")
    }
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": [candle], "1h": [candle]},
        files_by_interval={},
        days=1,
        statuses_by_interval=statuses,
        run_id=run_id,
    )


def _wait(symbol):
    from mu_strategy.entry.scanner import EntryScanResult

    return EntryScanResult(
        symbol=symbol,
        action="wait",
        reason="wait",
        last_close=100,
        regime_1h="yellow",
        rsi14=None,
        macd_hist=None,
        macd_hist_prev=None,
    )


class _Sink:
    def write(self, value):
        return len(value)

    def flush(self):
        return None


class _Provider:
    def __init__(self, *, ticker_rows, fail_history=None):
        self.ticker_rows = ticker_rows
        self.fail_history = set(fail_history or ())
        self.history_calls = []
        self.incremental_calls = []

    def fetch_tickers(self):
        return list(self.ticker_rows)

    def fetch_history(self, symbol, interval, *, days):
        if (symbol, interval) in self.fail_history:
            raise TimeoutError(f"blocked {symbol} {interval}")
        self.history_calls.append((symbol, interval, days))
        return _candles(interval)

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        self.incremental_calls.append((symbol, interval, since_time_ms))
        return _candles(interval)


class _TickerFailureProvider:
    def fetch_tickers(self):
        raise TimeoutError("ticker timeout")

    def fetch_history(self, symbol, interval, *, days):
        raise AssertionError("must not fetch history")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise AssertionError("must not fetch incremental")


def _candles(interval: str) -> list[Candle]:
    five = [Candle(index * 300_000, 100 + index, 101 + index, 99 + index, 100 + index, 10.0) for index in range(12)]
    if interval == "5m":
        return five
    from mu_strategy.market_data.trusted_data.validation import aggregate_candles

    return aggregate_candles(five, interval=interval)


if __name__ == "__main__":
    unittest.main()
