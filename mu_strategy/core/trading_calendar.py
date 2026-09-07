"""Offline reference-market sessions; strategy windows remain a separate constraint."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from mu_strategy.canonical import canonical_sha256


LEGACY_CALENDAR = "weekday_windows_v1"
US_EQUITIES_CALENDAR = "us_equities_2025_2028_v1"
CONTINUOUS_CALENDAR = "continuous_v1"
CALENDAR_IDS = (LEGACY_CALENDAR, US_EQUITIES_CALENDAR, CONTINUOUS_CALENDAR)
# NYSE official cash-equity schedules, plus the exceptional Carter closure.
# Sources and update/version contract: docs/trading-calendar.md.
_HOLIDAYS = {
    2025: "01-01 01-09 01-20 02-17 04-18 05-26 06-19 07-04 09-01 11-27 12-25",
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25",
    2027: "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24",
    2028: "01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25",
}
_EARLY_CLOSES = {2025: "07-03 11-28 12-24", 2026: "11-27 12-24", 2027: "11-26", 2028: "07-03 11-24"}


class CalendarUnavailable(ValueError):
    pass


@lru_cache(maxsize=3)
def calendar_sha256(calendar_id: str) -> str:
    if calendar_id not in CALENDAR_IDS:
        raise CalendarUnavailable(f"unsupported reference calendar: {calendar_id}")
    payload = {"id": calendar_id, "timezone": "America/New_York", "window_end": "inclusive_minute",
               "weekends_closed": calendar_id != CONTINUOUS_CALENDAR}
    if calendar_id == US_EQUITIES_CALENDAR:
        payload.update(holidays=_HOLIDAYS, early_closes=_EARLY_CLOSES, coverage=[2025, 2028],
                       cash_open="09:30", cash_close="16:00", early_close="13:00", cash_end="exclusive")
    return canonical_sha256(payload)


def _eastern():
    try:
        return ZoneInfo("America/New_York")
    except Exception as exc:
        raise CalendarUnavailable("America/New_York timezone unavailable") from exc


def _minute(value: str) -> int:
    try:
        hour, minute = map(int, value.split(":"))
        if len(value) != 5 or not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError()
        return hour * 60 + minute
    except (TypeError, ValueError, AttributeError) as exc:
        raise CalendarUnavailable("invalid strategy trading window") from exc


def _session(day: date, calendar_id: str) -> tuple[str, int, int]:
    if calendar_id == US_EQUITIES_CALENDAR and day.year not in _HOLIDAYS:
        raise CalendarUnavailable("reference calendar covers 2025 through 2028 only")
    if calendar_id != CONTINUOUS_CALENDAR and day.weekday() >= 5:
        return "weekend", 0, 0
    if calendar_id != US_EQUITIES_CALENDAR:
        return "open", 0, 1440
    key = day.strftime("%m-%d")
    if key in _HOLIDAYS[day.year].split():
        return "holiday", 0, 0
    early = key in _EARLY_CLOSES[day.year].split()
    return "early_close" if early else "open", 570, 780 if early else 960


def _windows(day, config):
    state, opening, closing = _session(day, config.trading_calendar_id)
    windows = []
    for start, end in config.trading_windows_et:
        first, last = _minute(start), _minute(end)
        if first > last:
            raise CalendarUnavailable("strategy window must not cross the ET date boundary")
        # Preserve the existing inclusive minute; the cash-market close is exclusive.
        first, stop = max(first, opening), min(last + 1, closing)
        if first < stop:
            windows.append((first, stop))
    return state, opening, closing, sorted(windows)


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


def evaluate_trading_window(at_ms: int, config) -> TradingWindowDecision:
    digest = calendar_sha256(config.trading_calendar_id)
    if config.trading_calendar_sha256 != digest:
        raise CalendarUnavailable("reference calendar content does not match frozen configuration")
    dt = datetime.fromtimestamp(at_ms / 1000, timezone.utc).astimezone(_eastern())
    session, _, _, windows = _windows(dt.date(), config)
    minute = dt.hour * 60 + dt.minute
    allowed = any(start <= minute < end for start, end in windows)
    reason = "allowed" if allowed else "reference_market_closed" if session in {"holiday", "weekend"} else "current_bar_outside_trading_window"
    return TradingWindowDecision(config.trading_calendar_id, digest, at_ms, dt.date().isoformat(), session, allowed, reason)


def trading_window_schedule(at_ms: int, config) -> dict:
    """Presentation of the same intersection; never runs indicators or changes a decision."""
    decision = evaluate_trading_window(at_ms, config)
    eastern = _eastern()
    today = date.fromisoformat(decision.eastern_date)
    def stamp(day, minute):
        return int((datetime.combine(day, time.min, eastern) + timedelta(minutes=minute)).timestamp() * 1000)
    session, opening, closing, windows = _windows(today, config)
    result = {**decision.to_dict(), "today_windows": [[stamp(today, start), stamp(today, end)] for start, end in windows],
              "market_session": [stamp(today, opening), stamp(today, closing)] if opening < closing else None,
              "next_window": None}
    for offset in range(370):
        day = today + timedelta(days=offset)
        try:
            _, _, _, upcoming = _windows(day, config)
        except CalendarUnavailable:
            break  # no prediction outside the published coverage
        for start, end in upcoming:
            if stamp(day, start) > at_ms:
                result["next_window"] = [stamp(day, start), stamp(day, end)]
                return result
    return result
