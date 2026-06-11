from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def open_time_iso(self) -> str:
        return datetime.fromtimestamp(self.open_time_ms / 1000, tz=timezone.utc).isoformat()

    @classmethod
    def from_binance_row(cls, row: list) -> "Candle":
        return cls(
            open_time_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "Candle":
        return cls(
            open_time_ms=int(row["open_time_ms"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )

    def to_csv_row(self) -> dict[str, str]:
        return {
            "open_time_ms": str(self.open_time_ms),
            "open_time_iso": self.open_time_iso,
            "open": f"{self.open:.8f}",
            "high": f"{self.high:.8f}",
            "low": f"{self.low:.8f}",
            "close": f"{self.close:.8f}",
            "volume": f"{self.volume:.8f}",
        }


@dataclass(frozen=True)
class EntrySignal:
    allowed: bool
    reason: str
    stop_price: float | None = None


@dataclass
class Fill:
    time_ms: int
    price: float
    margin_fraction: float
    notional: float
    units: float
    fee: float


@dataclass
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    fills: list[Fill]
    pnl: float
    fees: float
    return_pct: float
    max_stage: int
    exit_reason: str

    @property
    def entry_time_iso(self) -> str:
        return datetime.fromtimestamp(self.entry_time_ms / 1000, tz=timezone.utc).isoformat()

    @property
    def exit_time_iso(self) -> str:
        return datetime.fromtimestamp(self.exit_time_ms / 1000, tz=timezone.utc).isoformat()


@dataclass
class BacktestResult:
    starting_equity: float
    ending_equity: float
    trades: list[Trade]
    equity_curve: list[tuple[int, float]] = field(default_factory=list)

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def total_return_pct(self) -> float:
        return (self.ending_equity / self.starting_equity) - 1

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for trade in self.trades if trade.pnl > 0)
        return wins / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(trade.pnl for trade in self.trades if trade.pnl > 0)
        gross_loss = -sum(trade.pnl for trade in self.trades if trade.pnl < 0)
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak:
                max_dd = min(max_dd, (equity / peak) - 1)
        return max_dd


def closes(candles: Iterable[Candle]) -> list[float]:
    return [bar.close for bar in candles]
