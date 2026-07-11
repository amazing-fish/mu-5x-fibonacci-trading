from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, unique
from types import MappingProxyType
from typing import Iterable, Mapping


@unique
class EntryDisposition(str, Enum):
    READY = "ready"
    WAIT = "wait"
    BLOCK = "block"
    UNKNOWN = "unknown"


@unique
class EntryDecisionStage(str, Enum):
    INPUT = "input"
    SIGNAL = "signal"
    PENDING_ENTRY = "pending_entry"
    EXECUTION = "execution"
    UNKNOWN = "unknown"


@unique
class EntryDecisionCode(str, Enum):
    UNKNOWN = "unknown"
    MARKET_DATA_UNAVAILABLE = "market_data_unavailable"
    NO_CANDLES = "no_candles"
    CURRENT_BAR_OUTSIDE_TRADING_WINDOW = "current_bar_outside_trading_window"
    REGIME_BLOCKED = "regime_blocked"
    RSI_BELOW_FLOOR = "rsi_below_floor"
    MACD_WEAKENING = "macd_weakening"
    NO_CONFIRMED_FIB_RETEST = "no_confirmed_fib_retest"
    NO_RECENT_CONFIRMED_FIB_RETEST = "no_recent_confirmed_fib_retest"
    SIGNAL_CONFIRMED = "signal_confirmed"
    WAITING_SECOND_PULLBACK = "waiting_second_pullback"
    SECOND_PULLBACK_LIMIT_READY = "second_pullback_limit_ready"
    PRICE_AWAY_FROM_FIB = "price_away_from_fib"
    NEXT_CANDLE_REQUIRED = "next_candle_required"
    NEXT_FILL_OUTSIDE_TRADING_WINDOW = "next_fill_outside_trading_window"
    NEXT_CANDLE_DID_NOT_BREAK_SIGNAL_HIGH = "next_candle_did_not_break_signal_high"
    EXECUTION_PRICE_UNAVAILABLE = "execution_price_unavailable"
    SIGNAL_CANDLE_TOO_WIDE = "signal_candle_too_wide"
    ENTRY_TOO_FAR_ABOVE_FIB = "entry_too_far_above_fib"
    ENTRY_TOO_FAR_ABOVE_SIGNAL_CLOSE = "entry_too_far_above_signal_close"
    REVERSE_FIB_RESISTANCE = "reverse_fib_resistance"
    EXECUTION_ACCEPTED = "execution_accepted"


@dataclass(frozen=True)
class EntryDecisionMetadata:
    disposition: EntryDisposition
    stage: EntryDecisionStage


