"""Historical perpetual funding-rate series, sensitivity bands, and public read-only fetchers."""

from __future__ import annotations

from array import array
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import FrozenInstanceError, dataclass
from decimal import Decimal
import json
import math
import os
from pathlib import Path
from typing import Any, Literal
import urllib.parse
import uuid

from mu_strategy.fs_durability import fsync_directory
from mu_strategy.market_data.http_pool import get_json


OKX_FUNDING_HISTORY_URL = "https://www.okx.com/api/v5/public/funding-rate-history"
BINANCE_FUNDING_HISTORY_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
_HOUR_MS = 3_600_000
_YEAR_HOURS = 365.0 * 24.0
_OKX_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}
_BINANCE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}
FUNDING_JSON_SCHEMA_VERSION = 1


class MissingFundingHistoryError(ValueError):
    """Raised when historical funding data is required but absent or incomplete."""


def _validate_int_ts(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer millisecond timestamp")
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    return value


def _validate_rate(value: Any, label: str = "rate") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    rate = float(value)
    if not math.isfinite(rate):
        raise ValueError(f"{label} must be finite")
    return 0.0 if rate == 0.0 else rate


def _validate_interval_hours(value: int | None, label: str = "settlement_interval_hours") -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer or None")
    return value


def _parse_int_field(raw: Any, label: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{label} must be an integer")
    if isinstance(raw, int):
        return _validate_int_ts(raw, label)
    if isinstance(raw, str):
        text = raw.strip()
        if text and text.isascii() and text.isdecimal():
            return _validate_int_ts(int(text), label)
    raise ValueError(f"invalid {label}: {raw!r}")


def _parse_float_field(raw: Any, label: str) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{label} must be a finite float")
    if isinstance(raw, (int, float)):
        return _validate_rate(raw, label)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError(f"empty {label}")
        try:
            return _validate_rate(float(text), label)
        except ValueError as exc:
            raise ValueError(f"invalid {label}: {raw!r}") from exc
    raise ValueError(f"invalid {label}: {raw!r}")


def _infer_interval_hours(timestamps_ms: array) -> int | None:
    if len(timestamps_ms) < 2:
        return None
    min_gap: int | None = None
    prev = timestamps_ms[0]
    for idx in range(1, len(timestamps_ms)):
        gap = timestamps_ms[idx] - prev
        if gap > 0 and (min_gap is None or gap < min_gap):
            min_gap = gap
        prev = timestamps_ms[idx]
    if min_gap is None or min_gap % _HOUR_MS != 0:
        return None
    hours = min_gap // _HOUR_MS
    return hours if hours > 0 else None


def _infer_settlement_phase_ms(
    timestamps_ms: array | memoryview,
    step_ms: int,
    *,
    start_idx: int = 0,
    end_idx: int | None = None,
) -> int | None:
    """Return the unique ``ts % step_ms`` phase shared by ``timestamps_ms[start_idx:end_idx]``, or ``None``."""
    hi = len(timestamps_ms) if end_idx is None else end_idx
    if start_idx >= hi:
        return None
    phase = int(timestamps_ms[start_idx]) % step_ms
    for idx in range(start_idx + 1, hi):
        if int(timestamps_ms[idx]) % step_ms != phase:
            return None
    return phase


def _validate_archive_instrument_id(
    requested_id: str | None,
    *,
    embedded_id: str,
    expected_venue: str | None = None,
    expected_symbol: str | None = None,
) -> str:
    """Verify an explicit ``requested_id`` agrees with an archive's full venue/symbol identity."""
    if requested_id is None:
        return embedded_id
    if not isinstance(requested_id, str) or not requested_id or requested_id != requested_id.strip():
        raise ValueError(f"invalid instrument_id: {requested_id!r}")
    if requested_id == embedded_id:
        return embedded_id
    if ":" not in requested_id:
        sym = expected_symbol
        if sym is None and ":" in embedded_id:
            sym = embedded_id.split(":", 1)[1]
        if sym is not None and requested_id == sym:
            return requested_id
        raise ValueError(
            f"funding archive identity {embedded_id!r} disagrees with requested instrument_id {requested_id!r}"
        )
    req_venue, req_sym = requested_id.split(":", 1)
    if not req_venue or not req_sym:
        raise ValueError(f"invalid instrument_id: {requested_id!r}")
    if expected_venue is not None and req_venue != expected_venue:
        raise ValueError(
            f"funding archive venue {expected_venue!r} disagrees with requested instrument_id {requested_id!r}"
        )
    if expected_symbol is not None and req_sym != expected_symbol:
        raise ValueError(
            f"funding archive symbol {expected_symbol!r} disagrees with requested instrument_id {requested_id!r}"
        )
    raise ValueError(
        f"funding archive identity {embedded_id!r} disagrees with requested instrument_id {requested_id!r}"
    )


def _first_expected_after(start_ms: int, step_ms: int, phase_ms: int) -> int:
    """Smallest ``k * step_ms + phase_ms`` strictly greater than ``start_ms``."""
    rem = (start_ms - phase_ms) % step_ms
    return start_ms + (step_ms - rem)


def _last_expected_at_or_before(end_ms: int, step_ms: int, phase_ms: int) -> int:
    """Largest ``k * step_ms + phase_ms`` less than or equal to ``end_ms``."""
    rem = (end_ms - phase_ms) % step_ms
    return end_ms - rem


def format_apr_scenario_key(apr: float, *, prefix: str = "apr_") -> str:
    """Return a lossless, injective scenario key for ``apr`` (e.g. ``0.15 -> 'apr_15pct'``).

    Preserves readable labels for common APRs while guaranteeing distinct keys
    for every pair of distinct finite IEEE-754 floats (no ``:g`` rounding collisions).
    """
    val = _validate_rate(apr, "apr")
    dec = Decimal(str(val))
    tup = dec.as_tuple()
    assert isinstance(tup.exponent, int)
    scaled = Decimal((tup.sign, tup.digits, tup.exponent + 2))
    text = format(scaled, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return f"{prefix}{text}pct"


@dataclass(frozen=True, slots=True)
class FundingRateEvent:
    """Single perpetual funding settlement event.

    Sign convention:
      - ``rate > 0``: longs pay, shorts collect
      - ``rate < 0``: shorts pay, longs collect
    """

    ts_ms: int
    rate: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_ms", _validate_int_ts(self.ts_ms, "ts_ms"))
        object.__setattr__(self, "rate", _validate_rate(self.rate, "rate"))


class FundingSeries:
    """Immutable strictly-ordered historical funding-rate series with read-only public columns."""

    __slots__ = (
        "_instrument_id",
        "_timestamps_buf",
        "_rates_buf",
        "_settlement_interval_hours",
    )

    def __init__(
        self,
        instrument_id: str,
        timestamps_ms: Sequence[int] | array | memoryview,
        rates: Sequence[float] | array | memoryview,
        settlement_interval_hours: int | None = None,
    ) -> None:
        if not isinstance(instrument_id, str) or not instrument_id or instrument_id != instrument_id.strip():
            raise ValueError("instrument_id must be a non-empty string without surrounding whitespace")
        resolved_hours = _validate_interval_hours(settlement_interval_hours)

        if isinstance(timestamps_ms, array) and timestamps_ms.typecode == "q":
            ts_arr = array("q", timestamps_ms)
        elif (
            isinstance(timestamps_ms, memoryview)
            and timestamps_ms.format == "q"
            and timestamps_ms.ndim == 1
            and timestamps_ms.c_contiguous
        ):
            ts_arr = array("q")
            ts_arr.frombytes(timestamps_ms.cast("B"))
        else:
            ts_arr = array("q", (_validate_int_ts(v, "timestamps_ms") for v in timestamps_ms))

        if isinstance(rates, array) and rates.typecode == "d":
            rate_arr = array("d", rates)
        elif (
            isinstance(rates, memoryview)
            and rates.format == "d"
            and rates.ndim == 1
            and rates.c_contiguous
        ):
            rate_arr = array("d")
            rate_arr.frombytes(rates.cast("B"))
        else:
            rate_arr = array("d", (_validate_rate(v, "rates") for v in rates))

        if len(ts_arr) != len(rate_arr):
            raise ValueError(
                f"timestamps_ms length ({len(ts_arr)}) must match rates length ({len(rate_arr)})"
            )

        prev_ts = -1
        for idx in range(len(ts_arr)):
            ts = ts_arr[idx]
            if ts < 0:
                raise ValueError("funding timestamps must be non-negative")
            if prev_ts >= 0 and ts <= prev_ts:
                raise ValueError("funding timestamps must be strictly increasing")
            prev_ts = ts
            rate = rate_arr[idx]
            if not math.isfinite(rate):
                raise ValueError(f"funding rate at index {idx} must be finite")
            if rate == 0.0:
                rate_arr[idx] = 0.0

        # Freeze validated buffers into immutable ``bytes`` so public ``memoryview.obj`` is never writable.
        ts_buf = memoryview(ts_arr.tobytes()).cast("q")
        rate_buf = memoryview(rate_arr.tobytes()).cast("d")

        object.__setattr__(self, "_instrument_id", instrument_id)
        object.__setattr__(self, "_timestamps_buf", ts_buf)
        object.__setattr__(self, "_rates_buf", rate_buf)
        object.__setattr__(self, "_settlement_interval_hours", resolved_hours)

    def __setattr__(self, name: str, value: Any) -> None:
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError(f"cannot delete field {name!r}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FundingSeries):
            return NotImplemented
        return (
            self._instrument_id == other._instrument_id
            and self._settlement_interval_hours == other._settlement_interval_hours
            and self._timestamps_buf == other._timestamps_buf
            and self._rates_buf == other._rates_buf
        )

    def __repr__(self) -> str:
        return (
            f"FundingSeries(instrument_id={self._instrument_id!r}, "
            f"len={len(self._timestamps_buf)}, "
            f"settlement_interval_hours={self._settlement_interval_hours!r})"
        )

    @property
    def instrument_id(self) -> str:
        return self._instrument_id

    @property
    def settlement_interval_hours(self) -> int | None:
        return self._settlement_interval_hours

    @property
    def timestamps_ms(self) -> memoryview:
        """Independent read-only int64 view backed by immutable bytes (safe to ``.release()``)."""
        return memoryview(self._timestamps_buf)

    @property
    def rates(self) -> memoryview:
        """Independent read-only float64 view backed by immutable bytes (safe to ``.release()``)."""
        return memoryview(self._rates_buf)

    def __len__(self) -> int:
        return len(self._timestamps_buf)

    def __bool__(self) -> bool:
        return len(self._timestamps_buf) > 0

    def __getitem__(self, index: int) -> FundingRateEvent:
        return FundingRateEvent(ts_ms=int(self._timestamps_buf[index]), rate=float(self._rates_buf[index]))

    def __iter__(self) -> Iterator[FundingRateEvent]:
        ts_arr = self._timestamps_buf
        rate_arr = self._rates_buf
        for idx in range(len(ts_arr)):
            yield FundingRateEvent(ts_ms=int(ts_arr[idx]), rate=float(rate_arr[idx]))

    @property
    def first_ts_ms(self) -> int | None:
        return int(self._timestamps_buf[0]) if self._timestamps_buf else None

    @property
    def last_ts_ms(self) -> int | None:
        return int(self._timestamps_buf[-1]) if self._timestamps_buf else None

    def between_indices(self, start_ms: int, end_ms: int) -> tuple[int, int]:
        """Return ``(lo, hi)`` slice bounds for events with ``start_ms < ts_ms <= end_ms``."""
        if end_ms <= start_ms or not self._timestamps_buf:
            return (0, 0)
        lo = bisect_right(self._timestamps_buf, start_ms)
        hi = bisect_right(self._timestamps_buf, end_ms)
        return (lo, hi)

    def between(self, start_ms: int, end_ms: int) -> tuple[FundingRateEvent, ...]:
        """Return all funding events strictly in ``(start_ms, end_ms]`` in O(log N + K)."""
        lo, hi = self.between_indices(start_ms, end_ms)
        if lo >= hi:
            return ()
        ts_arr = self._timestamps_buf
        rate_arr = self._rates_buf
        return tuple(
            FundingRateEvent(ts_ms=int(ts_arr[idx]), rate=float(rate_arr[idx]))
            for idx in range(lo, hi)
        )

    def rate_sum_between(self, start_ms: int, end_ms: int) -> float:
        """Fast allocation-free sum of signed funding rates in ``(start_ms, end_ms]``."""
        lo, hi = self.between_indices(start_ms, end_ms)
        if lo >= hi:
            return 0.0
        total = 0.0
        rate_arr = self._rates_buf
        for idx in range(lo, hi):
            total += rate_arr[idx]
        return 0.0 if total == 0.0 else total

    def coverage_gaps(
        self,
        start_ms: int,
        end_ms: int,
        interval_hours: int | None = None,
        tolerance: float = 1.5,
        phase_ms: int | None = None,
    ) -> tuple[tuple[int, int], ...]:
        """Return ``(gap_start_ms, gap_end_ms)`` spans inside ``(start_ms, end_ms]`` with missing settlements.

        Uses ``phase_ms`` when explicitly provided, otherwise infers the settlement
        schedule phase strictly from in-window timestamps ``(start_ms, end_ms]`` so
        out-of-window events cannot hide missing settlements. When in-window phase
        evidence is ambiguous, edges spanning at least one settlement interval are
        conservatively reported as gaps.
        """
        start = _validate_int_ts(start_ms, "start_ms")
        end = _validate_int_ts(end_ms, "end_ms")
        if end < start:
            raise ValueError("end_ms must be >= start_ms")
        hours = _validate_interval_hours(
            self._settlement_interval_hours if interval_hours is None else interval_hours,
            "interval_hours",
        )
        if hours is None:
            raise ValueError("interval_hours is required when settlement_interval_hours is None")
        tol = _validate_rate(tolerance, "tolerance")
        if tol < 1.0:
            raise ValueError("tolerance must be >= 1.0")
        step_ms = hours * _HOUR_MS

        explicit_phase: int | None = None
        if phase_ms is not None:
            explicit_phase = _validate_int_ts(phase_ms, "phase_ms")
            if explicit_phase >= step_ms:
                raise ValueError(f"phase_ms ({explicit_phase}) must be < interval_ms ({step_ms})")

        if end == start:
            return ()

        ts_arr = self._timestamps_buf
        lo, hi = self.between_indices(start, end)

        if explicit_phase is not None:
            first_due = _first_expected_after(start, step_ms, explicit_phase)
            last_due = _last_expected_at_or_before(end, step_ms, explicit_phase)
            if first_due > end:
                return ()
            aligned = [int(ts_arr[i]) for i in range(lo, hi) if int(ts_arr[i]) % step_ms == explicit_phase]
            if not aligned:
                return ((start, end),)
            sched_gaps: list[tuple[int, int]] = []
            if aligned[0] > first_due:
                sched_gaps.append((start, aligned[0]))
            prev_aligned = aligned[0]
            for ts in aligned[1:]:
                if (ts - prev_aligned) > step_ms:
                    sched_gaps.append((prev_aligned, ts))
                prev_aligned = ts
            if prev_aligned < last_due:
                sched_gaps.append((prev_aligned, end))
            return tuple(sched_gaps)

        threshold_ms = float(step_ms) * tol
        edge_slack_ms = float(step_ms) * (tol - 1.0)

        if lo >= hi:
            if (end - start) > threshold_ms:
                return ((start, end),)
            if lo > 0 and int(ts_arr[lo - 1]) == start:
                check_phase = int(ts_arr[lo - 1]) % step_ms
            else:
                check_phase = 0
            if _first_expected_after(start, step_ms, check_phase) <= end:
                return ((start, end),)
            return ()

        in_window_phase = _infer_settlement_phase_ms(ts_arr, step_ms, start_idx=lo, end_idx=hi)

        gaps: list[tuple[int, int]] = []
        first_ts = int(ts_arr[lo])
        if in_window_phase is not None:
            first_due = _first_expected_after(start, step_ms, in_window_phase)
            leading_missing = (first_ts - start) > threshold_ms or (first_ts - first_due) > edge_slack_ms
        else:
            # Ambiguous in-window phases: fail closed on any leading span >= 1 settlement interval
            # or violating the leading event's own schedule phase.
            first_due = _first_expected_after(start, step_ms, first_ts % step_ms)
            leading_missing = (first_ts - start) >= step_ms or (first_ts - first_due) > edge_slack_ms
        if leading_missing:
            gaps.append((start, first_ts))

        prev_ts = first_ts
        for idx in range(lo + 1, hi):
            ts = int(ts_arr[idx])
            if (ts - prev_ts) > threshold_ms:
                gaps.append((prev_ts, ts))
            prev_ts = ts

        if in_window_phase is not None:
            last_due = _last_expected_at_or_before(end, step_ms, in_window_phase)
            trailing_missing = (end - prev_ts) > threshold_ms or (last_due - prev_ts) > edge_slack_ms
        else:
            last_due = _last_expected_at_or_before(end, step_ms, prev_ts % step_ms)
            trailing_missing = (end - prev_ts) >= step_ms or (last_due - prev_ts) > edge_slack_ms
        if trailing_missing:
            gaps.append((prev_ts, end))

        return tuple(gaps)

    def coverage_ratio(
        self,
        start_ms: int,
        end_ms: int,
        interval_hours: int | None = None,
        tolerance: float = 1.5,
        phase_ms: int | None = None,
    ) -> float:
        """Return fraction in ``[0.0, 1.0]`` of ``(start_ms, end_ms]`` covered without funding gaps."""
        gaps = self.coverage_gaps(start_ms, end_ms, interval_hours, tolerance, phase_ms=phase_ms)
        span = end_ms - start_ms
        if span <= 0 or not gaps:
            return 1.0
        uncovered = sum(g_end - g_start for g_start, g_end in gaps)
        ratio = 1.0 - (uncovered / float(span))
        if ratio <= 0.0:
            return 0.0
        return 1.0 if ratio >= 1.0 else ratio

    def fill_gaps_with_apr(
        self,
        *,
        start_ms: int,
        end_ms: int,
        interval_hours: int | None = None,
        apr: float,
        tolerance: float = 1.5,
        phase_ms: int | None = None,
    ) -> "FundingSeries":
        """Return a new FundingSeries preserving historical events and filling detected gaps on schedule.

        Resolves and validates ``phase_ms`` BEFORE gap detection so explicit settlement
        phases apply consistently to both gap detection and synthesis. Raises
        ``ValueError`` if gaps exist and the settlement phase cannot be uniquely
        determined.
        """
        start = _validate_int_ts(start_ms, "start_ms")
        end = _validate_int_ts(end_ms, "end_ms")
        if end < start:
            raise ValueError("end_ms must be >= start_ms")
        _validate_rate(apr, "apr")
        hours = _validate_interval_hours(
            self._settlement_interval_hours if interval_hours is None else interval_hours,
            "interval_hours",
        )
        if hours is None:
            raise ValueError("interval_hours is required when settlement_interval_hours is None")
        step_ms = hours * _HOUR_MS

        if phase_ms is not None:
            resolved_phase = _validate_int_ts(phase_ms, "phase_ms")
            if resolved_phase >= step_ms:
                raise ValueError(f"phase_ms ({resolved_phase}) must be < interval_ms ({step_ms})")
        else:
            lo, hi = self.between_indices(start, end)
            inferred_phase = (
                _infer_settlement_phase_ms(self._timestamps_buf, step_ms, start_idx=lo, end_idx=hi)
                if lo < hi
                else _infer_settlement_phase_ms(self._timestamps_buf, step_ms)
            )
            if inferred_phase is None:
                raise ValueError(
                    "cannot fill funding gaps: settlement schedule phase cannot be uniquely "
                    "determined from existing events (provide explicit phase_ms or align history)"
                )
            resolved_phase = inferred_phase

        gaps = self.coverage_gaps(start, end, hours, tolerance, phase_ms=resolved_phase)
        if not gaps:
            return self

        by_ts: dict[int, float] = {
            int(self._timestamps_buf[i]): float(self._rates_buf[i])
            for i in range(len(self._timestamps_buf))
        }
        for g_start, g_end in gaps:
            synth_end = (g_end - 1) if g_end in by_ts else g_end
            if synth_end <= g_start:
                continue
            synth = FundingSeries.synthetic_constant_apr(
                self._instrument_id,
                start_ms=g_start,
                end_ms=synth_end,
                interval_hours=hours,
                apr=apr,
                phase_ms=resolved_phase,
            )
            for idx in range(len(synth._timestamps_buf)):
                ts = int(synth._timestamps_buf[idx])
                if ts not in by_ts:
                    by_ts[ts] = float(synth._rates_buf[idx])

        ordered_ts = sorted(by_ts)
        return FundingSeries(
            instrument_id=self._instrument_id,
            timestamps_ms=array("q", ordered_ts),
            rates=array("d", (by_ts[ts] for ts in ordered_ts)),
            settlement_interval_hours=hours,
        )

    @classmethod
    def from_events(
        cls,
        instrument_id: str,
        events: Iterable[FundingRateEvent],
        *,
        settlement_interval_hours: int | None = None,
        sort: bool = False,
    ) -> "FundingSeries":
        event_list = list(events)
        for item in event_list:
            if not isinstance(item, FundingRateEvent):
                raise TypeError(f"expected FundingRateEvent, got {type(item).__name__}")
        if sort:
            event_list.sort(key=lambda e: e.ts_ms)
        ts_arr = array("q", (e.ts_ms for e in event_list))
        rate_arr = array("d", (e.rate for e in event_list))
        inferred = (
            settlement_interval_hours
            if settlement_interval_hours is not None
            else _infer_interval_hours(ts_arr)
        )
        return cls(
            instrument_id=instrument_id,
            timestamps_ms=ts_arr,
            rates=rate_arr,
            settlement_interval_hours=inferred,
        )

    @classmethod
    def synthetic_constant_apr(
        cls,
        instrument_id: str,
        *,
        start_ms: int,
        end_ms: int,
        interval_hours: int,
        apr: float,
        phase_ms: int = 0,
    ) -> "FundingSeries":
        """Build a synthetic funding series at constant annualized rate ``apr`` over ``(start_ms, end_ms]``."""
        start = _validate_int_ts(start_ms, "start_ms")
        end = _validate_int_ts(end_ms, "end_ms")
        if end < start:
            raise ValueError("end_ms must be >= start_ms")
        hours = _validate_interval_hours(interval_hours, "interval_hours")
        assert hours is not None
        annual_rate = _validate_rate(apr, "apr")
        step_ms = hours * _HOUR_MS
        phase = _validate_int_ts(phase_ms, "phase_ms")
        if phase >= step_ms:
            raise ValueError(f"phase_ms ({phase}) must be < interval_ms ({step_ms})")
        periods_per_year = _YEAR_HOURS / float(hours)
        per_period_rate = annual_rate / periods_per_year
        if per_period_rate == 0.0:
            per_period_rate = 0.0

        first_ts = _first_expected_after(start, step_ms, phase)
        ts_arr = array("q")
        rate_arr = array("d")
        ts = first_ts
        while ts <= end:
            ts_arr.append(ts)
            rate_arr.append(per_period_rate)
            ts += step_ms
        return cls(
            instrument_id=instrument_id,
            timestamps_ms=ts_arr,
            rates=rate_arr,
            settlement_interval_hours=hours,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FUNDING_JSON_SCHEMA_VERSION,
            "instrument_id": self._instrument_id,
            "settlement_interval_hours": self._settlement_interval_hours,
            "events": [
                {"ts_ms": int(self._timestamps_buf[i]), "rate": float(self._rates_buf[i])}
                for i in range(len(self._timestamps_buf))
            ],
        }

    def to_json_file(self, path: Path | str) -> None:
        """Atomically persist this funding series to JSON (temp + fsync + os.replace)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        tmp_path = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with tmp_path.open("wb") as handle:
                handle.write(payload)
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

    @classmethod
    def from_json_file(
        cls,
        path: Path | str,
        *,
        instrument_id: str | None = None,
        allow_legacy_okx_funding_rate: bool = False,
    ) -> "FundingSeries":
        """Load a FundingSeries from canonical JSON or raw OKX/Binance funding archive JSON.

        For OKX archives, ``realizedRate`` is required by default so predicted
        ``fundingRate`` values are never silently treated as realized settlements;
        pass ``allow_legacy_okx_funding_rate=True`` only for legacy archives that
        explicitly store actual settled rates in ``fundingRate``.
        """
        target = Path(path)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"failed to load funding JSON from {target}: {exc}") from exc

        if isinstance(raw, dict) and "events" in raw:
            version = raw.get("schema_version")
            if version != FUNDING_JSON_SCHEMA_VERSION:
                raise ValueError(f"unsupported funding JSON schema_version: {version!r}")
            raw_events = raw.get("events")
            if not isinstance(raw_events, list):
                raise ValueError("funding JSON 'events' must be a list")
            embedded_id = raw.get("instrument_id")
            if embedded_id is not None:
                if not isinstance(embedded_id, str) or not embedded_id or embedded_id != embedded_id.strip():
                    raise ValueError(f"funding JSON has invalid instrument_id: {embedded_id!r}")
                resolved_id = _validate_archive_instrument_id(
                    instrument_id,
                    embedded_id=embedded_id,
                )
            else:
                if not isinstance(instrument_id, str) or not instrument_id or instrument_id != instrument_id.strip():
                    raise ValueError("funding JSON missing instrument_id")
                resolved_id = instrument_id
            events: list[FundingRateEvent] = []
            for item in raw_events:
                if not isinstance(item, Mapping):
                    raise ValueError("all funding JSON events must be objects")
                events.append(
                    FundingRateEvent(
                        ts_ms=_parse_int_field(item.get("ts_ms"), "ts_ms"),
                        rate=_parse_float_field(item.get("rate"), "rate"),
                    )
                )
            return cls.from_events(
                resolved_id,
                events,
                settlement_interval_hours=raw.get("settlement_interval_hours"),
                sort=False,
            )

        # Compatibility loader for raw OKX funding archive files (e.g. {"instId": ..., "records": [...]})
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            inst = raw.get("instId")
            if inst is not None:
                if not isinstance(inst, str) or not inst or inst != inst.strip():
                    raise ValueError(f"OKX funding JSON has invalid instId: {inst!r}")
                resolved_id = _validate_archive_instrument_id(
                    instrument_id,
                    embedded_id=f"okx:{inst}",
                    expected_venue="okx",
                    expected_symbol=inst,
                )
                expected_inst = inst
            else:
                if not isinstance(instrument_id, str) or not instrument_id or instrument_id != instrument_id.strip():
                    raise ValueError("OKX funding JSON missing instId / instrument_id")
                if ":" in instrument_id:
                    req_venue, expected_inst = instrument_id.split(":", 1)
                    if req_venue != "okx" or not expected_inst:
                        raise ValueError(
                            f"OKX funding JSON cannot be loaded under non-OKX instrument_id {instrument_id!r}"
                        )
                else:
                    expected_inst = instrument_id
                resolved_id = instrument_id
            parsed = _parse_okx_rows(
                raw["records"],
                expected_inst_id=expected_inst,
                allow_funding_rate_fallback=allow_legacy_okx_funding_rate,
            )
            return cls.from_events(resolved_id, parsed, sort=True)

        # Compatibility loader for raw Binance funding arrays ([{"symbol": ..., "fundingTime": ..., "fundingRate": ...}])
        if isinstance(raw, list):
            if not raw:
                if not isinstance(instrument_id, str) or not instrument_id or instrument_id != instrument_id.strip():
                    raise ValueError("empty Binance funding JSON requires instrument_id")
                if ":" in instrument_id:
                    req_venue, req_sym = instrument_id.split(":", 1)
                    if req_venue != "binance" or not req_sym:
                        raise ValueError(
                            f"Binance funding JSON cannot be loaded under non-Binance instrument_id {instrument_id!r}"
                        )
                return cls.from_events(instrument_id, ())
            first = raw[0]
            sym = first.get("symbol") if isinstance(first, Mapping) else None
            if not isinstance(sym, str) or not sym or sym != sym.strip():
                raise ValueError("Binance funding JSON missing symbol / instrument_id")
            resolved_id = _validate_archive_instrument_id(
                instrument_id,
                embedded_id=f"binance:{sym}",
                expected_venue="binance",
                expected_symbol=sym,
            )
            parsed = _parse_binance_rows(raw, expected_symbol=sym)
            return cls.from_events(resolved_id, parsed, sort=True)

        raise ValueError(f"unrecognized funding JSON format in {target}")

    @classmethod
    def require_or_sensitivity_series(
        cls,
        series: "FundingSeries | None",
        *,
        instrument_id: str | None = None,
        start_ms: int,
        end_ms: int,
        interval_hours: int | None,
        apr_band: Sequence[float] = (0.0, 0.15, 0.30),
        tolerance: float = 1.5,
    ) -> "FundingResolution":
        return require_or_sensitivity_series(
            series,
            instrument_id=instrument_id,
            start_ms=start_ms,
            end_ms=end_ms,
            interval_hours=interval_hours,
            apr_band=apr_band,
            tolerance=tolerance,
        )


@dataclass(frozen=True, slots=True)
class FundingResolution:
    """Explicit resolution of funding data: either historical or mandatory APR sensitivity band."""

    mode: Literal["historical", "sensitivity_band"]
    scenarios: tuple[tuple[str, FundingSeries], ...]
    apr_band: tuple[float, ...] = ()

    @property
    def is_historical(self) -> bool:
        return self.mode == "historical"

    @property
    def is_sensitivity(self) -> bool:
        return self.mode == "sensitivity_band"

    def as_dict(self) -> dict[str, FundingSeries]:
        return dict(self.scenarios)


def require_funding_series(
    series: FundingSeries | None,
    *,
    instrument_id: str,
) -> FundingSeries:
    """Fail closed if historical funding series is missing or empty."""
    if series is None or len(series) == 0:
        raise MissingFundingHistoryError(
            f"missing historical funding series for {instrument_id}; "
            "use require_or_sensitivity_series(...) to run an explicit APR sensitivity band"
        )
    return series


def require_or_sensitivity_series(
    series: FundingSeries | None = None,
    *,
    instrument_id: str | None = None,
    start_ms: int,
    end_ms: int,
    interval_hours: int | None,
    apr_band: Sequence[float] = (0.0, 0.15, 0.30),
    tolerance: float = 1.5,
) -> FundingResolution:
    """Return historical funding iff it matches ``instrument_id`` and fully covers ``(start_ms, end_ms]``, else synthesize an explicit APR band.

    - If ``instrument_id`` is explicitly provided and disagrees with ``series.instrument_id``,
      raises ``ValueError``.
    - Partial historical funding (non-empty series with coverage gaps over
      ``(start_ms, end_ms]``) fails closed with ``MissingFundingHistoryError`` rather
      than being mislabeled as ``historical``.
    - Missing/empty funding history is NEVER allowed to silently default to a single
      ``0.0`` series: ``apr_band`` must contain at least 2 distinct APR points including
      at least one non-zero APR (default ``(0.0, 0.15, 0.30)``).
    """
    if series is not None and instrument_id is not None and series.instrument_id != instrument_id:
        raise ValueError(
            f"funding series instrument_id {series.instrument_id!r} does not match "
            f"requested instrument_id {instrument_id!r}"
        )
    if series is not None and len(series) > 0:
        resolved_hours = _validate_interval_hours(
            interval_hours if interval_hours is not None else series.settlement_interval_hours,
            "interval_hours",
        )
        if resolved_hours is None:
            raise MissingFundingHistoryError(
                f"cannot verify historical funding coverage for {series.instrument_id} without interval_hours"
            )
        gaps = series.coverage_gaps(start_ms, end_ms, resolved_hours, tolerance=tolerance)
        if gaps:
            raise MissingFundingHistoryError(
                f"historical funding series for {series.instrument_id} has {len(gaps)} coverage gap(s) "
                f"over ({start_ms}, {end_ms}]; partial funding cannot be labeled as historical"
            )
        return FundingResolution(
            mode="historical",
            scenarios=(("historical", series),),
            apr_band=(),
        )

    resolved_instrument_id = (
        instrument_id
        if instrument_id is not None
        else (series.instrument_id if series is not None else "unknown:PERP")
    )
    hours = _validate_interval_hours(interval_hours, "interval_hours")
    if hours is None:
        raise MissingFundingHistoryError(
            f"cannot synthesize funding sensitivity band for {resolved_instrument_id} without interval_hours"
        )
    if isinstance(apr_band, (str, bytes)) or not isinstance(apr_band, Sequence):
        raise ValueError("apr_band must be a sequence of finite APR floats")
    normalized_band = tuple(_validate_rate(v, "apr_band entry") for v in apr_band)
    if len(set(normalized_band)) != len(normalized_band):
        raise ValueError("apr_band entries must be distinct")
    if len(normalized_band) < 2 or all(v == 0.0 for v in normalized_band):
        raise ValueError(
            "missing funding history cannot silently default to 0.0; "
            "provide an explicit multi-point apr_band with at least one non-zero APR "
            "(e.g. (0.0, 0.15, 0.30))"
        )

    scenarios: list[tuple[str, FundingSeries]] = []
    for apr in normalized_band:
        label = format_apr_scenario_key(apr, prefix="apr_")
        synth = FundingSeries.synthetic_constant_apr(
            instrument_id=resolved_instrument_id,
            start_ms=start_ms,
            end_ms=end_ms,
            interval_hours=hours,
            apr=apr,
        )
        scenarios.append((label, synth))
    return FundingResolution(
        mode="sensitivity_band",
        scenarios=tuple(scenarios),
        apr_band=normalized_band,
    )


def _parse_okx_rows(
    rows: Sequence[Any],
    *,
    expected_inst_id: str | None = None,
    allow_funding_rate_fallback: bool = False,
) -> list[FundingRateEvent]:
    by_ts: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("OKX funding row must be an object")
        row_inst = row.get("instId")
        if expected_inst_id is not None and row_inst is not None:
            if not isinstance(row_inst, str) or row_inst != expected_inst_id:
                raise ValueError(
                    f"OKX funding row instId mismatch: expected {expected_inst_id!r}, got {row_inst!r}"
                )
        ts_ms = _parse_int_field(row.get("fundingTime"), "fundingTime")
        raw_rate = row.get("realizedRate")
        if raw_rate is None or (isinstance(raw_rate, str) and not raw_rate.strip()):
            if not allow_funding_rate_fallback:
                raise ValueError(
                    "OKX funding row is missing realizedRate; refusing to treat predicted fundingRate as realized"
                )
            raw_rate = row.get("fundingRate")
        rate = _parse_float_field(raw_rate, "realizedRate")
        existing = by_ts.get(ts_ms)
        if existing is not None and not math.isclose(existing, rate, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"conflicting duplicate OKX funding rate at ts_ms={ts_ms}")
        by_ts[ts_ms] = rate
    return [FundingRateEvent(ts_ms=ts, rate=by_ts[ts]) for ts in sorted(by_ts)]


def _parse_binance_rows(
    rows: Sequence[Any],
    *,
    expected_symbol: str | None = None,
) -> list[FundingRateEvent]:
    resolved_symbol = expected_symbol
    by_ts: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Binance funding row must be an object")
        row_sym = row.get("symbol")
        if not isinstance(row_sym, str) or not row_sym or row_sym != row_sym.strip():
            raise ValueError(f"Binance funding row missing valid symbol: {row_sym!r}")
        if resolved_symbol is None:
            resolved_symbol = row_sym
        elif row_sym != resolved_symbol:
            raise ValueError(
                f"Binance funding rows mix instruments: expected {resolved_symbol!r}, got {row_sym!r}"
            )
        ts_ms = _parse_int_field(row.get("fundingTime"), "fundingTime")
        rate = _parse_float_field(row.get("fundingRate"), "fundingRate")
        existing = by_ts.get(ts_ms)
        if existing is not None and not math.isclose(existing, rate, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"conflicting duplicate Binance funding rate at ts_ms={ts_ms}")
        by_ts[ts_ms] = rate
    return [FundingRateEvent(ts_ms=ts, rate=by_ts[ts]) for ts in sorted(by_ts)]


def fetch_okx_funding_history(
    symbol: str,
    *,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 100,
    max_pages: int = 100,
    settlement_interval_hours: int | None = None,
    timeout: float = 20.0,
    retries: int = 3,
) -> FundingSeries:
    """Fetch public OKX historical funding rates via ``GET /api/v5/public/funding-rate-history``."""
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if start_time_ms is not None:
        start_time_ms = _validate_int_ts(start_time_ms, "start_time_ms")
    if end_time_ms is not None:
        end_time_ms = _validate_int_ts(end_time_ms, "end_time_ms")
    if start_time_ms is not None and end_time_ms is not None and end_time_ms < start_time_ms:
        raise ValueError("end_time_ms must be >= start_time_ms")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    page_limit = min(limit, 100)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages must be a positive integer")

    by_ts: dict[int, float] = {}
    # OKX ``after`` returns records strictly older than ``after`` (newest-first).
    after: int | None = (end_time_ms + 1) if end_time_ms is not None else None

    for _ in range(max_pages):
        params: dict[str, str | int] = {
            "instId": symbol,
            "limit": page_limit,
        }
        if after is not None:
            params["after"] = after
        url = f"{OKX_FUNDING_HISTORY_URL}?{urllib.parse.urlencode(params)}"
        payload = get_json(
            url,
            headers=_OKX_HEADERS,
            timeout=timeout,
            retries=retries,
            retry_backoff_base=0.25,
            retry_backoff_cap=2.0,
        )
        if not isinstance(payload, Mapping) or payload.get("code") != "0":
            msg = payload.get("msg") if isinstance(payload, Mapping) else None
            code = payload.get("code") if isinstance(payload, Mapping) else None
            raise RuntimeError(f"OKX funding history request failed: {msg or code or 'invalid response'}")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("OKX funding history payload 'data' must be a list")
        if not rows:
            break

        page_events = _parse_okx_rows(
            rows,
            expected_inst_id=symbol,
            allow_funding_rate_fallback=False,
        )
        page_oldest = page_events[0].ts_ms
        for event in page_events:
            if end_time_ms is not None and event.ts_ms > end_time_ms:
                continue
            if start_time_ms is not None and event.ts_ms < start_time_ms:
                continue
            existing = by_ts.get(event.ts_ms)
            if existing is not None and not math.isclose(existing, event.rate, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"conflicting duplicate OKX funding rate at ts_ms={event.ts_ms}")
            by_ts[event.ts_ms] = event.rate

        if start_time_ms is not None and page_oldest <= start_time_ms:
            break
        if len(rows) < page_limit:
            break
        if after is not None and page_oldest >= after:
            break
        after = page_oldest

    ordered_events = [FundingRateEvent(ts_ms=ts, rate=by_ts[ts]) for ts in sorted(by_ts)]
    return FundingSeries.from_events(
        f"okx:{symbol}",
        ordered_events,
        settlement_interval_hours=settlement_interval_hours,
        sort=False,
    )


def fetch_binance_funding_history(
    symbol: str,
    *,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 1000,
    max_pages: int = 100,
    settlement_interval_hours: int | None = None,
    timeout: float = 20.0,
    retries: int = 3,
) -> FundingSeries:
    """Fetch public Binance USD-M historical funding rates via ``GET /fapi/v1/fundingRate``."""
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if start_time_ms is not None:
        start_time_ms = _validate_int_ts(start_time_ms, "start_time_ms")
    if end_time_ms is not None:
        end_time_ms = _validate_int_ts(end_time_ms, "end_time_ms")
    if start_time_ms is not None and end_time_ms is not None and end_time_ms < start_time_ms:
        raise ValueError("end_time_ms must be >= start_time_ms")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    page_limit = min(limit, 1000)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages must be a positive integer")

    by_ts: dict[int, float] = {}
    cursor: int | None = start_time_ms

    for _ in range(max_pages):
        params: dict[str, str | int] = {
            "symbol": symbol,
            "limit": page_limit,
        }
        if cursor is not None:
            params["startTime"] = cursor
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        url = f"{BINANCE_FUNDING_HISTORY_URL}?{urllib.parse.urlencode(params)}"
        payload = get_json(
            url,
            headers=_BINANCE_HEADERS,
            timeout=timeout,
            retries=retries,
            retry_backoff_base=0.25,
            retry_backoff_cap=2.0,
        )
        if not isinstance(payload, list):
            raise RuntimeError("Binance funding history response must be a JSON list")
        if not payload:
            break

        page_events = _parse_binance_rows(payload, expected_symbol=symbol)
        page_newest = page_events[-1].ts_ms
        for event in page_events:
            if start_time_ms is not None and event.ts_ms < start_time_ms:
                continue
            if end_time_ms is not None and event.ts_ms > end_time_ms:
                continue
            existing = by_ts.get(event.ts_ms)
            if existing is not None and not math.isclose(existing, event.rate, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"conflicting duplicate Binance funding rate at ts_ms={event.ts_ms}")
            by_ts[event.ts_ms] = event.rate

        if len(payload) < page_limit:
            break
        if end_time_ms is not None and page_newest >= end_time_ms:
            break
        next_cursor = page_newest + 1
        if cursor is not None and next_cursor <= cursor:
            break
        cursor = next_cursor

    ordered_events = [FundingRateEvent(ts_ms=ts, rate=by_ts[ts]) for ts in sorted(by_ts)]
    return FundingSeries.from_events(
        f"binance:{symbol}",
        ordered_events,
        settlement_interval_hours=settlement_interval_hours,
        sort=False,
    )


__all__ = [
    "BINANCE_FUNDING_HISTORY_URL",
    "FUNDING_JSON_SCHEMA_VERSION",
    "FundingRateEvent",
    "FundingResolution",
    "FundingSeries",
    "MissingFundingHistoryError",
    "OKX_FUNDING_HISTORY_URL",
    "fetch_binance_funding_history",
    "fetch_okx_funding_history",
    "format_apr_scenario_key",
    "require_funding_series",
    "require_or_sensitivity_series",
]
