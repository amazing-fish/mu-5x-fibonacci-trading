"""Documented contract-basis boundaries for trusted refresh inputs."""

from __future__ import annotations

from datetime import datetime, timezone

from mu_strategy.market_data.utils import interval_to_ms
from mu_strategy.models import Candle


SPCX_REBASE_MS = int(datetime(2026, 6, 2, 7, 10, tzinfo=timezone.utc).timestamp() * 1000)
SPCX_REBASE_SOURCE = (
    "https://www.okx.com/zh-hans/help/"
    "okx-to-execute-rebase-on-spacexusdt-pre-market-perpetual-futures-and-rename"
)


def select_consistent_contract_basis(
    symbol: str,
    interval: str,
    candles: list[Candle],
) -> tuple[list[Candle], tuple[str, ...]]:
    """Keep only complete SPCX candles in the post-rebase price/contract unit."""
    if symbol != "SPCX-USDT-SWAP" or not candles:
        return candles, ()
    step_ms = interval_to_ms(interval)
    first_post_rebase_ms = ((SPCX_REBASE_MS + step_ms - 1) // step_ms) * step_ms
    selected = [candle for candle in candles if candle.open_time_ms >= first_post_rebase_ms]
    if min(candle.open_time_ms for candle in candles) > first_post_rebase_ms:
        return selected, ()
    return selected, (
        f"contract_basis_start:effective_ms={SPCX_REBASE_MS}:"
        f"first_complete_open_ms={first_post_rebase_ms}:source={SPCX_REBASE_SOURCE}",
    )
