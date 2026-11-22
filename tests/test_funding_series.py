from __future__ import annotations

from array import array
from bisect import bisect_right
from dataclasses import FrozenInstanceError
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.parse

from mu_strategy.market_data.funding import (
    FundingRateEvent,
    FundingSeries,
    MissingFundingHistoryError,
    fetch_binance_funding_history,
    fetch_okx_funding_history,
    format_apr_scenario_key,
    require_funding_series,
    require_or_sensitivity_series,
)


HOUR_MS = 3_600_000
EIGHT_HOURS_MS = 8 * HOUR_MS


class FundingSeriesTests(unittest.TestCase):
    def test_signed_rates_immutability_and_half_open_between(self) -> None:
        ts_in = array("q", [1000, 2000, 3000, 4000])
        rates_in = array("d", [0.0001, -0.00025, -0.0, 0.0004])
        series = FundingSeries(
            instrument_id="okx:MU-USDT-SWAP",
            timestamps_ms=ts_in,
            rates=rates_in,
            settlement_interval_hours=8,
        )
        # Defensive copy: mutating caller arrays does not alter series
        ts_in[0] = 999_999
        rates_in[0] = 9.9
        self.assertEqual(1000, series[0].ts_ms)
        self.assertAlmostEqual(0.0001, series[0].rate)
        # -0.0 normalized to +0.0
        self.assertEqual(1.0, math.copysign(1.0, series[2].rate))
        # Exact signed negative rate preserved
        self.assertAlmostEqual(-0.00025, series[1].rate)

        with self.assertRaises(FrozenInstanceError):
            series.instrument_id = "other"  # type: ignore[misc]

        # CR1-15 + CR2-03 + CR2-05: public columns are independent read-only views backed by immutable bytes
        ts_view = series.timestamps_ms
        rate_view = series.rates
        self.assertIsInstance(ts_view, memoryview)
        self.assertIsInstance(rate_view, memoryview)
        self.assertTrue(ts_view.readonly)
        self.assertTrue(rate_view.readonly)
        self.assertIsInstance(ts_view.obj, bytes)
        self.assertIsInstance(rate_view.obj, bytes)
        self.assertEqual(2, bisect_right(ts_view, 2000))
        with self.assertRaises(TypeError):
            ts_view[0] = 9999  # type: ignore[index]
        with self.assertRaises(TypeError):
            rate_view[0] = -1.0  # type: ignore[index]
        # Releasing caller's view handles must NOT invalidate series or later callers
        with ts_view:
            self.assertEqual(1000, ts_view[0])
        with rate_view:
            self.assertAlmostEqual(0.0001, rate_view[0])
        with self.assertRaises(ValueError):
            _ = ts_view[0]
        self.assertEqual(1000, series.timestamps_ms[0])
        self.assertAlmostEqual(0.0001, series.rates[0])
        self.assertEqual(2, bisect_right(series.timestamps_ms, 2000))

        # CR2-41: strided memoryview slices remain valid constructor inputs
        strided_series = FundingSeries(
            "okx:MU-USDT-SWAP",
            series.timestamps_ms[::2],
            series.rates[::2],
            settlement_interval_hours=16,
        )
        self.assertEqual([1000, 3000], list(strided_series.timestamps_ms))
        self.assertEqual([0.0001, 0.0], list(strided_series.rates))

        # Half-open (start_ms, end_ms] semantics: start excluded, end included
        self.assertEqual(
            (FundingRateEvent(2000, -0.00025), FundingRateEvent(3000, 0.0)),
            series.between(1000, 3000),
        )
        self.assertEqual(
            (FundingRateEvent(1000, 0.0001),),
            series.between(0, 1000),
        )
        self.assertEqual((), series.between(3000, 3000))
        self.assertEqual((), series.between(4000, 2000))
        self.assertEqual((), series.between(4000, 9000))
        self.assertAlmostEqual(0.00015, series.rate_sum_between(1000, 4000), places=12)

    def test_validation_rejects_unsorted_duplicate_and_non_finite(self) -> None:
        with self.assertRaises(ValueError):
            FundingSeries("okx:MU", array("q", [2000, 1000]), array("d", [0.0001, 0.0001]))
        with self.assertRaises(ValueError):
            FundingSeries("okx:MU", array("q", [1000, 1000]), array("d", [0.0001, 0.0001]))
        with self.assertRaises(ValueError):
            FundingSeries("okx:MU", array("q", [-10]), array("d", [0.0001]))
        with self.assertRaises(ValueError):
            FundingSeries("okx:MU", array("q", [1000]), array("d", [math.nan]))
        with self.assertRaises(ValueError):
            FundingSeries("okx:MU", array("q", [1000]), array("d", [math.inf]))
        with self.assertRaises(ValueError):
            FundingSeries("okx:MU", array("q", [1000, 2000]), array("d", [0.0001]))
        with self.assertRaises(ValueError):
            FundingSeries("okx:MU", array("q", [1000]), array("d", [0.0]), settlement_interval_hours=0)

    def test_json_roundtrip_and_compat_loaders(self) -> None:
        events = [
            FundingRateEvent(1_700_006_400_000, 0.0001),
            FundingRateEvent(1_700_035_200_000, -0.00015),
            FundingRateEvent(1_700_064_000_000, 0.0),
        ]
        series = FundingSeries.from_events("okx:MU-USDT-SWAP", events)
        self.assertEqual(8, series.settlement_interval_hours)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "funding.json"
            series.to_json_file(canonical)
            # No leftover tmp files
            self.assertEqual(["funding.json"], [p.name for p in root.iterdir()])
            loaded = FundingSeries.from_json_file(canonical)
            self.assertEqual(series.instrument_id, loaded.instrument_id)
            self.assertEqual(series.settlement_interval_hours, loaded.settlement_interval_hours)
            self.assertEqual(list(series), list(loaded))
            # CR2-01: canonical archive rejects mismatched venue/symbol, allows exact or symbol-only
            self.assertEqual(
                "okx:MU-USDT-SWAP",
                FundingSeries.from_json_file(canonical, instrument_id="okx:MU-USDT-SWAP").instrument_id,
            )
            self.assertEqual(
                "MU-USDT-SWAP",
                FundingSeries.from_json_file(canonical, instrument_id="MU-USDT-SWAP").instrument_id,
            )
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(canonical, instrument_id="binance:MU-USDT-SWAP")
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(canonical, instrument_id="okx:BTC-USDT-SWAP")

            # Raw OKX archive compatibility (CR1-11: realizedRate required by default; legacy opt-in explicit)
            okx_archive = root / "okx_raw.json"
            okx_archive.write_text(
                json.dumps(
                    {
                        "instId": "MU-USDT-SWAP",
                        "records": [
                            {"instId": "MU-USDT-SWAP", "fundingTime": "1700035200000", "fundingRate": "0.0005", "realizedRate": "-0.00015"},
                            {"instId": "MU-USDT-SWAP", "fundingTime": "1700006400000", "fundingRate": "0.0001", "realizedRate": ""},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(okx_archive)
            okx_loaded = FundingSeries.from_json_file(okx_archive, allow_legacy_okx_funding_rate=True)
            self.assertEqual("okx:MU-USDT-SWAP", okx_loaded.instrument_id)
            self.assertEqual(
                [FundingRateEvent(1_700_006_400_000, 0.0001), FundingRateEvent(1_700_035_200_000, -0.00015)],
                list(okx_loaded),
            )

            # Raw Binance list compatibility + CR1-14 mixed-instrument rejection
            binance_archive = root / "binance_raw.json"
            binance_archive.write_text(
                json.dumps(
                    [
                        {"symbol": "BTCUSDT", "fundingTime": 1_700_006_400_000, "fundingRate": "0.00010000"},
                        {"symbol": "BTCUSDT", "fundingTime": 1_700_035_200_000, "fundingRate": "-0.00005000"},
                    ]
                ),
                encoding="utf-8",
            )
            binance_loaded = FundingSeries.from_json_file(binance_archive)
            self.assertEqual("binance:BTCUSDT", binance_loaded.instrument_id)
            self.assertEqual(2, len(binance_loaded))
            self.assertAlmostEqual(-0.00005, binance_loaded[1].rate)

            # Explicit mismatched symbol OR venue (CR2-01) must fail closed; symbol-only allowed
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(binance_archive, instrument_id="binance:ETHUSDT")
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(binance_archive, instrument_id="okx:BTCUSDT")
            self.assertEqual(
                "BTCUSDT",
                FundingSeries.from_json_file(binance_archive, instrument_id="BTCUSDT").instrument_id,
            )
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(
                    okx_archive,
                    instrument_id="binance:MU-USDT-SWAP",
                    allow_legacy_okx_funding_rate=True,
                )

            # Mixed-instrument Binance archive must fail closed even with explicit instrument_id
            mixed_binance = root / "binance_mixed.json"
            mixed_binance.write_text(
                json.dumps(
                    [
                        {"symbol": "BTCUSDT", "fundingTime": 1_700_006_400_000, "fundingRate": "0.0001"},
                        {"symbol": "ETHUSDT", "fundingTime": 1_700_035_200_000, "fundingRate": "0.0002"},
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(mixed_binance)
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(mixed_binance, instrument_id="binance:BTCUSDT")

            missing_sym_binance = root / "binance_missing_sym.json"
            missing_sym_binance.write_text(
                json.dumps([{"fundingTime": 1_700_006_400_000, "fundingRate": "0.0001"}]),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(missing_sym_binance, instrument_id="binance:BTCUSDT")

            # Corrupt / malformed JSON fails closed
            bad_json = root / "bad.json"
            bad_json.write_text(json.dumps({"schema_version": 99, "instrument_id": "x", "events": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(bad_json)

            missing_field = root / "missing.json"
            missing_field.write_text(
                json.dumps({"schema_version": 1, "instrument_id": "x", "events": [{"ts_ms": 1000}]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                FundingSeries.from_json_file(missing_field)

    def test_missing_funding_forces_explicit_sensitivity_band_never_silent_zero(self) -> None:
        start_ms = 0
        one_year_ms = 365 * 24 * HOUR_MS

        # Strictly required helper fails closed on None or empty series
        with self.assertRaises(MissingFundingHistoryError):
            require_funding_series(None, instrument_id="okx:MU-USDT-SWAP")
        empty = FundingSeries.from_events("okx:MU-USDT-SWAP", ())
        with self.assertRaises(MissingFundingHistoryError):
            require_funding_series(empty, instrument_id="okx:MU-USDT-SWAP")

        # Historical series returned directly ONLY when it fully covers the requested window
        hist_complete = FundingSeries.from_events(
            "okx:MU-USDT-SWAP",
            [FundingRateEvent(EIGHT_HOURS_MS, 0.0001), FundingRateEvent(2 * EIGHT_HOURS_MS, 0.0001)],
            settlement_interval_hours=8,
        )
        res_hist = require_or_sensitivity_series(
            hist_complete,
            instrument_id="okx:MU-USDT-SWAP",
            start_ms=start_ms,
            end_ms=2 * EIGHT_HOURS_MS,
            interval_hours=8,
        )
        self.assertTrue(res_hist.is_historical)
        self.assertEqual([("historical", hist_complete)], list(res_hist.scenarios))

        # CR2-02: reject mismatched requested instrument_id before accepting historical funding,
        # while omitting instrument_id resolves by the series' own identity
        res_hist_own = require_or_sensitivity_series(
            hist_complete,
            start_ms=start_ms,
            end_ms=2 * EIGHT_HOURS_MS,
            interval_hours=8,
        )
        self.assertTrue(res_hist_own.is_historical)
        with self.assertRaises(ValueError):
            require_or_sensitivity_series(
                hist_complete,
                instrument_id="binance:MU-USDT-SWAP",
                start_ms=start_ms,
                end_ms=2 * EIGHT_HOURS_MS,
                interval_hours=8,
            )

        # CR1-10: partial historical series MUST fail closed rather than be labeled historical
        with self.assertRaises(MissingFundingHistoryError):
            require_or_sensitivity_series(
                hist_complete,
                instrument_id="okx:MU-USDT-SWAP",
                start_ms=start_ms,
                end_ms=one_year_ms,
                interval_hours=8,
            )

        # CR1-12: distinct close APRs keep distinct keys without :g collisions
        res_close = require_or_sensitivity_series(
            None,
            instrument_id="okx:MU-USDT-SWAP",
            start_ms=start_ms,
            end_ms=2 * EIGHT_HOURS_MS,
            interval_hours=8,
            apr_band=(0.15, 0.1500001, 0.15000001),
        )
        close_dict = res_close.as_dict()
        self.assertEqual(3, len(close_dict))
        self.assertEqual(
            {"apr_15pct", "apr_15.00001pct", "apr_15.000001pct"},
            set(close_dict.keys()),
        )
        self.assertEqual("historical+apr_15.000001pct", format_apr_scenario_key(0.15000001, prefix="historical+apr_"))

        # Missing series synthesizes explicit 0% / +15% / +30% APR band
        res_sens = require_or_sensitivity_series(
            None,
            instrument_id="okx:MU-USDT-SWAP",
            start_ms=start_ms,
            end_ms=one_year_ms,
            interval_hours=8,
            apr_band=(0.0, 0.15, 0.30),
        )
        self.assertTrue(res_sens.is_sensitivity)
        self.assertEqual((0.0, 0.15, 0.30), res_sens.apr_band)
        by_name = res_sens.as_dict()
        self.assertEqual(["apr_0pct", "apr_15pct", "apr_30pct"], list(by_name))
        # 365 * 3 = 1095 settlements/year; summing rates over 1 year equals exact APR
        self.assertEqual(1095, len(by_name["apr_15pct"]))
        self.assertAlmostEqual(0.0, by_name["apr_0pct"].rate_sum_between(start_ms, one_year_ms), places=12)
        self.assertAlmostEqual(0.15, by_name["apr_15pct"].rate_sum_between(start_ms, one_year_ms), places=12)
        self.assertAlmostEqual(0.30, by_name["apr_30pct"].rate_sum_between(start_ms, one_year_ms), places=12)

        # NEVER allow silent single-zero or all-zero or empty band
        for bad_band in ((), (0.0,), (0.0, 0.0), (0.15,), (0.0, 0.15, 0.15)):
            with self.assertRaises(ValueError):
                require_or_sensitivity_series(
                    None,
                    instrument_id="okx:MU-USDT-SWAP",
                    start_ms=start_ms,
                    end_ms=one_year_ms,
                    interval_hours=8,
                    apr_band=bad_band,
                )

        # Missing interval_hours raises MissingFundingHistoryError
        with self.assertRaises(MissingFundingHistoryError):
            require_or_sensitivity_series(
                None,
                instrument_id="okx:MU-USDT-SWAP",
                start_ms=start_ms,
                end_ms=one_year_ms,
                interval_hours=None,
            )

    def test_coverage_gaps_leading_interior_trailing_and_fill_gaps_with_apr(self) -> None:
        t0 = 1_700_006_400_000  # exact 8h UTC boundary
        # Full contiguous 6-period series (t0 + 1..6 * 8h): zero gaps
        full = FundingSeries.from_events(
            "okx:MU-USDT-SWAP",
            [FundingRateEvent(t0 + i * EIGHT_HOURS_MS, 0.0001) for i in range(1, 7)],
            settlement_interval_hours=8,
        )
        self.assertEqual((), full.coverage_gaps(t0, t0 + 6 * EIGHT_HOURS_MS, 8))
        self.assertEqual(1.0, full.coverage_ratio(t0, t0 + 6 * EIGHT_HOURS_MS, 8))

        # Leading (0..3), interior (4..7 missing 5,6), and trailing (8..11) gaps
        partial = FundingSeries.from_events(
            "okx:MU-USDT-SWAP",
            [
                FundingRateEvent(t0 + 3 * EIGHT_HOURS_MS, 0.0002),
                FundingRateEvent(t0 + 4 * EIGHT_HOURS_MS, -0.0001),
                FundingRateEvent(t0 + 7 * EIGHT_HOURS_MS, 0.0003),
                FundingRateEvent(t0 + 8 * EIGHT_HOURS_MS, 0.00005),
            ],
            settlement_interval_hours=8,
        )
        gaps = partial.coverage_gaps(t0, t0 + 11 * EIGHT_HOURS_MS, 8, tolerance=1.5)
        self.assertEqual(
            (
                (t0, t0 + 3 * EIGHT_HOURS_MS),
                (t0 + 4 * EIGHT_HOURS_MS, t0 + 7 * EIGHT_HOURS_MS),
                (t0 + 8 * EIGHT_HOURS_MS, t0 + 11 * EIGHT_HOURS_MS),
            ),
            gaps,
        )
        ratio = partial.coverage_ratio(t0, t0 + 11 * EIGHT_HOURS_MS, 8, tolerance=1.5)
        self.assertAlmostEqual(2.0 / 11.0, ratio, places=12)

        # Filling gaps preserves exact historical rates and closes every gap
        filled = partial.fill_gaps_with_apr(
            start_ms=t0,
            end_ms=t0 + 11 * EIGHT_HOURS_MS,
            interval_hours=8,
            apr=0.15,
            tolerance=1.5,
        )
        self.assertEqual((), filled.coverage_gaps(t0, t0 + 11 * EIGHT_HOURS_MS, 8, tolerance=1.5))
        self.assertEqual(11, len(filled))
        by_ts = {ev.ts_ms: ev.rate for ev in filled}
        # Historical events untouched
        self.assertAlmostEqual(0.0002, by_ts[t0 + 3 * EIGHT_HOURS_MS], places=12)
        self.assertAlmostEqual(-0.0001, by_ts[t0 + 4 * EIGHT_HOURS_MS], places=12)
        self.assertAlmostEqual(0.0003, by_ts[t0 + 7 * EIGHT_HOURS_MS], places=12)
        self.assertAlmostEqual(0.00005, by_ts[t0 + 8 * EIGHT_HOURS_MS], places=12)
        # Synthetic gap events match 15% APR / 1095 settlements
        expected_synth = 0.15 / (365.0 * 3.0)
        for missing_idx in (1, 2, 5, 6, 9, 10, 11):
            self.assertAlmostEqual(expected_synth, by_ts[t0 + missing_idx * EIGHT_HOURS_MS], places=12)

        # Empty series: window crossing settlement at exact end_ms detects gap (CR1-09);
        # intra-interval window with no due settlement detects none
        empty = FundingSeries.from_events("okx:MU-USDT-SWAP", (), settlement_interval_hours=8)
        self.assertEqual(((t0, t0 + 3 * EIGHT_HOURS_MS),), empty.coverage_gaps(t0, t0 + 3 * EIGHT_HOURS_MS, 8))
        self.assertEqual(0.0, empty.coverage_ratio(t0, t0 + 3 * EIGHT_HOURS_MS, 8))
        self.assertEqual(((t0, t0 + EIGHT_HOURS_MS),), empty.coverage_gaps(t0, t0 + EIGHT_HOURS_MS, 8))
        self.assertEqual((), empty.coverage_gaps(t0 + HOUR_MS, t0 + 7 * HOUR_MS, 8))

        # CR1-09: missing settlement right at end_ms (e.g. events at 8h & 16h, query (8h, 24h])
        edge_missing = FundingSeries.from_events(
            "okx:MU-USDT-SWAP",
            [
                FundingRateEvent(t0 + EIGHT_HOURS_MS, 0.0001),
                FundingRateEvent(t0 + 2 * EIGHT_HOURS_MS, 0.0001),
            ],
            settlement_interval_hours=8,
        )
        self.assertEqual(
            ((t0 + 2 * EIGHT_HOURS_MS, t0 + 3 * EIGHT_HOURS_MS),),
            edge_missing.coverage_gaps(t0 + EIGHT_HOURS_MS, t0 + 3 * EIGHT_HOURS_MS, 8, tolerance=1.5),
        )
        # Missing leading edge settlement at 16h in (8h, 24h] when only 24h is present
        lead_missing = FundingSeries.from_events(
            "okx:MU-USDT-SWAP",
            [FundingRateEvent(t0 + 3 * EIGHT_HOURS_MS, 0.0001)],
            settlement_interval_hours=8,
        )
        self.assertEqual(
            ((t0 + EIGHT_HOURS_MS, t0 + 3 * EIGHT_HOURS_MS),),
            lead_missing.coverage_gaps(t0 + EIGHT_HOURS_MS, t0 + 3 * EIGHT_HOURS_MS, 8, tolerance=1.5),
        )

        # CR1-13: phase-aligned gap fill on offset schedule (UTC hours 1 and 17 -> inserts ONLY hour 9)
        offset_series = FundingSeries.from_events(
            "okx:OFFSET-SWAP",
            [
                FundingRateEvent(t0 + 1 * HOUR_MS, 0.0001),
                FundingRateEvent(t0 + 17 * HOUR_MS, 0.0002),
            ],
            settlement_interval_hours=8,
        )
        filled_offset = offset_series.fill_gaps_with_apr(
            start_ms=t0,
            end_ms=t0 + 17 * HOUR_MS,
            interval_hours=8,
            apr=0.15,
        )
        self.assertEqual(
            [t0 + 1 * HOUR_MS, t0 + 9 * HOUR_MS, t0 + 17 * HOUR_MS],
            list(filled_offset.timestamps_ms),
        )
        self.assertEqual((), filled_offset.coverage_gaps(t0, t0 + 17 * HOUR_MS, 8))

        # CR1-13: reject gap fill when phase cannot be determined (mixed phases or empty without phase_ms)
        mixed_phase = FundingSeries.from_events(
            "okx:MIXED-SWAP",
            [
                FundingRateEvent(t0 + 1 * HOUR_MS, 0.0001),
                FundingRateEvent(t0 + 4 * HOUR_MS, 0.0002),
            ],
            settlement_interval_hours=8,
        )
        with self.assertRaises(ValueError):
            mixed_phase.fill_gaps_with_apr(start_ms=t0, end_ms=t0 + 24 * HOUR_MS, interval_hours=8, apr=0.15)
        with self.assertRaises(ValueError):
            empty.fill_gaps_with_apr(start_ms=t0, end_ms=t0 + 24 * HOUR_MS, interval_hours=8, apr=0.15)

        # CR2-04: explicit phase_ms applies to gap detection as well as gap filling (even on empty series)
        self.assertEqual(
            ((t0, t0 + 2 * HOUR_MS),),
            empty.coverage_gaps(t0, t0 + 2 * HOUR_MS, 8, phase_ms=1 * HOUR_MS),
        )
        filled_empty_phase = empty.fill_gaps_with_apr(
            start_ms=t0,
            end_ms=t0 + 2 * HOUR_MS,
            interval_hours=8,
            apr=0.15,
            phase_ms=1 * HOUR_MS,
        )
        self.assertEqual([t0 + 1 * HOUR_MS], list(filled_empty_phase.timestamps_ms))

        # CR2-06: out-of-window event (hour 25) must NOT hide missing hour 24 in (0h, 24h]
        with_out_of_window = FundingSeries.from_events(
            "okx:MU-USDT-SWAP",
            [
                FundingRateEvent(t0 + EIGHT_HOURS_MS, 0.0001),
                FundingRateEvent(t0 + 2 * EIGHT_HOURS_MS, 0.0001),
                FundingRateEvent(t0 + 25 * HOUR_MS, 0.0001),
            ],
            settlement_interval_hours=8,
        )
        self.assertEqual(
            ((t0 + 2 * EIGHT_HOURS_MS, t0 + 3 * EIGHT_HOURS_MS),),
            with_out_of_window.coverage_gaps(t0, t0 + 3 * EIGHT_HOURS_MS, 8, tolerance=1.5),
        )
        # Ambiguous in-window phases must NOT falsely report complete coverage over edges >= 1 interval
        ambiguous_in_window = FundingSeries.from_events(
            "okx:MU-USDT-SWAP",
            [
                FundingRateEvent(t0 + 8 * HOUR_MS, 0.0001),
                FundingRateEvent(t0 + 13 * HOUR_MS, 0.0001),
                FundingRateEvent(t0 + 16 * HOUR_MS, 0.0001),
            ],
            settlement_interval_hours=8,
        )
        self.assertNotEqual((), ambiguous_in_window.coverage_gaps(t0, t0 + 24 * HOUR_MS, 8, tolerance=1.5))

        # Validation failures
        with self.assertRaises(ValueError):
            full.coverage_gaps(t0 + 1000, t0, 8)
        with self.assertRaises(ValueError):
            full.coverage_gaps(t0, t0 + EIGHT_HOURS_MS, 8, tolerance=0.5)

    def test_fetch_okx_funding_history_paginated_mocked_no_network(self) -> None:
        t0 = 1_700_006_400_000
        # 5 events spaced by 8h; OKX returns newest-first in pages of limit=2
        all_rows = [
            {"instId": "MU-USDT-SWAP", "fundingTime": str(t0 + 4 * EIGHT_HOURS_MS), "fundingRate": "0.0002", "realizedRate": "0.0002"},
            {"instId": "MU-USDT-SWAP", "fundingTime": str(t0 + 3 * EIGHT_HOURS_MS), "fundingRate": "-0.0001", "realizedRate": "-0.0001"},
            {"instId": "MU-USDT-SWAP", "fundingTime": str(t0 + 2 * EIGHT_HOURS_MS), "fundingRate": "0.00099", "realizedRate": "0.00005"},
            {"instId": "MU-USDT-SWAP", "fundingTime": str(t0 + 1 * EIGHT_HOURS_MS), "fundingRate": "0", "realizedRate": "0"},
            {"instId": "MU-USDT-SWAP", "fundingTime": str(t0), "fundingRate": "-0.0003", "realizedRate": "-0.0003"},
        ]
        seen_urls: list[str] = []

        def fake_get_json(url: str, **kwargs):
            seen_urls.append(url)
            parsed = urllib.parse.urlsplit(url)
            qs = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(["MU-USDT-SWAP"], qs["instId"])
            limit = int(qs["limit"][0])
            after = int(qs["after"][0]) if "after" in qs else None
            filtered = [r for r in all_rows if after is None or int(r["fundingTime"]) < after]
            return {"code": "0", "msg": "", "data": filtered[:limit]}

        with (
            patch("socket.create_connection", side_effect=AssertionError("live network forbidden")),
            patch("mu_strategy.market_data.funding.get_json", side_effect=fake_get_json),
        ):
            series = fetch_okx_funding_history(
                "MU-USDT-SWAP",
                start_time_ms=t0 + EIGHT_HOURS_MS,
                end_time_ms=t0 + 4 * EIGHT_HOURS_MS,
                limit=2,
            )

        self.assertEqual("okx:MU-USDT-SWAP", series.instrument_id)
        self.assertEqual(8, series.settlement_interval_hours)
        self.assertEqual(2, len(seen_urls))  # Page 1: [t4, t3]; Page 2: [t2, t1] -> stops at start_time_ms=t1!
        self.assertEqual(
            [
                FundingRateEvent(t0 + 1 * EIGHT_HOURS_MS, 0.0),
                FundingRateEvent(t0 + 2 * EIGHT_HOURS_MS, 0.00005),
                FundingRateEvent(t0 + 3 * EIGHT_HOURS_MS, -0.0001),
                FundingRateEvent(t0 + 4 * EIGHT_HOURS_MS, 0.0002),
            ],
            list(series),
        )

    def test_fetch_okx_funding_history_fails_closed_on_api_error(self) -> None:
        with patch(
            "mu_strategy.market_data.funding.get_json",
            return_value={"code": "51001", "msg": "Instrument ID does not exist", "data": []},
        ):
            with self.assertRaises(RuntimeError):
                fetch_okx_funding_history("INVALID-SWAP")

        # CR1-11: exchange history MUST reject blank/missing realizedRate rather than using predicted fundingRate
        with patch(
            "mu_strategy.market_data.funding.get_json",
            return_value={
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "instId": "MU-USDT-SWAP",
                        "fundingTime": str(1_700_006_400_000),
                        "fundingRate": "0.0005",
                        "realizedRate": "",
                    }
                ],
            },
        ):
            with self.assertRaises(ValueError):
                fetch_okx_funding_history("MU-USDT-SWAP")

    def test_fetch_binance_funding_history_paginated_mocked_no_network(self) -> None:
        t0 = 1_700_006_400_000
        all_rows = [
            {"symbol": "BTCUSDT", "fundingTime": t0 + i * EIGHT_HOURS_MS, "fundingRate": str(rate)}
            for i, rate in enumerate((0.0001, -0.0002, 0.0003, -0.00005, 0.00015))
        ]
        seen_urls: list[str] = []

        def fake_get_json(url: str, **kwargs):
            seen_urls.append(url)
            parsed = urllib.parse.urlsplit(url)
            qs = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(["BTCUSDT"], qs["symbol"])
            limit = int(qs["limit"][0])
            start = int(qs["startTime"][0]) if "startTime" in qs else None
            end = int(qs["endTime"][0]) if "endTime" in qs else None
            filtered = [
                r
                for r in all_rows
                if (start is None or r["fundingTime"] >= start)
                and (end is None or r["fundingTime"] <= end)
            ]
            return filtered[:limit]

        with (
            patch("socket.create_connection", side_effect=AssertionError("live network forbidden")),
            patch("mu_strategy.market_data.funding.get_json", side_effect=fake_get_json),
        ):
            series = fetch_binance_funding_history(
                "BTCUSDT",
                start_time_ms=t0,
                end_time_ms=t0 + 4 * EIGHT_HOURS_MS,
                limit=2,
            )

        self.assertEqual("binance:BTCUSDT", series.instrument_id)
        self.assertEqual(8, series.settlement_interval_hours)
        self.assertEqual(3, len(seen_urls))  # [0,1], [2,3], [4]
        self.assertEqual(5, len(series))
        self.assertAlmostEqual(-0.0002, series[1].rate)
        self.assertAlmostEqual(0.00015, series[4].rate)

    def test_fetch_binance_funding_history_fails_closed_on_bad_payload(self) -> None:
        with patch(
            "mu_strategy.market_data.funding.get_json",
            return_value={"code": -1121, "msg": "Invalid symbol."},
        ):
            with self.assertRaises(RuntimeError):
                fetch_binance_funding_history("INVALID")


if __name__ == "__main__":
    unittest.main()