ENTRY_DECISION_CATALOG: Mapping[EntryDecisionCode, EntryDecisionMetadata] = MappingProxyType(
    {
        EntryDecisionCode.UNKNOWN: EntryDecisionMetadata(
            EntryDisposition.UNKNOWN,
            EntryDecisionStage.UNKNOWN,
        ),
        EntryDecisionCode.MARKET_DATA_UNAVAILABLE: EntryDecisionMetadata(
            EntryDisposition.BLOCK,
            EntryDecisionStage.INPUT,
        ),
        EntryDecisionCode.NO_CANDLES: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.INPUT,
        ),
        EntryDecisionCode.CURRENT_BAR_OUTSIDE_TRADING_WINDOW: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.INPUT,
        ),
        EntryDecisionCode.REGIME_BLOCKED: EntryDecisionMetadata(
            EntryDisposition.BLOCK,
            EntryDecisionStage.SIGNAL,
        ),
        EntryDecisionCode.RSI_BELOW_FLOOR: EntryDecisionMetadata(
            EntryDisposition.BLOCK,
            EntryDecisionStage.SIGNAL,
        ),
        EntryDecisionCode.MACD_WEAKENING: EntryDecisionMetadata(
            EntryDisposition.BLOCK,
            EntryDecisionStage.SIGNAL,
        ),
        EntryDecisionCode.NO_CONFIRMED_FIB_RETEST: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.SIGNAL,
        ),
        EntryDecisionCode.NO_RECENT_CONFIRMED_FIB_RETEST: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.SIGNAL,
        ),
        EntryDecisionCode.SIGNAL_CONFIRMED: EntryDecisionMetadata(
            EntryDisposition.READY,
            EntryDecisionStage.SIGNAL,
        ),
        EntryDecisionCode.WAITING_SECOND_PULLBACK: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.PENDING_ENTRY,
        ),
        EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY: EntryDecisionMetadata(
            EntryDisposition.READY,
            EntryDecisionStage.PENDING_ENTRY,
        ),
        EntryDecisionCode.PRICE_AWAY_FROM_FIB: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.PENDING_ENTRY,
        ),
        EntryDecisionCode.NEXT_CANDLE_REQUIRED: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.EXECUTION,
        ),
        EntryDecisionCode.NEXT_FILL_OUTSIDE_TRADING_WINDOW: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.EXECUTION,
        ),
        EntryDecisionCode.NEXT_CANDLE_DID_NOT_BREAK_SIGNAL_HIGH: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.EXECUTION,
        ),
        EntryDecisionCode.EXECUTION_PRICE_UNAVAILABLE: EntryDecisionMetadata(
            EntryDisposition.WAIT,
            EntryDecisionStage.EXECUTION,
        ),
        EntryDecisionCode.SIGNAL_CANDLE_TOO_WIDE: EntryDecisionMetadata(
            EntryDisposition.BLOCK,
            EntryDecisionStage.EXECUTION,
        ),
        EntryDecisionCode.ENTRY_TOO_FAR_ABOVE_FIB: EntryDecisionMetadata(
            EntryDisposition.BLOCK,
            EntryDecisionStage.EXECUTION,
        ),
        EntryDecisionCode.ENTRY_TOO_FAR_ABOVE_SIGNAL_CLOSE: EntryDecisionMetadata(
            EntryDisposition.BLOCK,
            EntryDecisionStage.EXECUTION,
        ),
        EntryDecisionCode.REVERSE_FIB_RESISTANCE: EntryDecisionMetadata(
            EntryDisposition.BLOCK,
            EntryDecisionStage.EXECUTION,
        ),
        EntryDecisionCode.EXECUTION_ACCEPTED: EntryDecisionMetadata(
            EntryDisposition.READY,
            EntryDecisionStage.EXECUTION,
        ),
    }
)


_SCANNER_ACTIONS = {
    EntryDisposition.READY: "enter",
    EntryDisposition.WAIT: "wait",
    EntryDisposition.BLOCK: "skip",
}
_EXECUTION_ACTIONS = {
    EntryDisposition.READY: "allow",
    EntryDisposition.WAIT: "wait",
    EntryDisposition.BLOCK: "block",
}


def entry_decision_metadata(code: EntryDecisionCode) -> EntryDecisionMetadata:
    if not isinstance(code, EntryDecisionCode):
        raise TypeError("code must be an EntryDecisionCode")
    return ENTRY_DECISION_CATALOG[code]


def scanner_action_for(disposition: EntryDisposition) -> str:
    if not isinstance(disposition, EntryDisposition):
        raise TypeError("disposition must be an EntryDisposition")
    try:
        return _SCANNER_ACTIONS[disposition]
    except KeyError as exc:
        raise ValueError(f"scanner action is undefined for {disposition.name}") from exc


def execution_action_for(disposition: EntryDisposition) -> str:
    if not isinstance(disposition, EntryDisposition):
        raise TypeError("disposition must be an EntryDisposition")
    try:
        return _EXECUTION_ACTIONS[disposition]
    except KeyError as exc:
        raise ValueError(f"execution action is undefined for {disposition.name}") from exc


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
    decision_code: EntryDecisionCode = EntryDecisionCode.UNKNOWN

    def __post_init__(self) -> None:
        if self.decision_code is not EntryDecisionCode.UNKNOWN:
            object.__setattr__(self, "allowed", self.disposition is EntryDisposition.READY)

    @property
    def disposition(self) -> EntryDisposition:
        return entry_decision_metadata(self.decision_code).disposition

    @property
    def stage(self) -> EntryDecisionStage:
        return entry_decision_metadata(self.decision_code).stage


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
