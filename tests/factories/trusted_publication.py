from __future__ import annotations

import json
from pathlib import Path

from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
from mu_strategy.market_data.trusted_data.validation import aggregate_candles
from mu_strategy.models import Candle


SYMBOL = "BTC-USDT-SWAP"


def candles(*, count: int = 12, offset: int = 0) -> list[Candle]:
    return [
        Candle((offset + index) * 300_000, 100.0 + offset + index, 101.0 + offset + index, 99.0 + offset + index, 100.0 + offset + index, 10.0)
        for index in range(count)
    ]


def candles_by_interval(*, offset: int = 0) -> dict[str, list[Candle]]:
    five = candles(offset=offset)
    return {
        "5m": five,
        "15m": aggregate_candles(five, interval="15m"),
        "1h": aggregate_candles(five, interval="1h"),
    }


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
        extra = Candle(next_time_ms, 112.0, 113.0, 111.0, 112.0, 20.0)
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


def read_current(data_dir: Path) -> dict:
    return json.loads((data_dir / "current.json").read_text(encoding="utf-8"))


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


def write_generation_pointer(data_dir: Path, *, generation_id: str, manifest: dict) -> Path:
    generation_dir = data_dir / "generations" / generation_id
    generation_dir.mkdir(parents=True)
    (generation_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (data_dir / "current.json").write_text(
        json.dumps({"schema_version": 1, "generation_id": generation_id, "manifest": f"generations/{generation_id}/manifest.json"}),
        encoding="utf-8",
    )
    return generation_dir
