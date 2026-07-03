from __future__ import annotations

import json
from pathlib import Path

from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
from mu_strategy.market_data.trusted_data.validation import aggregate_candles
from mu_strategy.models import Candle


SYMBOL = "BTC-USDT-SWAP"


def candles(*, count: int = 288, offset: int = 0) -> list[Candle]:
    return [
        Candle(index * 300_000, 100.0 + offset + index, 101.0 + offset + index, 99.0 + offset + index, 100.0 + offset + index, 10.0)
        for index in range(count)
    ]


def candles_by_interval(*, offset: int = 0) -> dict[str, list[Candle]]:
    five = candles(offset=offset)
    return {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }


def short_candles(interval: str) -> list[Candle]:
    five = [Candle(index * 300_000, 100 + index, 101 + index, 99 + index, 100 + index, 10.0) for index in range(12)]
    if interval == "5m":
        return five
    return aggregate_candles(five, interval=interval)


def constant_candles(timestamps: tuple[int, ...]) -> list[Candle]:
    return [Candle(timestamp, 100.0, 101.0, 99.0, 100.0, 10.0) for timestamp in timestamps]


def range_candles(start_ms: int, end_ms: int, *, step_ms: int = 300_000) -> list[Candle]:
    rows: list[Candle] = []
    timestamp = start_ms
    index = 0
    while timestamp <= end_ms:
        price = 100.0 + index
        rows.append(Candle(timestamp, price, price + 1.0, price - 1.0, price, 1000.0))
        timestamp += step_ms
        index += 1
    return rows


