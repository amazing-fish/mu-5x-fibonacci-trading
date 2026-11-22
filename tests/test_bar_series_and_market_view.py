from __future__ import annotations

from array import array
import math
import os
from pathlib import Path
import statistics
import struct
from tempfile import TemporaryDirectory
import time
import unittest

from mu_strategy.market_data.bar_series import (
    BAR_BIN_MAGIC,
    BAR_BIN_VERSION,
    BAR_RECORD_STRUCT,
    BarSeries,
    BarSeriesCacheError,
    BarSeriesSlice,
    MarketView,
    load_or_build_bar_series,
    read_bar_series_bin,
    write_bar_series_bin,
)
from mu_strategy.market_data.funding import FundingRateEvent, FundingSeries
from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
from mu_strategy.models import Candle


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DATA_DIR = REPO_ROOT / "data" / "live"
MIN_15_MS = 900_000
HOUR_1_MS = 3_600_000


def _make_candles(count: int, *, start_ms: int = 1_700_000_400_000, step_ms: int = MIN_15_MS) -> list[Candle]:
    candles: list[Candle] = []
    for idx in range(count):
        base = 100.0 + idx * 0.5
        candles.append(
            Candle(
                open_time_ms=start_ms + idx * step_ms,
                open=base,
                high=base + 1.25,
                low=base - 0.75,
                close=base + 0.25,
                volume=1000.0 + idx * 10.0,
            )
        )
    return candles


class BarSeriesTests(unittest.TestCase):
    def test_from_candles_immutability_indexing_and_zero_copy_slice(self) -> None:
        candles = _make_candles(10)
        series = BarSeries.from_candles("MU-USDT-SWAP", "15m", candles)
        self.assertEqual(10, len(series))
        self.assertEqual("MU-USDT-SWAP", series.symbol)
        self.assertEqual("15m", series.interval)
        self.assertEqual(MIN_15_MS, series.interval_ms)
        self.assertTrue(series.is_uniform_close_times)
        self.assertEqual(candles[0], series[0])
        self.assertEqual(candles[-1], series[-1])
        self.assertEqual(candles[-1], series.latest())
        self.assertEqual(candles, series.to_candles())

        # Immutability
        with self.assertRaises(AttributeError):
            series.symbol = "OTHER"  # type: ignore[misc]

        # index_at_or_before (by open & by close)
        t0 = candles[0].open_time_ms
        self.assertEqual(-1, series.index_at_or_before(t0 - 1, by="open"))
        self.assertEqual(0, series.index_at_or_before(t0, by="open"))
        self.assertEqual(0, series.index_at_or_before(t0 + MIN_15_MS - 1, by="open"))
        self.assertEqual(1, series.index_at_or_before(t0 + MIN_15_MS, by="open"))

        self.assertEqual(-1, series.index_at_or_before(t0 + MIN_15_MS - 1, by="close"))
        self.assertEqual(0, series.index_at_or_before(t0 + MIN_15_MS, by="close"))
        self.assertEqual(9, series.index_at_or_before(t0 + 20 * MIN_15_MS, by="close"))

        # Zero-copy slice_view bounds enforcement
        sl = series.slice_view(2, 6)
        self.assertIsInstance(sl, BarSeriesSlice)
        self.assertEqual(2, sl.start)
        self.assertEqual(6, sl.end)
        self.assertEqual(4, len(sl))
        self.assertEqual(candles[2], sl[0])
        self.assertEqual(candles[5], sl[-1])
        self.assertEqual(candles[5], sl.latest())
        self.assertEqual(candles[2].close, sl.closes[0])
        self.assertEqual(candles[5].close, sl.closes[-1])
        self.assertEqual(candles[5].open_time_ms + MIN_15_MS, sl.close_times[-1])
        self.assertEqual(candles[4:6], sl.tail(2).to_candles())
        self.assertEqual(candles[3:5], sl.subslice(1, 3).to_candles())
        self.assertEqual(candles[3:5], sl[1:3].to_candles())

        # Cannot read past slice end even though underlying series has 10 bars
        with self.assertRaises(IndexError):
            _ = sl[4]
        with self.assertRaises(IndexError):
            _ = sl[-5]
        with self.assertRaises(IndexError):
            _ = sl.closes[4]
        with self.assertRaises(IndexError):
            _ = sl.closes[-5]
        with self.assertRaises(IndexError):
            _ = sl.close_times[4]

    def test_empty_single_bar_and_validation_errors(self) -> None:
        empty = BarSeries.from_candles("BTCUSDT", "1h", [])
        self.assertEqual(0, len(empty))
        self.assertFalse(empty)
        self.assertIsNone(empty.latest())
        self.assertEqual(-1, empty.index_at_or_before(10_000))
        self.assertEqual(0, empty.closed_count_at(10_000))
        self.assertEqual(0, len(empty.slice_view(0, 0)))

        single = BarSeries.from_candles("BTCUSDT", "1h", _make_candles(1, step_ms=HOUR_1_MS))
        self.assertEqual(1, len(single))
        self.assertEqual(0, single.closed_count_at(single.open_times[0]))
        self.assertEqual(0, single.closed_count_at(single.open_times[0] + HOUR_1_MS - 1))
        self.assertEqual(1, single.closed_count_at(single.open_times[0] + HOUR_1_MS))

        # Validation failures
        with self.assertRaises(ValueError):
            BarSeries.from_candles("BTC", "15m", [Candle(2000, 10, 11, 9, 10, 1), Candle(1000, 10, 11, 9, 10, 1)])
        with self.assertRaises(ValueError):
            BarSeries.from_candles("BTC", "15m", [Candle(1000, 10, 9.5, 9, 10, 1)])  # high < open
        with self.assertRaises(ValueError):
            BarSeries.from_candles("BTC", "15m", [Candle(1000, 10, 11, 10.5, 10, 1)])  # low > open
        with self.assertRaises(ValueError):
            BarSeries.from_candles("BTC", "15m", [Candle(1000, 0.0, 1, 0, 1, 1)])  # non-positive price
        with self.assertRaises(ValueError):
            BarSeries.from_candles("BTC", "15m", [Candle(1000, 10, 11, 9, 10, -1)])  # negative volume
        with self.assertRaises(ValueError):
            BarSeries.from_candles("BTC", "15m", [Candle(1000, math.nan, 11, 9, 10, 1)])


