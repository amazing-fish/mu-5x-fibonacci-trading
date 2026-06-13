from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyComponents:
    entry: str = "突破前高确认"
    position: str = "5x 金字塔 20/20/20/40"
    exit: str = "baseline 抬止损"
    filters: tuple[str, ...] = ("1h regime", "15m RSI/MACD", "美股现金盘窗口")
