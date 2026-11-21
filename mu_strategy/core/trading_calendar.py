"""Offline reference-market sessions; strategy windows remain a separate constraint."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from mu_strategy.canonical import canonical_sha256


LEGACY_CALENDAR = "weekday_windows_v1"
US_EQUITIES_CALENDAR = "us_equities_2025_2028_v1"
US_EQUITIES_ALGORITHMIC_CALENDAR = "us_equities_nyse_algorithmic_v1"
CONTINUOUS_CALENDAR = "continuous_v1"
CALENDAR_IDS = (
    LEGACY_CALENDAR,
    US_EQUITIES_CALENDAR,
    US_EQUITIES_ALGORITHMIC_CALENDAR,
    CONTINUOUS_CALENDAR,
)

_US_EQUITIES_V1_YEARS = (2025, 2026, 2027, 2028)
_ALGORITHMIC_MIN_YEAR = 2018

# Immutable NYSE 2025-2028 cash-equity schedule for the frozen us_equities_2025_2028_v1 ID.
# Kept independent of _nyse_year_schedule so future algorithmic updates never alter saved digests.
_FIXED_US_EQUITIES_2025_2028_HOLIDAYS: dict[int, str] = {
    2025: "01-01 01-09 01-20 02-17 04-18 05-26 06-19 07-04 09-01 11-27 12-25",
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25",
    2027: "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24",
    2028: "01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25",
}
_FIXED_US_EQUITIES_2025_2028_EARLY_CLOSES: dict[int, str] = {
    2025: "07-03 11-28 12-24",
    2026: "11-27 12-24",
    2027: "11-26",
    2028: "07-03 11-24",
}
_FIXED_US_EQUITIES_2025_2028_DATES: dict[int, tuple[frozenset[date], frozenset[date]]] = {
    year: (
        frozenset(
            date(year, int(token[:2]), int(token[3:]))
            for token in _FIXED_US_EQUITIES_2025_2028_HOLIDAYS[year].split()
        ),
        frozenset(
            date(year, int(token[:2]), int(token[3:]))
            for token in _FIXED_US_EQUITIES_2025_2028_EARLY_CLOSES[year].split()
        ),
    )
    for year in _US_EQUITIES_V1_YEARS
}

# Verified extraordinary unscheduled NYSE closures since 2018 (National Days of Mourning).
_EXCEPTIONAL_NYSE_CLOSURES: frozenset[date] = frozenset({
    date(2018, 12, 5),  # National Day of Mourning for President George H. W. Bush
    date(2025, 1, 9),   # National Day of Mourning for President Jimmy Carter
})


class CalendarUnavailable(ValueError):
    pass


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) for Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _observed_nyse(day: date, *, observe_saturday_on_friday: bool = True) -> date | None:
    weekday = day.weekday()
    if weekday == 5:
        return day - timedelta(days=1) if observe_saturday_on_friday else None
    if weekday == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def _nyse_year_schedule(year: int) -> tuple[frozenset[date], frozenset[date]]:
    """Deterministic algorithmic NYSE cash-equity holidays and 13:00 ET early closes."""
    holidays: set[date] = set()

    # NYSE Rule 7.2: New Year's Day falling on Saturday is NOT observed on prior Friday Dec 31.
    new_years = _observed_nyse(date(year, 1, 1), observe_saturday_on_friday=False)
    if new_years is not None:
        holidays.add(new_years)

    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))  # MLK Day (3rd Monday in January)
    holidays.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday (3rd Monday in February)
    holidays.add(_easter_sunday(year) - timedelta(days=2))  # Good Friday
    holidays.add(_last_weekday(year, 5, 0))  # Memorial Day (last Monday in May)
    if year >= 2022:
        juneteenth = _observed_nyse(date(year, 6, 19))
        if juneteenth is not None:
            holidays.add(juneteenth)
    independence = _observed_nyse(date(year, 7, 4))
    if independence is not None:
        holidays.add(independence)
    holidays.add(_nth_weekday(year, 9, 0, 1))  # Labor Day (1st Monday in September)
    thanksgiving = _nth_weekday(year, 11, 3, 4)  # Thanksgiving (4th Thursday in November)
    holidays.add(thanksgiving)
    christmas = _observed_nyse(date(year, 12, 25))
    if christmas is not None:
        holidays.add(christmas)

    for closure in _EXCEPTIONAL_NYSE_CLOSURES:
        if closure.year == year:
            holidays.add(closure)

    early_closes: set[date] = set()
    # July 3 early close when July 3 is Mon-Thu (i.e. July 4 is Tue-Fri) and not a holiday.
    july_3 = date(year, 7, 3)
    if july_3.weekday() < 4 and july_3 not in holidays:
        early_closes.add(july_3)
    # Day after Thanksgiving (Black Friday).
    black_friday = thanksgiving + timedelta(days=1)
    if black_friday not in holidays:
        early_closes.add(black_friday)
    # Christmas Eve early close when Dec 24 is Mon-Thu (i.e. Dec 25 is Tue-Fri) and not a holiday.
    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 4 and christmas_eve not in holidays:
        early_closes.add(christmas_eve)

    return frozenset(holidays), frozenset(early_closes)


@lru_cache(maxsize=8)
def calendar_sha256(calendar_id: str) -> str:
    if calendar_id not in CALENDAR_IDS:
        raise CalendarUnavailable(f"unsupported reference calendar: {calendar_id}")
    payload = {
        "id": calendar_id,
        "timezone": "America/New_York",
        "window_end": "inclusive_minute",
        "weekends_closed": calendar_id != CONTINUOUS_CALENDAR,
    }
    if calendar_id == US_EQUITIES_CALENDAR:
        payload.update(
            holidays=_FIXED_US_EQUITIES_2025_2028_HOLIDAYS,
            early_closes=_FIXED_US_EQUITIES_2025_2028_EARLY_CLOSES,
            coverage=[_US_EQUITIES_V1_YEARS[0], _US_EQUITIES_V1_YEARS[-1]],
            cash_open="09:30",
            cash_close="16:00",
            early_close="13:00",
            cash_end="exclusive",
        )
    elif calendar_id == US_EQUITIES_ALGORITHMIC_CALENDAR:
        payload.update(
            schedule="nyse_algorithmic_v1",
            min_year=_ALGORITHMIC_MIN_YEAR,
            exceptional_closures=sorted(d.isoformat() for d in _EXCEPTIONAL_NYSE_CLOSURES),
            cash_open="09:30",
            cash_close="16:00",
            early_close="13:00",
            cash_end="exclusive",
        )
    return canonical_sha256(payload)


@lru_cache(maxsize=4)
def _eastern_for(zone_info_cls) -> ZoneInfo:
    try:
        return zone_info_cls("America/New_York")
    except (KeyError, ValueError, OSError, RuntimeError) as exc:
        raise CalendarUnavailable("America/New_York timezone unavailable") from exc


def _eastern() -> ZoneInfo:
    return _eastern_for(ZoneInfo)


def eastern_date(at_ms: int) -> date:
    """Return the America/New_York calendar date for a UTC epoch timestamp in ms."""
    return datetime.fromtimestamp(at_ms / 1000.0, tz=_eastern()).date()


def _minute(value: str) -> int:
    try:
        if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
            raise ValueError()
        hour = int(value[:2])
        minute = int(value[3:])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError()
        return hour * 60 + minute
    except (TypeError, ValueError) as exc:
        raise CalendarUnavailable("invalid strategy trading window") from exc


@lru_cache(maxsize=128)
def _parse_windows(trading_windows_et: tuple[tuple[str, str], ...]) -> tuple[tuple[int, int], ...]:
    parsed: list[tuple[int, int]] = []
    for window in trading_windows_et:
        try:
            start, end = window
        except (TypeError, ValueError) as exc:
            raise CalendarUnavailable("invalid strategy trading window") from exc
        first, last = _minute(start), _minute(end)
        if first > last:
            raise CalendarUnavailable("strategy window must not cross the ET date boundary")
        # Preserve the existing inclusive minute; the cash-market close is exclusive.
        parsed.append((first, last + 1))
    parsed.sort()
    return tuple(parsed)


def _normalize_windows_key(trading_windows_et) -> tuple[tuple[str, str], ...]:
    if isinstance(trading_windows_et, tuple):
        all_clean = True
        for window in trading_windows_et:
            if (
                type(window) is not tuple
                or len(window) != 2
                or type(window[0]) is not str
                or type(window[1]) is not str
            ):
                all_clean = False
                break
        if all_clean:
            return trading_windows_et
    try:
        if isinstance(trading_windows_et, (str, bytes)):
            raise TypeError("window collection must not be a string")
        normalized: list[tuple[str, str]] = []
        for window in trading_windows_et:
            if isinstance(window, (str, bytes)):
                raise TypeError("window entry must be a pair of HH:MM strings")
            start, end = window
            if not isinstance(start, str) or not isinstance(end, str):
                raise TypeError("window bounds must be strings")
            normalized.append((start, end))
        return tuple(normalized)
    except (TypeError, ValueError) as exc:
        raise CalendarUnavailable("invalid strategy trading window") from exc


def _session(day: date, calendar_id: str) -> tuple[str, int, int]:
    if calendar_id == US_EQUITIES_CALENDAR and not (
        _US_EQUITIES_V1_YEARS[0] <= day.year <= _US_EQUITIES_V1_YEARS[-1]
    ):
        raise CalendarUnavailable("reference calendar covers 2025 through 2028 only")
    if calendar_id == US_EQUITIES_ALGORITHMIC_CALENDAR and day.year < _ALGORITHMIC_MIN_YEAR:
        raise CalendarUnavailable(f"reference calendar covers {_ALGORITHMIC_MIN_YEAR} onward only")
    if calendar_id != CONTINUOUS_CALENDAR and day.weekday() >= 5:
        return "weekend", 0, 0
    if calendar_id not in (US_EQUITIES_CALENDAR, US_EQUITIES_ALGORITHMIC_CALENDAR):
        return "open", 0, 1440
    if calendar_id == US_EQUITIES_CALENDAR:
        holidays, early_closes = _FIXED_US_EQUITIES_2025_2028_DATES[day.year]
    else:
        holidays, early_closes = _nyse_year_schedule(day.year)
    if day in holidays:
        return "holiday", 0, 0
    if day in early_closes:
        return "early_close", 570, 780
    return "open", 570, 960


@lru_cache(maxsize=2048)
def _session_windows(
    day: date,
    calendar_id: str,
    trading_windows_et: tuple[tuple[str, str], ...],
) -> tuple[str, int, int, tuple[tuple[int, int], ...]]:
    parsed_windows = _parse_windows(trading_windows_et)
    state, opening, closing = _session(day, calendar_id)
    if opening >= closing:
        return state, opening, closing, ()
    clipped: list[tuple[int, int]] = []
    for first, stop in parsed_windows:
        start = first if first > opening else opening
        end = stop if stop < closing else closing
        if start < end:
            clipped.append((start, end))
    return state, opening, closing, tuple(clipped)


def _windows(day: date, config) -> tuple[str, int, int, tuple[tuple[int, int], ...]]:
    return _session_windows(day, config.trading_calendar_id, _normalize_windows_key(config.trading_windows_et))


def _verified_digest(config) -> str:
    digest = calendar_sha256(config.trading_calendar_id)
    if config.trading_calendar_sha256 != digest:
        raise CalendarUnavailable("reference calendar content does not match frozen configuration")
    return digest


@dataclass(frozen=True)
class TradingWindowDecision:
    calendar_id: str
    calendar_sha256: str
    evaluated_at_ms: int
    eastern_date: str
    session: str
    allowed: bool
    reason: str

    def to_dict(self):
        return asdict(self)


@lru_cache(maxsize=512)
def _utc_day_allowed_intervals(
    utc_day_index: int,
    calendar_id: str,
    trading_windows_et: tuple[tuple[str, str], ...],
    zone_info_cls,
) -> tuple[tuple[int, int], ...] | None:
    """Return exact [start_ms, end_ms) allowed intervals covering a UTC day, or None on DST switch days."""
    eastern = _eastern_for(zone_info_cls)
    utc_start_ms = utc_day_index * 86_400_000
    utc_end_ms = utc_start_ms + 86_400_000
    start_et_day = datetime.fromtimestamp(utc_start_ms / 1000.0, tz=eastern).date()
    end_et_day = datetime.fromtimestamp((utc_end_ms - 1) / 1000.0, tz=eastern).date()

    intervals: list[tuple[int, int]] = []
    day = start_et_day
    while day <= end_et_day:
        # Fall back to direct datetime evaluation on DST transition days (2 days/year).
        day_start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=eastern)
        day_end = datetime(day.year, day.month, day.day, 23, 59, tzinfo=eastern)
        if day_start.utcoffset() != day_end.utcoffset():
            return None
        try:
            _, _, _, windows = _session_windows(day, calendar_id, trading_windows_et)
        except CalendarUnavailable:
            return None
        if windows:
            midnight_ms = int(day_start.timestamp() * 1000)
            for first_min, end_min in windows:
                s_ms = midnight_ms + first_min * 60_000
                e_ms = midnight_ms + end_min * 60_000
                if e_ms > utc_start_ms and s_ms < utc_end_ms:
                    intervals.append((s_ms, e_ms))
        day += timedelta(days=1)
    return tuple(intervals)


def is_trading_window_allowed(at_ms: int, config) -> bool:
    """Hot-path check equivalent to ``evaluate_trading_window(at_ms, config).allowed``."""
    _verified_digest(config)
    windows_key = _normalize_windows_key(config.trading_windows_et)
    intervals = _utc_day_allowed_intervals(
        at_ms // 86_400_000,
        config.trading_calendar_id,
        windows_key,
        ZoneInfo,
    )
    if intervals is not None:
        for start_ms, end_ms in intervals:
            if start_ms <= at_ms < end_ms:
                return True
        return False

    dt = datetime.fromtimestamp(at_ms / 1000.0, tz=_eastern())
    _, _, _, windows = _session_windows(dt.date(), config.trading_calendar_id, windows_key)
    if not windows:
        return False
    minute = dt.hour * 60 + dt.minute
    for start, end in windows:
        if start <= minute < end:
            return True
    return False


def evaluate_trading_window(at_ms: int, config) -> TradingWindowDecision:
    digest = _verified_digest(config)
    dt = datetime.fromtimestamp(at_ms / 1000.0, tz=_eastern())
    day = dt.date()
    session, _, _, windows = _session_windows(
        day,
        config.trading_calendar_id,
        _normalize_windows_key(config.trading_windows_et),
    )
    minute = dt.hour * 60 + dt.minute
    allowed = False
    for start, end in windows:
        if start <= minute < end:
            allowed = True
            break
    reason = (
        "allowed"
        if allowed
        else "reference_market_closed"
        if session in {"holiday", "weekend"}
        else "current_bar_outside_trading_window"
    )
    return TradingWindowDecision(
        config.trading_calendar_id,
        digest,
        at_ms,
        day.isoformat(),
        session,
        allowed,
        reason,
    )


def trading_window_schedule(at_ms: int, config) -> dict:
    """Presentation of the same intersection; never runs indicators or changes a decision."""
    decision = evaluate_trading_window(at_ms, config)
    eastern = _eastern()
    today = date.fromisoformat(decision.eastern_date)

    def stamp(day: date, minute: int) -> int:
        return int((datetime.combine(day, time.min, eastern) + timedelta(minutes=minute)).timestamp() * 1000)

    session, opening, closing, windows = _windows(today, config)
    result = {
        **decision.to_dict(),
        "today_windows": [[stamp(today, start), stamp(today, end)] for start, end in windows],
        "market_session": [stamp(today, opening), stamp(today, closing)] if opening < closing else None,
        "next_window": None,
    }
    for offset in range(370):
        day = today + timedelta(days=offset)
        try:
            _, _, _, upcoming = _windows(day, config)
        except CalendarUnavailable:
            break
        for start, end in upcoming:
            start_ms = stamp(day, start)
            if start_ms > at_ms:
                result["next_window"] = [start_ms, stamp(day, end)]
                return result
    return result