class StaticProvider:
    def __init__(self, *, symbol: str = SYMBOL, offset: int = 0, fail_interval: str | None = None):
        self.symbol = symbol
        self.offset = offset
        self.fail_interval = fail_interval

    def fetch_tickers(self):
        return [{"instId": self.symbol, "last": "100", "volCcy24h": "10"}]

    def fetch_history(self, symbol, interval, *, days):
        if interval == self.fail_interval:
            raise TimeoutError("blocked")
        return candles_by_interval(offset=self.offset)[interval]

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        if interval == self.fail_interval:
            raise TimeoutError("blocked")
        step_ms = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}[interval]
        next_time_ms = since_time_ms + (step_ms if interval == "1h" else step_ms * 2)
        price = 100.0 + self.offset + (next_time_ms // 300_000)
        extra = Candle(next_time_ms, price, price + 1.0, price - 1.0, price, 20.0)
        return [extra]


class UniverseFailureProvider:
    def fetch_tickers(self):
        raise TimeoutError("ticker timeout")

    def fetch_history(self, symbol, interval, *, days):
        raise AssertionError("universe failure must not fetch history")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise AssertionError("universe failure must not fetch incremental")


class HoleyProvider(StaticProvider):
    def fetch_history(self, symbol, interval, *, days):
        if interval == "5m":
            return [Candle(0, 100.0, 101.0, 99.0, 100.0, 10.0), Candle(600_000, 101.0, 102.0, 100.0, 101.0, 10.0)]
        return super().fetch_history(symbol, interval, days=days)


class RecordingProvider:
    def __init__(self, *, ticker_rows=None, fail_history=None, history_fetcher=None):
        self.ticker_rows = ticker_rows or []
        self.fail_history = set(fail_history or ())
        self.history_fetcher = history_fetcher
        self.history_calls = []
        self.incremental_calls = []

    def fetch_tickers(self):
        return list(self.ticker_rows)

    def fetch_history(self, symbol, interval, *, days):
        if (symbol, interval) in self.fail_history:
            raise TimeoutError(f"blocked {symbol} {interval}")
        self.history_calls.append((symbol, interval, days))
        if self.history_fetcher is not None:
            return self.history_fetcher(symbol, interval, days=days)
        return short_candles(interval)

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        self.incremental_calls.append((symbol, interval, since_time_ms))
        return short_candles(interval)


class TickerFailureProvider:
    def fetch_tickers(self):
        raise TimeoutError("ticker timeout")

    def fetch_history(self, symbol, interval, *, days):
        raise AssertionError("must not fetch history")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise AssertionError("must not fetch incremental")


class IncrementalFailureProvider:
    def fetch_tickers(self):
        return [{"instId": SYMBOL, "last": "100", "volCcy24h": "10"}]

    def fetch_history(self, symbol, interval, *, days):
        raise AssertionError("cache should force incremental path")

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        raise TimeoutError("blocked incremental")


class FixedClock:
    def __init__(self, now_ms: int):
        self.now = now_ms
        self.calls = 0

    def now_ms(self) -> int:
        self.calls += 1
        return self.now


class SequenceClock:
    def __init__(self, *values: int):
        self.values = list(values)
        self.calls = 0

    def now_ms(self) -> int:
        self.calls += 1
        if self.values:
            return self.values.pop(0)
        raise AssertionError("sequence clock exhausted")


class TextSink:
    def __init__(self):
        self.values = []

    def write(self, value):
        self.values.append(value)
        return len(value)

    def flush(self):
        return None

    @property
    def text(self):
        return "".join(self.values)


def read_current(data_dir: Path) -> dict:
    return json.loads((data_dir / "current.json").read_text(encoding="utf-8"))


def manifest_path(data_dir: Path) -> Path:
    current = data_dir / "current.json"
    if current.exists():
        return data_dir / read_current(data_dir)["manifest"]
    return TrustedDataStore(data_dir=data_dir).flat_manifest_path


def current_generation_dir(data_dir: Path) -> Path:
    return data_dir / "generations" / read_current(data_dir)["generation_id"]


def generation_manifest(data_dir: Path, generation_id: str) -> dict:
    return json.loads((data_dir / "generations" / generation_id / "manifest.json").read_text(encoding="utf-8"))


def write_flat_v3_publication(data_dir: Path, *, symbol: str = SYMBOL, run_id: str = "flat-run", source_file=None) -> dict:
    store = TrustedDataStore(data_dir=data_dir)
    symbols = {symbol: {"intervals": {}}}
    for interval, rows in candles_by_interval().items():
        path = data_dir / "okx" / symbol / f"{interval}.csv"
        store.write_csv(rows, path)
        source = source_file(symbol, interval, path) if callable(source_file) else source_file
        symbols[symbol]["intervals"][interval] = {
            "symbol": symbol,
            "interval": interval,
            "availability": "available",
            "integrity": "valid",
            "freshness": "fresh",
            "reasons": ["ok"],
            "rows": len(rows),
            "first_timestamp_ms": rows[0].open_time_ms,
            "last_timestamp_ms": rows[-1].open_time_ms,
            "updated_at_ms": 3_600_000,
            "source_file": str(path if source is None else source),
            "content_sha256": candles_content_sha256(rows),
            "validation": {"ok": True, "reason": "ok"},
        }
    manifest = {
        "schema_version": 3,
        "run_id": run_id,
        "attempt_status": "success",
        "snapshot_usability": "usable",
        "started_at_ms": 0,
        "completed_at_ms": 3_600_000,
        "updated_at_ms": 3_600_000,
        "requested_intervals": ["15m", "1h"],
        "effective_intervals": ["5m", "15m", "1h"],
        "intervals": ["5m", "15m", "1h"],
        "universes": {"crypto_top": [{"inst_id": symbol, "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"}], "stock_token_top": []},
        "symbols": symbols,
        "provider_failures": [],
        "warnings": [],
        "cycle_error": None,
    }
    (data_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def write_flat_v2_publication(data_dir: Path, *, symbol: str = SYMBOL, run_id: str = "flat-v2-run", source_file=None) -> dict:
    manifest = write_flat_v3_publication(data_dir, symbol=symbol, run_id=run_id, source_file=source_file)
    manifest.pop("attempt_status")
    manifest.pop("snapshot_usability")
    manifest["schema_version"] = 2
    manifest["outcome"] = "success"
    manifest["status"] = "ok"
    (data_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def write_flat_v1_publication(data_dir: Path, *, symbol: str = SYMBOL, run_id: str = "flat-v1-run", source_file=None) -> dict:
    manifest = write_flat_v2_publication(data_dir, symbol=symbol, run_id=run_id, source_file=source_file)
    manifest["schema_version"] = 1
    manifest.pop("outcome", None)
    (data_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def write_generation_pointer(data_dir: Path, *, generation_id: str, manifest: dict) -> Path:
    generation_dir = data_dir / "generations" / generation_id
    generation_dir.mkdir(parents=True)
    (generation_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (data_dir / "current.json").write_text(
        json.dumps({"schema_version": 1, "generation_id": generation_id, "manifest": f"generations/{generation_id}/manifest.json"}),
        encoding="utf-8",
    )
    return generation_dir


def write_flat_manifest_and_caches(
    data_dir: Path,
    *,
    symbol: str,
    days: int,
    outcome: str = "success",
    status: str = "ok",
    integrity: str = "valid",
    freshness: str = "fresh",
    run_id: str = "run-1",
    universe_symbols: tuple[str, ...] | None = None,
    stock_token_symbols: tuple[str, ...] | None = None,
) -> dict:
    from mu_strategy.market_data.trusted_data.contracts import RefreshAttemptStatus, SnapshotUsability
    from mu_strategy.market_data.utils import DAY_MS

    store = TrustedDataStore(data_dir=data_dir)
    five = [
        Candle(index * 300_000, 100.0 + index, 101.0 + index, 99.0 + index, 100.0 + index, 1000.0)
        for index in range(days * DAY_MS // 300_000)
    ]
    rows_by_interval = {"5m": five, "15m": aggregate_candles(five, interval="15m"), "1h": aggregate_candles(five, interval="1h")}
    previous_symbols = {}
    if manifest_path(data_dir).exists():
        previous_symbols = json.loads(manifest_path(data_dir).read_text(encoding="utf-8")).get("symbols") or {}
    symbols = dict(previous_symbols)
    symbols.setdefault(symbol, {"intervals": {}})
    reason = "ok"
    if integrity == "invalid":
        reason = "refresh_failed"
    elif freshness == "stale":
        reason = "stale_by_clock"
    elif freshness == "unknown":
        reason = "freshness_unknown"
    for interval, rows in rows_by_interval.items():
        path = store.flat_cache_path(symbol, interval)
        store.write_csv(rows, path)
        symbols[symbol]["intervals"][interval] = {
            "symbol": symbol,
            "interval": interval,
            "availability": "available",
            "integrity": integrity,
            "freshness": freshness,
            "reasons": [reason],
            "rows": len(rows),
            "first_timestamp_ms": rows[0].open_time_ms,
            "last_timestamp_ms": rows[-1].open_time_ms,
            "updated_at_ms": 86_400_000,
            "source_file": str(path),
            "content_sha256": candles_content_sha256(rows) if integrity == "valid" else None,
            "validation": {"ok": integrity == "valid", "reason": "ok" if integrity == "valid" else reason},
        }
    manifest = {
        "schema_version": 3,
        "run_id": run_id,
        "attempt_status": (
            RefreshAttemptStatus.FAILED.value
            if outcome == "failed"
            else RefreshAttemptStatus.DEGRADED.value
            if outcome == "partial"
            else RefreshAttemptStatus.SUCCESS.value
        ),
        "snapshot_usability": {
            "ok": SnapshotUsability.USABLE.value,
            "stale": SnapshotUsability.STALE.value,
            "invalid": SnapshotUsability.INVALID.value,
        }.get(status, status),
        "started_at_ms": 0,
        "completed_at_ms": 0,
        "requested_intervals": ["15m", "1h"],
        "effective_intervals": ["5m", "15m", "1h"],
        "universes": {
            "crypto_top": [{"inst_id": item, "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"} for item in (universe_symbols or ())],
            "stock_token_top": [
                {"inst_id": item, "last": 100.0, "volume_ccy_24h": 10.0, "source": "stock_token"} for item in (stock_token_symbols or ())
            ],
        },
        "symbols": symbols,
        "provider_failures": [],
        "warnings": [],
        "cycle_error": {"error_type": "TimeoutError", "message": "blocked"} if outcome == "failed" else None,
    }
    store.write_manifest(manifest)
    return manifest


def write_generation_publication(
    data_dir: Path,
    *,
    symbol: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    candles_by_interval: dict[str, list[Candle]] | None = None,
    run_id: str = "run-coverage",
) -> dict:
    store = TrustedDataStore(data_dir=data_dir)
    if candles_by_interval is None:
        if start_ms is None or end_ms is None:
            raise ValueError("start_ms and end_ms are required without explicit candles")
        five = range_candles(start_ms, end_ms)
        candles_by_interval = {"5m": five, "15m": aggregate_candles(five, interval="15m"), "1h": aggregate_candles(five, interval="1h")}
    store.prepare_generation(run_id)
    completed_at_ms = max(candle.open_time_ms for rows in candles_by_interval.values() for candle in rows)
    symbols = {symbol: {"intervals": {}}}
    for interval, rows in candles_by_interval.items():
        path = store.generation_cache_path(run_id, symbol, interval)
        store.write_csv(rows, path)
        symbols[symbol]["intervals"][interval] = {
            "symbol": symbol,
            "interval": interval,
            "availability": "available",
            "integrity": "valid",
            "freshness": "fresh",
            "reasons": ["ok"],
            "rows": len(rows),
            "first_timestamp_ms": rows[0].open_time_ms,
            "last_timestamp_ms": rows[-1].open_time_ms,
            "updated_at_ms": completed_at_ms,
            "source_file": store.generation_source_file(symbol, interval).as_posix(),
            "content_sha256": candles_content_sha256(rows),
            "validation": {"ok": True, "reason": "ok"},
        }
    manifest = {
        "schema_version": 3,
        "run_id": run_id,
        "attempt_status": "success",
        "snapshot_usability": "usable",
        "started_at_ms": completed_at_ms,
        "completed_at_ms": completed_at_ms,
        "requested_intervals": ["15m", "1h"],
        "effective_intervals": ["5m", "15m", "1h"],
        "universes": {"crypto_top": [{"inst_id": symbol, "last": 100.0, "volume_ccy_24h": 10.0, "source": "top"}], "stock_token_top": []},
        "symbols": symbols,
        "provider_failures": [],
        "warnings": [],
        "cycle_error": None,
    }
    store.write_generation_manifest(run_id, manifest)
    store.replace_current(run_id)
    return manifest


def write_orphan_flat_caches(data_dir: Path, *, symbol: str, days: int) -> None:
    from mu_strategy.market_data.utils import DAY_MS

    store = TrustedDataStore(data_dir=data_dir)
    five = [
        Candle(index * 300_000, 100.0 + index, 101.0 + index, 99.0 + index, 100.0 + index, 1000.0)
        for index in range(days * DAY_MS // 300_000)
    ]
    for interval, rows in {"5m": five, "15m": aggregate_candles(five, interval="15m"), "1h": aggregate_candles(five, interval="1h")}.items():
        store.write_csv(rows, store.flat_cache_path(symbol, interval))