class BinaryCacheTests(unittest.TestCase):
    def test_bin_roundtrip_exact_size_and_non_uniform_close_times(self) -> None:
        candles = _make_candles(64)
        series = BarSeries.from_candles("MU-USDT-SWAP", "15m", candles)
        sha = candles_content_sha256(candles)

        with TemporaryDirectory() as tmp:
            bin_path = Path(tmp) / "mu_15m.bin"
            write_bar_series_bin(bin_path, series, sha)
            # Atomic write leaves no stray tmp files
            self.assertEqual(["mu_15m.bin"], [p.name for p in Path(tmp).iterdir()])
            # 72B header + symbol + interval + exactly 48 bytes/bar (BAR_RECORD_STRUCT.size)
            expected_bytes = 72 + len(b"MU-USDT-SWAP") + len(b"15m") + (len(candles) * BAR_RECORD_STRUCT.size)
            self.assertEqual(expected_bytes, bin_path.stat().st_size)

            loaded = read_bar_series_bin(bin_path, expected_sha256=sha)
            self.assertEqual(series.symbol, loaded.symbol)
            self.assertEqual(series.interval, loaded.interval)
            self.assertEqual(series.interval_ms, loaded.interval_ms)
            self.assertTrue(loaded.is_uniform_close_times)
            self.assertEqual(candles, loaded.to_candles())
            self.assertIsInstance(loaded.open_times, array)
            self.assertEqual("q", loaded.open_times.typecode)
            self.assertIsInstance(loaded.close_times, array)
            self.assertEqual("q", loaded.close_times.typecode)
            self.assertIsInstance(loaded.closes, array)
            self.assertEqual("d", loaded.closes.typecode)
            self.assertEqual(list(series.closes), list(loaded.closes))
            # Re-serializing a loaded BarSeries round-trips cleanly
            re_path = Path(tmp) / "re_mu_15m.bin"
            write_bar_series_bin(re_path, loaded, sha)
            self.assertEqual(bin_path.read_bytes(), re_path.read_bytes())

            # Empty series roundtrip
            empty_series = BarSeries.from_candles("MU-USDT-SWAP", "15m", [])
            empty_path = Path(tmp) / "empty.bin"
            empty_sha = "0" * 64
            write_bar_series_bin(empty_path, empty_series, empty_sha)
            loaded_empty = read_bar_series_bin(empty_path, expected_sha256=empty_sha)
            self.assertEqual(0, len(loaded_empty))

            # Non-uniform close_times roundtrip
            non_uniform = BarSeries(
                symbol="US:MU",
                interval="1d",
                interval_ms=86_400_000,
                open_times=array("q", [1_000, 100_000]),
                close_times=array("q", [25_000, 130_000]),
                opens=array("d", [100.0, 101.0]),
                highs=array("d", [102.0, 103.0]),
                lows=array("d", [99.0, 100.0]),
                closes=array("d", [101.0, 102.0]),
                volumes=array("d", [50.0, 60.0]),
            )
            self.assertFalse(non_uniform.is_uniform_close_times)
            nu_path = Path(tmp) / "non_uniform.bin"
            write_bar_series_bin(nu_path, non_uniform, sha)
            loaded_nu = read_bar_series_bin(nu_path, expected_sha256=sha)
            self.assertFalse(loaded_nu.is_uniform_close_times)
            self.assertEqual([25_000, 130_000], list(loaded_nu.close_times))

    def test_corrupt_bin_files_fail_closed(self) -> None:
        candles = _make_candles(20)
        series = BarSeries.from_candles("MU-USDT-SWAP", "15m", candles)
        sha = candles_content_sha256(candles)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid_path = root / "valid.bin"
            write_bar_series_bin(valid_path, series, sha)
            raw = valid_path.read_bytes()

            # 1. Missing file
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(root / "missing.bin", expected_sha256=sha)

            # 2. Wrong expected_sha256
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(valid_path, expected_sha256="f" * 64)

            # 3. Truncated header
            p = root / "trunc_hdr.bin"
            p.write_bytes(raw[:16])
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(p, expected_sha256=sha)

            # 4. Bad magic
            bad_magic = bytearray(raw)
            bad_magic[0:8] = b"BADMAGIC"
            p = root / "bad_magic.bin"
            p.write_bytes(bad_magic)
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(p, expected_sha256=sha)

            # 5. Unsupported version
            bad_ver = bytearray(raw)
            struct.pack_into("<H", bad_ver, 8, BAR_BIN_VERSION + 1)
            p = root / "bad_ver.bin"
            p.write_bytes(bad_ver)
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(p, expected_sha256=sha)

            # 6. Unknown flags
            bad_flags = bytearray(raw)
            struct.pack_into("<H", bad_flags, 10, 0x0080)
            p = root / "bad_flags.bin"
            p.write_bytes(bad_flags)
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(p, expected_sha256=sha)

            # 7. Truncated payload
            p = root / "trunc_payload.bin"
            p.write_bytes(raw[:-8])
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(p, expected_sha256=sha)

            # 8. Trailing garbage bytes
            p = root / "trailing.bin"
            p.write_bytes(raw + b"\x00")
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(p, expected_sha256=sha)

            # 9. Single-bit flip in header interval_ms (must fail CRC!)
            flipped_hdr = bytearray(raw)
            flipped_hdr[12] ^= 0x01
            p = root / "flip_hdr.bin"
            p.write_bytes(flipped_hdr)
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(p, expected_sha256=sha)

            # 10. Single-bit flip in middle of payload (must fail CRC!)
            flipped_body = bytearray(raw)
            flipped_body[len(raw) // 2] ^= 0x01
            p = root / "flip_body.bin"
            p.write_bytes(flipped_body)
            with self.assertRaises(BarSeriesCacheError):
                read_bar_series_bin(p, expected_sha256=sha)

    def test_load_or_build_rebuilds_on_hash_change_and_respects_strict_cache(self) -> None:
        candles_v1 = _make_candles(12)
        candles_v2 = _make_candles(15)
        with TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "series.bin"
            s1 = load_or_build_bar_series(candles_v1, symbol="BTCUSDT", interval="15m", cache_path=cache_file)
            self.assertEqual(12, len(s1))
            self.assertTrue(cache_file.exists())

            # Warm cache hit
            s1_hit = load_or_build_bar_series(candles_v1, symbol="BTCUSDT", interval="15m", cache_path=cache_file)
            self.assertEqual(candles_v1, s1_hit.to_candles())

            # Source changed -> strict_cache=True fails closed; default rebuilds atomically
            with self.assertRaises(BarSeriesCacheError):
                load_or_build_bar_series(
                    candles_v2,
                    symbol="BTCUSDT",
                    interval="15m",
                    cache_path=cache_file,
                    strict_cache=True,
                )
            s2 = load_or_build_bar_series(candles_v2, symbol="BTCUSDT", interval="15m", cache_path=cache_file)
            self.assertEqual(15, len(s2))
            self.assertEqual(candles_v2, s2.to_candles())

            # Identical candles + same cache_path with different symbol/interval/interval_ms
            # must NOT silently return stale metadata
            with self.assertRaises(BarSeriesCacheError):
                load_or_build_bar_series(
                    candles_v2,
                    symbol="ETHUSDT",
                    interval="15m",
                    cache_path=cache_file,
                    strict_cache=True,
                )
            rebuilt_sym = load_or_build_bar_series(
                candles_v2,
                symbol="ETHUSDT",
                interval="15m",
                cache_path=cache_file,
            )
            self.assertEqual("ETHUSDT", rebuilt_sym.symbol)
            self.assertEqual("15m", rebuilt_sym.interval)
            self.assertEqual(MIN_15_MS, rebuilt_sym.interval_ms)

            with self.assertRaises(BarSeriesCacheError):
                load_or_build_bar_series(
                    candles_v2,
                    symbol="ETHUSDT",
                    interval="1h",
                    cache_path=cache_file,
                    strict_cache=True,
                )
            rebuilt_iv = load_or_build_bar_series(
                candles_v2,
                symbol="ETHUSDT",
                interval="1h",
                cache_path=cache_file,
            )
            self.assertEqual("1h", rebuilt_iv.interval)
            self.assertEqual(HOUR_1_MS, rebuilt_iv.interval_ms)

    def test_warm_binary_load_under_1ms_on_data_live(self) -> None:
        if not (LIVE_DATA_DIR / "current.json").exists():
            self.skipTest("data/live not present")
        store = TrustedDataStore(data_dir=LIVE_DATA_DIR)
        manifest_res = store.read_manifest()
        self.assertTrue(manifest_res.ok)
        assert manifest_res.snapshot is not None
        health = manifest_res.snapshot.datasets[("MU-USDT-SWAP", "15m")]
        expected_sha = health.content_sha256
        assert expected_sha is not None

        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            # Cold build from TrustedDataStore CSV -> writes .bin in temp dir
            built = load_or_build_bar_series(
                store,
                symbol="MU-USDT-SWAP",
                interval="15m",
                cache_dir=cache_dir,
            )
            self.assertGreaterEqual(len(built), 13_440)
            bin_files = list(cache_dir.rglob("*.bin"))
            self.assertEqual(1, len(bin_files))
            bin_path = bin_files[0]

            # Warm OS page cache once, then measure median across 15 reads
            warmed = read_bar_series_bin(bin_path, expected_sha256=expected_sha)
            self.assertEqual(len(built), len(warmed))
            self.assertEqual(built[0], warmed[0])
            self.assertEqual(built[-1], warmed[-1])

            timings_ms: list[float] = []
            for _ in range(15):
                t0 = time.perf_counter()
                loaded = read_bar_series_bin(bin_path, expected_sha256=expected_sha)
                t1 = time.perf_counter()
                self.assertEqual(len(built), len(loaded))
                timings_ms.append((t1 - t0) * 1000.0)

            best_ms = min(timings_ms)
            median_ms = statistics.median(timings_ms)
            strict_perf = os.environ.get("MU_STRICT_PERF") == "1"
            budget_ms = 1.0 if strict_perf else 3.0
            self.assertLess(
                best_ms,
                budget_ms,
                f"Expected warm .bin load of {len(built)} bars < {budget_ms:.1f} ms "
                f"(MU_STRICT_PERF={int(strict_perf)}), got min={best_ms:.3f} ms "
                f"(median={median_ms:.3f} ms, max={max(timings_ms):.3f} ms)",
            )


class MarketViewCausalityTests(unittest.TestCase):
    def test_strict_no_lookahead_single_and_multi_timeframe(self) -> None:
        # Align to an exact UTC hour boundary
        base_ms = 1_700_006_400_000  # divisible by 3_600_000
        bars_15m = BarSeries.from_candles("MU-USDT-SWAP", "15m", _make_candles(12, start_ms=base_ms, step_ms=MIN_15_MS))
        bars_1h = BarSeries.from_candles("MU-USDT-SWAP", "1h", _make_candles(3, start_ms=base_ms, step_ms=HOUR_1_MS))
        funding = FundingSeries.from_events(
            "okx:MU-USDT-SWAP",
            [
                FundingRateEvent(base_ms + 2 * MIN_15_MS, 0.0001),
                FundingRateEvent(base_ms + 6 * MIN_15_MS, -0.0002),
                FundingRateEvent(base_ms + 10 * MIN_15_MS, 0.0003),
            ],
        )
        sentinel_features = object()
        view0 = MarketView(
            {("MU-USDT-SWAP", "15m"): bars_15m, ("MU-USDT-SWAP", "1h"): bars_1h},
            now_ms=base_ms,
            funding={"MU-USDT-SWAP": funding},
            features=sentinel_features,
        )

        # At exact open of bar 0: NOTHING has closed yet!
        self.assertEqual(0, len(view0.closed_bars("MU-USDT-SWAP", "15m")))
        self.assertIsNone(view0.latest_closed_bar("MU-USDT-SWAP", "15m"))
        self.assertEqual(0, len(view0.closed_bars("MU-USDT-SWAP", "1h")))
        self.assertEqual((), view0.funding_between("MU-USDT-SWAP", 0, base_ms + 100 * HOUR_1_MS))
        self.assertEqual(0, len(view0.funding_series("MU-USDT-SWAP")))  # type: ignore[arg-type]

        # 1 ms before bar 0 closes: STILL nothing closed!
        view_before_close = view0.at(base_ms + MIN_15_MS - 1)
        self.assertIs(sentinel_features, view_before_close.features)
        self.assertEqual(0, len(view_before_close.closed_bars("MU-USDT-SWAP", "15m")))
        self.assertIsNone(view_before_close.latest_closed_bar("MU-USDT-SWAP", "15m"))

        # At exact close of 15m bar 0: 15m has 1 closed bar; 1h STILL has 0 closed bars!
        view_15m = view0.at(base_ms + MIN_15_MS)
        self.assertEqual(1, len(view_15m.closed_bars("MU-USDT-SWAP", "15m")))
        self.assertEqual(bars_15m[0], view_15m.latest_closed_bar("MU-USDT-SWAP", "15m"))
        self.assertEqual(0, len(view_15m.closed_bars("MU-USDT-SWAP", "1h")))

        # At 1h + 45m (7 closed 15m bars): 1h has ONLY bar 0 (closed at +1h); bar 1 (closes at +2h) is HIDDEN!
        t_1h45 = base_ms + HOUR_1_MS + 3 * MIN_15_MS
        view_1h45 = view0.at(t_1h45)
        closed_15m = view_1h45.closed_bars("MU-USDT-SWAP", "15m")
        closed_1h = view_1h45.closed_bars("MU-USDT-SWAP", "1h")
        self.assertEqual(7, len(closed_15m))
        self.assertEqual(1, len(closed_1h))
        self.assertEqual(bars_15m[6], closed_15m[-1])
        self.assertEqual(bars_1h[0], closed_1h[-1])
        self.assertTrue(all(ct <= t_1h45 for ct in closed_15m.close_times))
        self.assertTrue(all(ct <= t_1h45 for ct in closed_1h.close_times))

        # Funding strictly causal even when caller requests far-future end_ms!
        visible_funding = view_1h45.funding_between("MU-USDT-SWAP", base_ms, base_ms + 99 * HOUR_1_MS)
        self.assertEqual(
            (
                FundingRateEvent(base_ms + 2 * MIN_15_MS, 0.0001),
                FundingRateEvent(base_ms + 6 * MIN_15_MS, -0.0002),
            ),
            visible_funding,
        )
        # Funding event at +10*15m is strictly hidden in funding_series() too!
        fs = view_1h45.funding_series("MU-USDT-SWAP")
        assert fs is not None
        self.assertEqual(2, len(fs))
        self.assertTrue(all(ev.ts_ms <= t_1h45 for ev in fs))

    def test_future_perturbation_invariance_proves_zero_lookahead(self) -> None:
        base_ms = 1_700_006_400_000
        orig_candles = _make_candles(10, start_ms=base_ms, step_ms=MIN_15_MS)
        perturbed_candles = list(orig_candles)
        # Wildly alter bars 5..9 (future relative to close of bar 4)
        for i in range(5, 10):
            c = perturbed_candles[i]
            perturbed_candles[i] = Candle(
                open_time_ms=c.open_time_ms,
                open=c.open * 10.0,
                high=c.high * 12.0,
                low=c.low * 8.0,
                close=c.close * 11.0,
                volume=c.volume * 99.0,
            )

        cutoff_ms = orig_candles[4].open_time_ms + MIN_15_MS
        view_a = MarketView({("MU-USDT-SWAP", "15m"): BarSeries.from_candles("MU-USDT-SWAP", "15m", orig_candles)}, cutoff_ms)
        view_b = MarketView({("MU-USDT-SWAP", "15m"): BarSeries.from_candles("MU-USDT-SWAP", "15m", perturbed_candles)}, cutoff_ms)

        self.assertEqual(
            view_a.closed_bars("MU-USDT-SWAP", "15m").to_candles(),
            view_b.closed_bars("MU-USDT-SWAP", "15m").to_candles(),
        )
        self.assertEqual(
            view_a.latest_closed_bar("MU-USDT-SWAP", "15m"),
            view_b.latest_closed_bar("MU-USDT-SWAP", "15m"),
        )


if __name__ == "__main__":
    unittest.main()
