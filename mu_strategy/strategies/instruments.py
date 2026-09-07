"""Explicit reference-calendar selection, independent of refresh universe membership."""
from functools import lru_cache
import json
from pathlib import Path

from mu_strategy.core.trading_calendar import CALENDAR_IDS
from mu_strategy.market_data.symbols import resolve_okx_swap_symbol


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "instrument_calendars.json"


def read_instrument_calendars(path=DEFAULT_CONFIG):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate instrument calendar field")
            result[key] = value
        return result
    payload = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "default_calendar", "symbols"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["default_calendar"] not in CALENDAR_IDS or not isinstance(payload["symbols"], dict)):
        raise ValueError("invalid instrument calendar configuration")
    for symbol, calendar_id in payload["symbols"].items():
        if (not symbol or resolve_okx_swap_symbol(symbol).inst_id != symbol or calendar_id not in CALENDAR_IDS):
            raise ValueError("instrument calendar requires canonical symbols and supported calendar IDs")
    return payload


@lru_cache(maxsize=1)
def _configured_calendars():
    return read_instrument_calendars()


def instrument_calendar(symbol):
    payload = _configured_calendars()
    return payload["symbols"].get(resolve_okx_swap_symbol(symbol).inst_id, payload["default_calendar"])
