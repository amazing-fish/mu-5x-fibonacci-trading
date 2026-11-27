from __future__ import annotations

import csv
import io
from pathlib import Path

from mu_strategy.market_data.providers.binance import BASE_URL, fetch_historical, fetch_klines
from mu_strategy.market_data.providers.okx import (
    OKX_BASE_URL,
    fetch_okx_candles,
    fetch_okx_historical,
    fetch_okx_incremental,
    okx_interval,
    okx_row_to_candle,
)
from mu_strategy.market_data.utils import (
    CSV_FIELDS,
    dedupe_candles,
    format_open_time_iso,
    interval_to_ms,
    prune_candles_to_window,
)
from mu_strategy.models import Candle


def write_csv(candles: list[Candle], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        writer.writerows(
            (
                candle.open_time_ms,
                format_open_time_iso(candle.open_time_ms),
                f"{candle.open:.8f}",
                f"{candle.high:.8f}",
                f"{candle.low:.8f}",
                f"{candle.close:.8f}",
                f"{candle.volume:.8f}",
            )
            for candle in candles
        )


def read_csv_text(text: str) -> list[Candle]:
    candles: list[Candle] = []
    append = candles.append
    _int = int
    _float = float
    _candle_cls = Candle
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if not header:
        return candles
    idx_ts = header.index("open_time_ms")
    idx_o = header.index("open")
    idx_h = header.index("high")
    idx_l = header.index("low")
    idx_c = header.index("close")
    idx_v = header.index("volume")
    for row in reader:
        if not row:
            continue
        append(
            _candle_cls(
                _int(row[idx_ts]),
                _float(row[idx_o]),
                _float(row[idx_h]),
                _float(row[idx_l]),
                _float(row[idx_c]),
                _float(row[idx_v]),
            )
        )
    return candles


def read_csv(path: Path) -> list[Candle]:
    return read_csv_text(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "BASE_URL",
    "CSV_FIELDS",
    "OKX_BASE_URL",
    "dedupe_candles",
    "fetch_historical",
    "fetch_klines",
    "fetch_okx_candles",
    "fetch_okx_historical",
    "fetch_okx_incremental",
    "interval_to_ms",
    "okx_interval",
    "okx_row_to_candle",
    "prune_candles_to_window",
    "read_csv",
    "read_csv_text",
    "write_csv",
]
