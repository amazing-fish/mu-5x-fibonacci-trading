"""Columnar BarSeries, SHA-256 keyed packed binary cache, and strictly causal MarketView."""

from __future__ import annotations

from array import array
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
import hashlib
import math
import os
from pathlib import Path
import struct
import sys
from types import MappingProxyType
from typing import Any, Literal, overload
import uuid
import zlib

from mu_strategy.fs_durability import fsync_directory
from mu_strategy.market_data.funding import FundingRateEvent, FundingSeries
from mu_strategy.market_data.utils import interval_to_ms
from mu_strategy.models import Candle


BAR_BIN_MAGIC = b"MUBARS01"
BAR_BIN_VERSION = 1
# Canonical 48-byte bar schema (<qddddd: open_time_ms, open, high, low, close, volume).
# On disk, rows are stored as 6 contiguous little-endian column blocks (exact 48 bytes/bar)
# so pure-Python C-backed ``array.frombytes`` loads 13,000+ bars in < 1 ms.
BAR_RECORD_STRUCT = struct.Struct("<qddddd")
_FLAG_EXPLICIT_CLOSE_TIMES = 1 << 0
_HEADER_STRUCT = struct.Struct("<8sHHqqIIHH32s")
_IS_BIG_ENDIAN = sys.byteorder != "little"
_EMPTY_FUNDING: Mapping[str, FundingSeries] = MappingProxyType({})
_OBJ_NEW = object.__new__
_OBJ_SETATTR = object.__setattr__


class BarSeriesCacheError(ValueError):
    """Raised when a packed BarSeries binary cache fails magic/version/size/hash/CRC checks."""


def _validate_non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty string without surrounding whitespace")
    return value


def _validate_interval_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("interval_ms must be a positive integer")
    return value


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _validate_sha256_hex(value: str) -> bytes:
    if not isinstance(value, str) or len(value) != 64 or not all(ch in _HEX_DIGITS for ch in value):
        raise ValueError("source_sha256 must be a 64-character hex SHA-256 digest")
    return bytes.fromhex(value)


def _to_le_bytes(buf: array | memoryview) -> bytes:
    if isinstance(buf, memoryview):
        return buf.tobytes()
    if not _IS_BIG_ENDIAN:
        return buf.tobytes()
    swapped = array(buf.typecode, buf)
    swapped.byteswap()
    return swapped.tobytes()


def _from_le_bytes(typecode: str, buf: memoryview) -> array:
    arr = array(typecode)
    arr.frombytes(buf)
    if _IS_BIG_ENDIAN:
        arr.byteswap()
    return arr


