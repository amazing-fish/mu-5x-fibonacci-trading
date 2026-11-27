from __future__ import annotations

from mu_strategy.market_data.trusted_data.compat import CandleBundle
from mu_strategy.market_data.trusted_data.load import (
    TRUSTED_CONSUMER_REFRESH_ERROR,
    load_trusted_candle_bundle,
)

# Backwards-compatible alias kept for cache-only consumers (cli, viz, lab).
refresh_trusted_candle_bundle = load_trusted_candle_bundle

__all__ = [
    "CandleBundle",
    "TRUSTED_CONSUMER_REFRESH_ERROR",
    "load_trusted_candle_bundle",
    "refresh_trusted_candle_bundle",
]