def _read_all_bytes_fast(path: Path) -> bytes:
    """Read an entire file with minimal Python overhead and concurrent-growth safety."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        size = os.fstat(fd).st_size
        if size <= 0:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return chunks[0] if len(chunks) == 1 else b"".join(chunks)
        first = os.read(fd, size + 1)
        if len(first) == size:
            tail = os.read(fd, 1)
            if not tail:
                return first
            chunks = [first, tail]
        elif not first:
            return b""
        else:
            chunks = [first]
        while True:
            more = os.read(fd, 65536)
            if not more:
                break
            chunks.append(more)
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)
    finally:
        os.close(fd)


class _ColumnSlice(Sequence[Any]):
    """Zero-copy O(1) bounded read-only slice over a typed ``array`` or ``memoryview``."""

    __slots__ = ("_data", "_start", "_end")

    def __init__(self, data: array | memoryview, start: int, end: int) -> None:
        self._data = data
        self._start = start
        self._end = end

    def __len__(self) -> int:
        return self._end - self._start

    @overload
    def __getitem__(self, index: int) -> Any: ...

    @overload
    def __getitem__(self, index: slice) -> "_ColumnSlice": ...

    def __getitem__(self, index: int | slice) -> Any:
        length = self._end - self._start
        if isinstance(index, slice):
            start, stop, step = index.indices(length)
            if step == 1:
                return _ColumnSlice(self._data, self._start + start, self._start + stop)
            return tuple(self._data[self._start + i] for i in range(start, stop, step))
        if index < 0:
            index += length
        if index < 0 or index >= length:
            raise IndexError("column slice index out of range")
        return self._data[self._start + index]

    def __iter__(self) -> Iterator[Any]:
        data = self._data
        for idx in range(self._start, self._end):
            yield data[idx]

    def __reversed__(self) -> Iterator[Any]:
        data = self._data
        for idx in range(self._end - 1, self._start - 1, -1):
            yield data[idx]

    def __repr__(self) -> str:
        return f"_ColumnSlice(len={len(self)}, start={self._start}, end={self._end})"


class _UniformCloseTimeSlice(Sequence[int]):
    """Zero-copy O(1) bounded view of ``open_times[i] + interval_ms`` without materializing an array."""

    __slots__ = ("_open_times", "_interval_ms", "_start", "_end")

    def __init__(self, open_times: array | memoryview, interval_ms: int, start: int, end: int) -> None:
        self._open_times = open_times
        self._interval_ms = interval_ms
        self._start = start
        self._end = end

    def __len__(self) -> int:
        return self._end - self._start

    @overload
    def __getitem__(self, index: int) -> int: ...

    @overload
    def __getitem__(self, index: slice) -> "_UniformCloseTimeSlice": ...

    def __getitem__(self, index: int | slice) -> Any:
        length = self._end - self._start
        if isinstance(index, slice):
            start, stop, step = index.indices(length)
            if step == 1:
                return _UniformCloseTimeSlice(
                    self._open_times,
                    self._interval_ms,
                    self._start + start,
                    self._start + stop,
                )
            step_ms = self._interval_ms
            return tuple(int(self._open_times[self._start + i]) + step_ms for i in range(start, stop, step))
        if index < 0:
            index += length
        if index < 0 or index >= length:
            raise IndexError("close_times slice index out of range")
        return int(self._open_times[self._start + index]) + self._interval_ms

    def __iter__(self) -> Iterator[int]:
        open_times = self._open_times
        step_ms = self._interval_ms
        for idx in range(self._start, self._end):
            yield int(open_times[idx]) + step_ms

    def __reversed__(self) -> Iterator[int]:
        open_times = self._open_times
        step_ms = self._interval_ms
        for idx in range(self._end - 1, self._start - 1, -1):
            yield int(open_times[idx]) + step_ms


class BarSeries:
    """Immutable columnar OHLCV bar series backed by typed stdlib ``array`` / zero-copy buffers."""

    __slots__ = (
        "_symbol",
        "_interval",
        "_interval_ms",
        "_open_times",
        "_close_times",
        "_uniform_close_times",
        "_opens",
        "_highs",
        "_lows",
        "_closes",
        "_volumes",
        "_open_times_buf",
        "_close_times_buf",
        "_opens_buf",
        "_highs_buf",
        "_lows_buf",
        "_closes_buf",
        "_volumes_buf",
    )

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        interval_ms: int,
        open_times: Sequence[int] | array,
        close_times: Sequence[int] | array | None,
        opens: Sequence[float] | array,
        highs: Sequence[float] | array,
        lows: Sequence[float] | array,
        closes: Sequence[float] | array,
        volumes: Sequence[float] | array,
    ) -> None:
        clean_symbol = _validate_non_empty_str(symbol, "symbol")
        clean_interval = _validate_non_empty_str(interval, "interval")
        step_ms = _validate_interval_ms(interval_ms)

        ot = array("q", open_times)
        op = array("d", opens)
        hi = array("d", highs)
        lo = array("d", lows)
        cl = array("d", closes)
        vo = array("d", volumes)

        n = len(ot)
        if not (len(op) == len(hi) == len(lo) == len(cl) == len(vo) == n):
            raise ValueError("all BarSeries columns must have identical lengths")

        ct: array | None
        uniform = True
        if close_times is None:
            ct = None
        else:
            ct = array("q", close_times)
            if len(ct) != n:
                raise ValueError("close_times length must match open_times length")

        prev_open = -1
        prev_close = -1
        for i in range(n):
            t_open = ot[i]
            if t_open < 0:
                raise ValueError(f"open_times[{i}] must be non-negative")
            if prev_open >= 0 and t_open <= prev_open:
                raise ValueError("open_times must be strictly increasing")
            prev_open = t_open

            if ct is not None:
                t_close = ct[i]
                if t_close <= t_open:
                    raise ValueError(f"close_times[{i}] must be strictly greater than open_times[{i}]")
                if prev_close >= 0 and t_close <= prev_close:
                    raise ValueError("close_times must be strictly increasing")
                if t_close != t_open + step_ms:
                    uniform = False
                prev_close = t_close

            o_v, h_v, l_v, c_v, v_v = op[i], hi[i], lo[i], cl[i], vo[i]
            if not (
                math.isfinite(o_v)
                and math.isfinite(h_v)
                and math.isfinite(l_v)
                and math.isfinite(c_v)
                and math.isfinite(v_v)
            ):
                raise ValueError(f"non-finite OHLCV value at index {i}")
            if o_v <= 0.0 or h_v <= 0.0 or l_v <= 0.0 or c_v <= 0.0:
                raise ValueError(f"OHLC prices must be strictly positive at index {i}")
            if h_v < o_v or h_v < c_v or h_v < l_v:
                raise ValueError(f"high must be >= open, close, and low at index {i}")
            if l_v > o_v or l_v > c_v:
                raise ValueError(f"low must be <= open and close at index {i}")
            if v_v < 0.0:
                raise ValueError(f"volume must be non-negative at index {i}")
            if v_v == 0.0:
                vo[i] = 0.0

        object.__setattr__(self, "_symbol", clean_symbol)
        object.__setattr__(self, "_interval", clean_interval)
        object.__setattr__(self, "_interval_ms", step_ms)
        object.__setattr__(self, "_open_times", ot)
        object.__setattr__(self, "_close_times", ct)
        object.__setattr__(self, "_uniform_close_times", uniform)
        object.__setattr__(self, "_opens", op)
        object.__setattr__(self, "_highs", hi)
        object.__setattr__(self, "_lows", lo)
        object.__setattr__(self, "_closes", cl)
        object.__setattr__(self, "_volumes", vo)
        object.__setattr__(self, "_open_times_buf", ot)
        object.__setattr__(self, "_close_times_buf", ct)
        object.__setattr__(self, "_opens_buf", op)
        object.__setattr__(self, "_highs_buf", hi)
        object.__setattr__(self, "_lows_buf", lo)
        object.__setattr__(self, "_closes_buf", cl)
        object.__setattr__(self, "_volumes_buf", vo)

    @classmethod
    def _from_trusted_buffers(
        cls,
        *,
        symbol: str,
        interval: str,
        interval_ms: int,
        open_times_buf: array | memoryview,
        close_times_buf: array | memoryview | None,
        uniform_close_times: bool,
        opens_buf: array | memoryview,
        highs_buf: array | memoryview,
        lows_buf: array | memoryview,
        closes_buf: array | memoryview,
        volumes_buf: array | memoryview,
    ) -> "BarSeries":
        inst = object.__new__(cls)
        object.__setattr__(inst, "_symbol", symbol)
        object.__setattr__(inst, "_interval", interval)
        object.__setattr__(inst, "_interval_ms", interval_ms)
        object.__setattr__(inst, "_open_times", open_times_buf if isinstance(open_times_buf, array) else None)
        object.__setattr__(inst, "_close_times", close_times_buf if isinstance(close_times_buf, array) else None)
        object.__setattr__(inst, "_uniform_close_times", uniform_close_times)
        object.__setattr__(inst, "_opens", opens_buf if isinstance(opens_buf, array) else None)
        object.__setattr__(inst, "_highs", highs_buf if isinstance(highs_buf, array) else None)
        object.__setattr__(inst, "_lows", lows_buf if isinstance(lows_buf, array) else None)
        object.__setattr__(inst, "_closes", closes_buf if isinstance(closes_buf, array) else None)
        object.__setattr__(inst, "_volumes", volumes_buf if isinstance(volumes_buf, array) else None)
        object.__setattr__(inst, "_open_times_buf", open_times_buf)
        object.__setattr__(inst, "_close_times_buf", close_times_buf)
        object.__setattr__(inst, "_opens_buf", opens_buf)
        object.__setattr__(inst, "_highs_buf", highs_buf)
        object.__setattr__(inst, "_lows_buf", lows_buf)
        object.__setattr__(inst, "_closes_buf", closes_buf)
        object.__setattr__(inst, "_volumes_buf", volumes_buf)
        return inst

    def _materialize_array(self, attr: str, buf_attr: str, typecode: str) -> array:
        arr = getattr(self, attr)
        if arr is None:
            buf = getattr(self, buf_attr)
            arr = array(typecode)
            arr.frombytes(buf.cast("B") if isinstance(buf, memoryview) else buf)
            object.__setattr__(self, attr, arr)
            object.__setattr__(self, buf_attr, arr)
        return arr

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("BarSeries is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("BarSeries is immutable")

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def interval(self) -> str:
        return self._interval

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    @property
    def open_times(self) -> array:
        arr = self._open_times
        return arr if arr is not None else self._materialize_array("_open_times", "_open_times_buf", "q")

    @property
    def close_times(self) -> array:
        ct = self._close_times
        if ct is None:
            ct_buf = self._close_times_buf
            if ct_buf is not None:
                ct = array("q")
                ct.frombytes(ct_buf.cast("B") if isinstance(ct_buf, memoryview) else ct_buf)
            else:
                ct = array("q", map(self._interval_ms.__add__, self._open_times_buf))
            _OBJ_SETATTR(self, "_close_times", ct)
            _OBJ_SETATTR(self, "_close_times_buf", ct)
        return ct

    @property
    def is_uniform_close_times(self) -> bool:
        return self._uniform_close_times

    @property
    def opens(self) -> array:
        arr = self._opens
        return arr if arr is not None else self._materialize_array("_opens", "_opens_buf", "d")

    @property
    def highs(self) -> array:
        arr = self._highs
        return arr if arr is not None else self._materialize_array("_highs", "_highs_buf", "d")

    @property
    def lows(self) -> array:
        arr = self._lows
        return arr if arr is not None else self._materialize_array("_lows", "_lows_buf", "d")

    @property
    def closes(self) -> array:
        arr = self._closes
        return arr if arr is not None else self._materialize_array("_closes", "_closes_buf", "d")

    @property
    def volumes(self) -> array:
        arr = self._volumes
        return arr if arr is not None else self._materialize_array("_volumes", "_volumes_buf", "d")

    def __len__(self) -> int:
        return len(self._open_times_buf)

    def __bool__(self) -> bool:
        return len(self._open_times_buf) > 0

    @overload
    def __getitem__(self, index: int) -> Candle: ...

    @overload
    def __getitem__(self, index: slice) -> "BarSeriesSlice | tuple[Candle, ...]": ...

    def __getitem__(self, index: int | slice) -> "Candle | BarSeriesSlice | tuple[Candle, ...]":
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self._open_times_buf))
            if step == 1:
                return BarSeriesSlice(self, start, stop)
            return tuple(self.bar_at(i) for i in range(start, stop, step))
        return self.bar_at(index)

    def bar_at(self, index: int) -> Candle:
        ot = self._open_times_buf
        n = len(ot)
        if index < 0:
            index += n
        if index < 0 or index >= n:
            raise IndexError("BarSeries index out of range")
        return Candle(
            open_time_ms=int(ot[index]),
            open=float(self._opens_buf[index]),
            high=float(self._highs_buf[index]),
            low=float(self._lows_buf[index]),
            close=float(self._closes_buf[index]),
            volume=float(self._volumes_buf[index]),
        )

    def latest(self) -> Candle | None:
        if not self._open_times_buf:
            return None
        return self.bar_at(-1)

    def index_at_or_before(self, ts_ms: int, *, by: Literal["open", "close"] = "open") -> int:
        """Return the greatest index whose open or close timestamp is ``<= ts_ms``, or ``-1``."""
        if by == "open":
            return bisect_right(self._open_times_buf, ts_ms) - 1
        if by == "close":
            ct_buf = self._close_times_buf
            if self._uniform_close_times and ct_buf is None:
                return bisect_right(self._open_times_buf, ts_ms - self._interval_ms) - 1
            return bisect_right(ct_buf if ct_buf is not None else self.close_times, ts_ms) - 1
        raise ValueError(f"by must be 'open' or 'close', got {by!r}")

    def closed_count_at(self, now_ms: int) -> int:
        """Return count of bars with ``close_time_ms <= now_ms`` in O(log N)."""
        ct_buf = self._close_times_buf
        if self._uniform_close_times and ct_buf is None:
            return bisect_right(self._open_times_buf, now_ms - self._interval_ms)
        return bisect_right(ct_buf if ct_buf is not None else self.close_times, now_ms)

    def slice_view(self, start_idx: int, end_idx: int) -> "BarSeriesSlice":
        """Return a zero-copy O(1) bounded ``BarSeriesSlice`` over ``[start_idx, end_idx)``."""
        n = len(self._open_times_buf)
        if start_idx < 0 or end_idx < start_idx or end_idx > n:
            raise IndexError(
                f"invalid slice bounds [{start_idx}, {end_idx}) for BarSeries of length {n}"
            )
        return BarSeriesSlice(self, start_idx, end_idx)

    def to_candles(self) -> list[Candle]:
        ot = self._open_times_buf
        op = self._opens_buf
        hi = self._highs_buf
        lo = self._lows_buf
        cl = self._closes_buf
        vo = self._volumes_buf
        return [
            Candle(
                open_time_ms=int(ot[i]),
                open=float(op[i]),
                high=float(hi[i]),
                low=float(lo[i]),
                close=float(cl[i]),
                volume=float(vo[i]),
            )
            for i in range(len(ot))
        ]

    @classmethod
    def from_candles(
        cls,
        symbol: str,
        interval: str,
        candles: Sequence[Candle],
        *,
        interval_ms: int | None = None,
    ) -> "BarSeries":
        resolved_ms = interval_to_ms(interval) if interval_ms is None else _validate_interval_ms(interval_ms)
        ot = array("q")
        op = array("d")
        hi = array("d")
        lo = array("d")
        cl = array("d")
        vo = array("d")
        for bar in candles:
            ot.append(bar.open_time_ms)
            op.append(bar.open)
            hi.append(bar.high)
            lo.append(bar.low)
            cl.append(bar.close)
            vo.append(bar.volume)
        return cls(
            symbol=symbol,
            interval=interval,
            interval_ms=resolved_ms,
            open_times=ot,
            close_times=None,
            opens=op,
            highs=hi,
            lows=lo,
            closes=cl,
            volumes=vo,
        )

    def __repr__(self) -> str:
        return f"BarSeries(symbol={self._symbol!r}, interval={self._interval!r}, len={len(self)})"


class BarSeriesSlice:
    """Zero-copy O(1) causally bounded view into an immutable ``BarSeries``."""

    __slots__ = ("_series", "_start", "_end")

    def __init__(self, series: BarSeries, start: int, end: int) -> None:
        n = len(series)
        if start < 0 or end < start or end > n:
            raise IndexError(f"invalid BarSeriesSlice bounds [{start}, {end}) for length {n}")
        object.__setattr__(self, "_series", series)
        object.__setattr__(self, "_start", start)
        object.__setattr__(self, "_end", end)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("BarSeriesSlice is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("BarSeriesSlice is immutable")

    @property
    def start(self) -> int:
        return self._start

    @property
    def end(self) -> int:
        return self._end

    @property
    def symbol(self) -> str:
        return self._series.symbol

    @property
    def interval(self) -> str:
        return self._series.interval

    @property
    def interval_ms(self) -> int:
        return self._series.interval_ms

    @property
    def open_times(self) -> _ColumnSlice:
        return _ColumnSlice(self._series._open_times_buf, self._start, self._end)

    @property
    def close_times(self) -> _ColumnSlice | _UniformCloseTimeSlice:
        ct = self._series._close_times_buf
        if ct is not None:
            return _ColumnSlice(ct, self._start, self._end)
        return _UniformCloseTimeSlice(
            self._series._open_times_buf,
            self._series._interval_ms,
            self._start,
            self._end,
        )

    @property
    def opens(self) -> _ColumnSlice:
        return _ColumnSlice(self._series._opens_buf, self._start, self._end)

    @property
    def highs(self) -> _ColumnSlice:
        return _ColumnSlice(self._series._highs_buf, self._start, self._end)

    @property
    def lows(self) -> _ColumnSlice:
        return _ColumnSlice(self._series._lows_buf, self._start, self._end)

    @property
    def closes(self) -> _ColumnSlice:
        return _ColumnSlice(self._series._closes_buf, self._start, self._end)

    @property
    def volumes(self) -> _ColumnSlice:
        return _ColumnSlice(self._series._volumes_buf, self._start, self._end)

    def __len__(self) -> int:
        return self._end - self._start

    def __bool__(self) -> bool:
        return self._end > self._start

    @overload
    def __getitem__(self, index: int) -> Candle: ...

    @overload
    def __getitem__(self, index: slice) -> "BarSeriesSlice | tuple[Candle, ...]": ...

    def __getitem__(self, index: int | slice) -> "Candle | BarSeriesSlice | tuple[Candle, ...]":
        if isinstance(index, slice):
            length = self._end - self._start
            start, stop, step = index.indices(length)
            if step == 1:
                return BarSeriesSlice(self._series, self._start + start, self._start + stop)
            return tuple(self.bar_at(i) for i in range(start, stop, step))
        return self.bar_at(index)

    def bar_at(self, index: int) -> Candle:
        length = self._end - self._start
        if index < 0:
            index += length
        if index < 0 or index >= length:
            raise IndexError("BarSeriesSlice index out of range")
        return self._series.bar_at(self._start + index)

    def latest(self) -> Candle | None:
        if self._end <= self._start:
            return None
        return self._series.bar_at(self._end - 1)

    def tail(self, count: int) -> "BarSeriesSlice":
        if count < 0:
            raise ValueError("count must be non-negative")
        length = self._end - self._start
        take = min(count, length)
        return BarSeriesSlice(self._series, self._end - take, self._end)

    def subslice(self, start: int, end: int) -> "BarSeriesSlice":
        length = self._end - self._start
        if start < 0 or end < start or end > length:
            raise IndexError(f"invalid subslice [{start}, {end}) for slice length {length}")
        return BarSeriesSlice(self._series, self._start + start, self._start + end)

    def to_candles(self) -> list[Candle]:
        s = self._series
        ot = s._open_times_buf
        op = s._opens_buf
        hi = s._highs_buf
        lo = s._lows_buf
        cl = s._closes_buf
        vo = s._volumes_buf
        return [
            Candle(
                open_time_ms=int(ot[i]),
                open=float(op[i]),
                high=float(hi[i]),
                low=float(lo[i]),
                close=float(cl[i]),
                volume=float(vo[i]),
            )
            for i in range(self._start, self._end)
        ]

    def __repr__(self) -> str:
        return (
            f"BarSeriesSlice(symbol={self.symbol!r}, interval={self.interval!r}, "
            f"start={self._start}, end={self._end})"
        )


def write_bar_series_bin(
    path: Path | str,
    series: BarSeries,
    source_sha256: str,
) -> Path:
    """Atomically serialize ``series`` into a SHA-256 keyed packed binary cache file.

    Format:
      - 72-byte little-endian header: magic, version, flags, interval_ms, row_count,
        CRC32, reserved=0, symbol_len, interval_len, 32-byte source_sha256 digest
      - UTF-8 symbol + interval metadata bytes
      - Columnar little-endian blocks (``row_count * 48`` bytes for uniform intervals,
        matching ``BAR_RECORD_STRUCT = struct.Struct("<qddddd")``)
    """
    digest = _validate_sha256_hex(source_sha256)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    sym_bytes = series.symbol.encode("utf-8")
    iv_bytes = series.interval.encode("utf-8")
    if len(sym_bytes) > 65535 or len(iv_bytes) > 65535:
        raise ValueError("symbol and interval UTF-8 lengths must fit in uint16")

    flags = 0 if series.is_uniform_close_times else _FLAG_EXPLICIT_CLOSE_TIMES
    parts = [
        sym_bytes,
        iv_bytes,
        _to_le_bytes(series._open_times_buf),
        _to_le_bytes(series._opens_buf),
        _to_le_bytes(series._highs_buf),
        _to_le_bytes(series._lows_buf),
        _to_le_bytes(series._closes_buf),
        _to_le_bytes(series._volumes_buf),
    ]
    if flags & _FLAG_EXPLICIT_CLOSE_TIMES:
        ct_buf = series._close_times_buf
        parts.append(_to_le_bytes(ct_buf if ct_buf is not None else series.close_times))

    header_for_crc = _HEADER_STRUCT.pack(
        BAR_BIN_MAGIC,
        BAR_BIN_VERSION,
        flags,
        series.interval_ms,
        len(series),
        0,
        0,
        len(sym_bytes),
        len(iv_bytes),
        digest,
    )

    crc = zlib.crc32(header_for_crc)
    for chunk in parts:
        crc = zlib.crc32(chunk, crc)
    crc &= 0xFFFFFFFF

    header = _HEADER_STRUCT.pack(
        BAR_BIN_MAGIC,
        BAR_BIN_VERSION,
        flags,
        series.interval_ms,
        len(series),
        crc,
        0,
        len(sym_bytes),
        len(iv_bytes),
        digest,
    )

    tmp_path = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp_path.open("wb") as handle:
            handle.write(header)
            for chunk in parts:
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        fsync_directory(target.parent)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return target


def read_bar_series_bin(
    path: Path | str,
    expected_sha256: str | None = None,
) -> BarSeries:
    """Read and strictly validate a packed binary ``BarSeries`` cache file.

    Fails closed (raising ``BarSeriesCacheError``) on missing file, bad magic,
    unsupported version, unknown flags, truncated/oversized payload, SHA-256
    mismatch, or payload CRC32 corruption.
    """
    expected_digest = _validate_sha256_hex(expected_sha256) if expected_sha256 is not None else None
    target = Path(path)
    try:
        raw = _read_all_bytes_fast(target)
    except OSError as exc:
        raise BarSeriesCacheError(f"unable to read bar cache {target}: {exc}") from exc

    header_size = _HEADER_STRUCT.size
    if len(raw) < header_size:
        raise BarSeriesCacheError(f"truncated bar cache header ({len(raw)} < {header_size} bytes)")

    (
        magic,
        version,
        flags,
        interval_ms,
        row_count,
        stored_crc,
        reserved,
        sym_len,
        iv_len,
        stored_digest,
    ) = _HEADER_STRUCT.unpack_from(raw, 0)

    if magic != BAR_BIN_MAGIC:
        raise BarSeriesCacheError(f"invalid bar cache magic: {magic!r}")
    if version != BAR_BIN_VERSION:
        raise BarSeriesCacheError(f"unsupported bar cache version: {version}")
    if flags & ~_FLAG_EXPLICIT_CLOSE_TIMES:
        raise BarSeriesCacheError(f"unsupported bar cache flags: {flags:#x}")
    if reserved != 0:
        raise BarSeriesCacheError(f"corrupt bar cache reserved header field: {reserved}")
    if interval_ms <= 0:
        raise BarSeriesCacheError(f"invalid bar cache interval_ms: {interval_ms}")
    if row_count < 0:
        raise BarSeriesCacheError(f"invalid bar cache row_count: {row_count}")
    if sym_len == 0 or iv_len == 0:
        raise BarSeriesCacheError("bar cache header missing symbol or interval metadata")
    if expected_digest is not None and stored_digest != expected_digest:
        raise BarSeriesCacheError(
            f"bar cache source_sha256 mismatch: expected {expected_sha256}, got {stored_digest.hex()}"
        )

    has_explicit_close = bool(flags & _FLAG_EXPLICIT_CLOSE_TIMES)
    bytes_per_row = BAR_RECORD_STRUCT.size + (8 if has_explicit_close else 0)
    meta_end = header_size + sym_len + iv_len
    expected_size = meta_end + (row_count * bytes_per_row)
    if len(raw) != expected_size:
        raise BarSeriesCacheError(
            f"bar cache size mismatch: expected {expected_size} bytes, got {len(raw)}"
        )

    mv = memoryview(raw)
    header_zero_crc = _HEADER_STRUCT.pack(
        magic,
        version,
        flags,
        interval_ms,
        row_count,
        0,
        reserved,
        sym_len,
        iv_len,
        stored_digest,
    )
    computed_crc = zlib.crc32(mv[header_size:], zlib.crc32(header_zero_crc)) & 0xFFFFFFFF
    if computed_crc != stored_crc:
        raise BarSeriesCacheError(
            f"bar cache CRC32 mismatch: expected {stored_crc:#010x}, got {computed_crc:#010x}"
        )

    try:
        symbol = raw[header_size : header_size + sym_len].decode("utf-8")
        interval = raw[header_size + sym_len : meta_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BarSeriesCacheError(f"corrupt UTF-8 metadata in bar cache: {exc}") from exc

    col_bytes = row_count * 8
    offset = meta_end
    if not _IS_BIG_ENDIAN:
        open_times_buf: array | memoryview = mv[offset : offset + col_bytes].cast("q")
        offset += col_bytes
        opens_buf: array | memoryview = mv[offset : offset + col_bytes].cast("d")
        offset += col_bytes
        highs_buf: array | memoryview = mv[offset : offset + col_bytes].cast("d")
        offset += col_bytes
        lows_buf: array | memoryview = mv[offset : offset + col_bytes].cast("d")
        offset += col_bytes
        closes_buf: array | memoryview = mv[offset : offset + col_bytes].cast("d")
        offset += col_bytes
        volumes_buf: array | memoryview = mv[offset : offset + col_bytes].cast("d")
        offset += col_bytes
        close_times_buf: array | memoryview | None = (
            mv[offset : offset + col_bytes].cast("q") if has_explicit_close else None
        )
    else:
        open_times_buf = _from_le_bytes("q", mv[offset : offset + col_bytes])
        offset += col_bytes
        opens_buf = _from_le_bytes("d", mv[offset : offset + col_bytes])
        offset += col_bytes
        highs_buf = _from_le_bytes("d", mv[offset : offset + col_bytes])
        offset += col_bytes
        lows_buf = _from_le_bytes("d", mv[offset : offset + col_bytes])
        offset += col_bytes
        closes_buf = _from_le_bytes("d", mv[offset : offset + col_bytes])
        offset += col_bytes
        volumes_buf = _from_le_bytes("d", mv[offset : offset + col_bytes])
        offset += col_bytes
        close_times_buf = _from_le_bytes("q", mv[offset : offset + col_bytes]) if has_explicit_close else None

    return BarSeries._from_trusted_buffers(
        symbol=symbol,
        interval=interval,
        interval_ms=interval_ms,
        open_times_buf=open_times_buf,
        close_times_buf=close_times_buf,
        uniform_close_times=not has_explicit_close,
        opens_buf=opens_buf,
        highs_buf=highs_buf,
        lows_buf=lows_buf,
        closes_buf=closes_buf,
        volumes_buf=volumes_buf,
    )


def _compute_candles_sha256(candles: Sequence[Candle]) -> str:
    from mu_strategy.market_data.trusted_data.store import candles_content_sha256

    return candles_content_sha256(candles)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def load_or_build_bar_series(
    store_or_candles: Any,
    *,
    symbol: str,
    interval: str,
    cache_path: Path | str | None = None,
    cache_dir: Path | str | None = None,
    source_sha256: str | None = None,
    interval_ms: int | None = None,
    strict_cache: bool = False,
) -> BarSeries:
    """Read-through packed binary cache loader for ``TrustedDataStore``, CSV files, or candles.

    - When ``store_or_candles`` is a ``TrustedDataStore`` (or directory ``Path``), resolves the
      current generation manifest SHA-256 and reads/writes ``<cache_dir>/<gen>/<symbol>_<interval>.bin``
      (defaulting to ``store.data_dir / ".bar_cache"`` unless ``cache_path``/``cache_dir`` is given).
    - When ``store_or_candles`` is a CSV file ``Path``, keys the binary cache by file SHA-256.
    - When ``store_or_candles`` is a ``Sequence[Candle]``, keys by ``source_sha256`` (or canonical
      content SHA-256).
    - If ``strict_cache=True``, any corrupt or mismatched existing ``.bin`` file raises
      ``BarSeriesCacheError`` instead of rebuilding.
    """
    from mu_strategy.market_data.trusted_data.contracts import AvailabilityState, IntegrityState
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore, validate_storage_segment

    clean_symbol = _validate_non_empty_str(symbol, "symbol")
    clean_interval = _validate_non_empty_str(interval, "interval")
    resolved_interval_ms = (
        interval_to_ms(clean_interval) if interval_ms is None else _validate_interval_ms(interval_ms)
    )

    if isinstance(store_or_candles, (str, Path)):
        src_path = Path(store_or_candles)
        if src_path.is_dir():
            store_or_candles = TrustedDataStore(data_dir=src_path)
        else:
            expected_sha = source_sha256 or _file_sha256(src_path)
            resolved_cache = (
                Path(cache_path)
                if cache_path is not None
                else (
                    Path(cache_dir) / f"{validate_storage_segment(clean_symbol, field='symbol')}_{validate_storage_segment(clean_interval, field='interval')}.bin"
                    if cache_dir is not None
                    else src_path.with_suffix(".bin")
                )
            )
            cached = _try_read_cache(
                resolved_cache,
                expected_sha,
                expected_symbol=clean_symbol,
                expected_interval=clean_interval,
                expected_interval_ms=resolved_interval_ms,
                strict_cache=strict_cache,
            )
            if cached is not None:
                return cached
            candles = TrustedDataStore(data_dir=src_path.parent).read_csv(src_path)
            series = BarSeries.from_candles(
                clean_symbol,
                clean_interval,
                candles,
                interval_ms=resolved_interval_ms,
            )
            write_bar_series_bin(resolved_cache, series, expected_sha)
            return series

    if isinstance(store_or_candles, TrustedDataStore):
        store = store_or_candles
        manifest_result = store.read_manifest()
        if not manifest_result.ok or manifest_result.snapshot is None or manifest_result.generation_id is None:
            raise ValueError(
                f"cannot load BarSeries from TrustedDataStore: {manifest_result.message or manifest_result.reason}"
            )
        manifest = manifest_result.snapshot
        health = manifest.datasets.get((clean_symbol, clean_interval))
        if health is None:
            raise KeyError(f"dataset ({clean_symbol!r}, {clean_interval!r}) not found in trusted manifest")
        if health.availability is not AvailabilityState.AVAILABLE or health.integrity is not IntegrityState.VALID:
            raise ValueError(
                f"trusted dataset ({clean_symbol}, {clean_interval}) is not available/valid "
                f"(availability={health.availability.value}, integrity={health.integrity.value})"
            )
        expected_sha = source_sha256 or health.content_sha256
        if not expected_sha:
            raise ValueError(f"trusted dataset ({clean_symbol}, {clean_interval}) is missing content_sha256")

        safe_sym = validate_storage_segment(clean_symbol, field="symbol")
        safe_iv = validate_storage_segment(clean_interval, field="interval")
        safe_gen = validate_storage_segment(manifest_result.generation_id, field="generation_id")
        if cache_path is not None:
            resolved_cache = Path(cache_path)
        else:
            root = Path(cache_dir) if cache_dir is not None else (store.data_dir / ".bar_cache")
            resolved_cache = root / safe_gen / f"{safe_sym}_{safe_iv}.bin"

        cached = _try_read_cache(
            resolved_cache,
            expected_sha,
            expected_symbol=clean_symbol,
            expected_interval=clean_interval,
            expected_interval_ms=resolved_interval_ms,
            strict_cache=strict_cache,
        )
        if cached is not None:
            return cached

        candles, actual_sha = store.read_generation_dataset_with_hash(
            manifest,
            symbol=clean_symbol,
            interval=clean_interval,
            generation_root=manifest_result.generation_root or store.data_dir,
            generation_id=manifest_result.generation_id,
            verify_logical=True,
            compute_hash=True,
        )
        if actual_sha != expected_sha:
            raise BarSeriesCacheError(
                f"trusted CSV content_sha256 mismatch for ({clean_symbol}, {clean_interval}): "
                f"expected {expected_sha}, got {actual_sha}"
            )
        series = BarSeries.from_candles(
            clean_symbol,
            clean_interval,
            candles,
            interval_ms=resolved_interval_ms,
        )
        write_bar_series_bin(resolved_cache, series, expected_sha)
        return series

    if isinstance(store_or_candles, Sequence):
        candles = store_or_candles
        if cache_path is None and cache_dir is None:
            return BarSeries.from_candles(
                clean_symbol,
                clean_interval,
                candles,
                interval_ms=resolved_interval_ms,
            )
        expected_sha = source_sha256 or _compute_candles_sha256(candles)
        resolved_cache = (
            Path(cache_path)
            if cache_path is not None
            else Path(cache_dir)  # type: ignore[arg-type]
            / f"{validate_storage_segment(clean_symbol, field='symbol')}_{validate_storage_segment(clean_interval, field='interval')}.bin"
        )
        cached = _try_read_cache(
            resolved_cache,
            expected_sha,
            expected_symbol=clean_symbol,
            expected_interval=clean_interval,
            expected_interval_ms=resolved_interval_ms,
            strict_cache=strict_cache,
        )
        if cached is not None:
            return cached
        series = BarSeries.from_candles(
            clean_symbol,
            clean_interval,
            candles,
            interval_ms=resolved_interval_ms,
        )
        write_bar_series_bin(resolved_cache, series, expected_sha)
        return series

    raise TypeError(
        "store_or_candles must be a TrustedDataStore, Path, or Sequence[Candle], "
        f"got {type(store_or_candles).__name__}"
    )


def _try_read_cache(
    cache_path: Path,
    expected_sha256: str,
    *,
    expected_symbol: str,
    expected_interval: str,
    expected_interval_ms: int,
    strict_cache: bool,
) -> BarSeries | None:
    if not cache_path.exists():
        return None
    try:
        cached = read_bar_series_bin(cache_path, expected_sha256=expected_sha256)
        if (
            cached.symbol != expected_symbol
            or cached.interval != expected_interval
            or cached.interval_ms != expected_interval_ms
        ):
            raise BarSeriesCacheError(
                f"bar cache metadata mismatch: expected ({expected_symbol!r}, {expected_interval!r}, "
                f"{expected_interval_ms}), got ({cached.symbol!r}, {cached.interval!r}, {cached.interval_ms})"
            )
        return cached
    except BarSeriesCacheError:
        if strict_cache:
            raise
        return None


class MarketView:
    """Strictly causal market snapshot exposing ONLY bars with ``close_time_ms <= now_ms``."""

    __slots__ = ("_bars", "_funding", "_features", "_now_ms")

    def __init__(
        self,
        bars: Mapping[tuple[str, str], BarSeries],
        now_ms: int,
        *,
        funding: Mapping[str, FundingSeries] | None = None,
        features: Any | None = None,
    ) -> None:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int):
            raise ValueError("now_ms must be an integer millisecond timestamp")
        bars_copy: dict[tuple[str, str], BarSeries] = {}
        for key, series in bars.items():
            if not (isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], str) and isinstance(key[1], str)):
                raise ValueError(f"bars keys must be (symbol, interval) tuples, got {key!r}")
            if not isinstance(series, BarSeries):
                raise TypeError(f"bars values must be BarSeries, got {type(series).__name__}")
            bars_copy[key] = series

        funding_map: Mapping[str, FundingSeries]
        if funding is None:
            funding_map = _EMPTY_FUNDING
        else:
            funding_copy: dict[str, FundingSeries] = {}
            for sym, fseries in funding.items():
                if not isinstance(sym, str) or not sym:
                    raise ValueError(f"funding keys must be non-empty symbol strings, got {sym!r}")
                if not isinstance(fseries, FundingSeries):
                    raise TypeError(f"funding values must be FundingSeries, got {type(fseries).__name__}")
                funding_copy[sym] = fseries
            funding_map = MappingProxyType(funding_copy)

        object.__setattr__(self, "_bars", MappingProxyType(bars_copy))
        object.__setattr__(self, "_funding", funding_map)
        object.__setattr__(self, "_features", features)
        object.__setattr__(self, "_now_ms", now_ms)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("MarketView is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("MarketView is immutable")

    @property
    def now_ms(self) -> int:
        return self._now_ms

    @property
    def features(self) -> Any | None:
        return self._features

    def has_series(self, symbol: str, interval: str) -> bool:
        return (symbol, interval) in self._bars

    def symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(sym for sym, _ in self._bars))

    def available_intervals(self, symbol: str) -> tuple[str, ...]:
        return tuple(iv for sym, iv in self._bars if sym == symbol)

    def closed_bars(self, symbol: str, interval: str) -> BarSeriesSlice:
        """Return a zero-copy slice of all bars strictly closed by ``now_ms`` (``close_time_ms <= now_ms``)."""
        series = self._bars[(symbol, interval)]
        end = series.closed_count_at(self._now_ms)
        sl = _OBJ_NEW(BarSeriesSlice)
        _OBJ_SETATTR(sl, "_series", series)
        _OBJ_SETATTR(sl, "_start", 0)
        _OBJ_SETATTR(sl, "_end", end)
        return sl

    def latest_closed_bar(self, symbol: str, interval: str) -> Candle | None:
        """Return the most recent closed bar with ``close_time_ms <= now_ms``, or ``None``."""
        series = self._bars[(symbol, interval)]
        end = series.closed_count_at(self._now_ms)
        if end <= 0:
            return None
        return series.bar_at(end - 1)

    def has_funding(self, symbol: str) -> bool:
        return symbol in self._funding

    def funding_series(self, symbol: str) -> FundingSeries | None:
        """Return the funding series causally bounded to ``ts_ms <= now_ms``, or ``None``."""
        series = self._funding.get(symbol)
        if series is None:
            return None
        hi = bisect_right(series.timestamps_ms, self._now_ms)
        if hi == len(series):
            return series
        return FundingSeries(
            instrument_id=series.instrument_id,
            timestamps_ms=series.timestamps_ms[:hi],
            rates=series.rates[:hi],
            settlement_interval_hours=series.settlement_interval_hours,
        )

    def funding_between(self, symbol: str, start_ms: int, end_ms: int) -> tuple[FundingRateEvent, ...]:
        """Return funding events in ``(start_ms, min(end_ms, now_ms)]`` (strictly causal)."""
        series = self._funding.get(symbol)
        if series is None:
            return ()
        causal_end = end_ms if end_ms <= self._now_ms else self._now_ms
        return series.between(start_ms, causal_end)

    def at(self, new_now_ms: int) -> "MarketView":
        """Fast O(1) causal re-binding to ``new_now_ms``, sharing underlying series and FeatureCache."""
        if type(new_now_ms) is not int and (isinstance(new_now_ms, bool) or not isinstance(new_now_ms, int)):
            raise ValueError("new_now_ms must be an integer millisecond timestamp")
        view = _OBJ_NEW(MarketView)
        _OBJ_SETATTR(view, "_bars", self._bars)
        _OBJ_SETATTR(view, "_funding", self._funding)
        _OBJ_SETATTR(view, "_features", self._features)
        _OBJ_SETATTR(view, "_now_ms", new_now_ms)
        return view

    def __repr__(self) -> str:
        return f"MarketView(now_ms={self._now_ms}, series={len(self._bars)})"


__all__ = [
    "BAR_BIN_MAGIC",
    "BAR_BIN_VERSION",
    "BAR_RECORD_STRUCT",
    "BarSeries",
    "BarSeriesCacheError",
    "BarSeriesSlice",
    "MarketView",
    "load_or_build_bar_series",
    "read_bar_series_bin",
    "write_bar_series_bin",
]
