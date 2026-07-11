from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mu_strategy.models import (
    Candle,
    EntryDecisionCode,
    EntryDecisionStage,
    EntryDisposition,
    EntrySignal,
    entry_decision_metadata,
)


FEE_PROFILE_RATES = {
    "market": 0.0005,
    "limit": 0.0002,
}
FEE_PROFILE_LABELS = {
    "market": "market/taker (市价/吃单)",
    "limit": "limit/maker (限价挂单成本假设)",
}
FEE_PROFILE_CHOICES = tuple(FEE_PROFILE_RATES)
DEFAULT_FEE_PROFILE = "market"


@dataclass(frozen=True)
class EntryExecution:
    allowed: bool
    reason: str
    entry_price: float | None = None
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


@dataclass(frozen=True)
class StrategyConfig:
    symbol: str = "MUUSDT"
    entry_execution: str = "break_high"
    second_pullback_wait_bars: int = 8
    stop_tightening: str = "baseline"
    leverage: float = 5.0
    margin_steps: tuple[float, ...] = (0.20, 0.20, 0.20, 0.40)
    initial_stop_pct: float = 0.02
    add_thresholds: tuple[float, ...] = (0.02, 0.04, 0.06)
    rsi_floor: float = 45.0
    rsi_add_floor: float = 50.0
    fib_tolerance_pct: float = 0.002
    fee_profile: str = DEFAULT_FEE_PROFILE
    fee_rate: float | None = None
    fib_lookback: int = 32
    stop_buffer_pct: float = 0.0005
    stop_transition_bars: int = 8
    stop_transition_curve: str = "linear"
    max_entry_above_fib_pct: float | None = None
    yellow_max_entry_above_fib_pct: float | None = None
    max_signal_range_pct: float | None = None
    max_entry_above_signal_close_pct: float | None = None
    block_reverse_fib_resistance: bool = False
    reverse_fib_lookback: int = 64
    reverse_fib_tolerance_pct: float = 0.003
    yellow_stop_tightening: str | None = None
    green_stop_tightening: str | None = None
    green_wide_stop_buffer_pct: float = 0.01
    allowed_regimes: tuple[str, ...] = ("green", "yellow")
    full_size_regime: str = "green"
    trading_windows_bjt: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: (("21:45", "23:30"), ("02:30", "03:45"))
    )
    trading_windows_et: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: (("09:45", "11:30"), ("14:30", "15:45"))
    )

    def __post_init__(self) -> None:
        profile = normalize_fee_profile(self.fee_profile)
        object.__setattr__(self, "fee_profile", profile)
        if self.fee_rate is None:
            object.__setattr__(self, "fee_rate", fee_rate_for_profile(profile))


def normalize_fee_profile(fee_profile: str) -> str:
    profile = fee_profile.lower().strip()
    if profile not in FEE_PROFILE_RATES:
        raise ValueError(f"unsupported fee_profile: {fee_profile}")
    return profile


def fee_rate_for_profile(fee_profile: str) -> float:
    return FEE_PROFILE_RATES[normalize_fee_profile(fee_profile)]


def fee_profile_label(config_or_profile: StrategyConfig | str) -> str:
    profile = config_or_profile if isinstance(config_or_profile, str) else config_or_profile.fee_profile
    return FEE_PROFILE_LABELS[normalize_fee_profile(profile)]


def with_fee_profile(config: StrategyConfig, fee_profile: str) -> StrategyConfig:
    profile = normalize_fee_profile(fee_profile)
    return replace(config, fee_profile=profile, fee_rate=fee_rate_for_profile(profile))


from mu_strategy.strategies.registry import (
    StrategyGroup,
    baseline_green_wide_strategy_group,
    baseline_half_green_wide_strategy_group,
    baseline_half_protect_strategy_group,
    baseline_strategy_group,
    baseline_yellow_green_wide_strategy_group,
    baseline_yellow_wide_strategy_group,
    default_strategy_groups,
    direct_next_open_strategy_group,
    legacy_break_high_strategy_group,
    optimized_strategy_group,
    second_pullback_strategy_group,
    selected_strategy_groups,
)


def fibonacci_levels(low: float, high: float) -> dict[str, float]:
    if high <= low:
        raise ValueError("high must be greater than low")
    span = high - low
    return {
        "0.236": round(high - span * 0.236, 8),
        "0.382": round(high - span * 0.382, 8),
        "0.5": round(high - span * 0.5, 8),
        "0.618": round(high - span * 0.618, 8),
        "0.786": round(high - span * 0.786, 8),
    }


def one_hour_regime(
    close: float,
    ema21: float,
    rsi14: float,
    macd_hist: float,
    macd_hist_prev: float,
) -> str:
    if rsi14 < 45 or close < ema21 * 0.985 or (macd_hist < 0 and macd_hist < macd_hist_prev):
        return "red"
    if close > ema21 and rsi14 >= 50 and macd_hist >= macd_hist_prev:
        return "green"
    return "yellow"


def should_enter_long(
    candle: Candle,
    fib_level: float,
    regime: str,
    rsi14: float,
    macd_hist: float,
    macd_hist_prev: float,
    config: StrategyConfig,
) -> EntrySignal:
    if regime not in config.allowed_regimes:
        return EntrySignal(False, "1h regime blocks long", decision_code=EntryDecisionCode.REGIME_BLOCKED)
    if rsi14 < config.rsi_floor:
        return EntrySignal(False, "15m RSI below floor", decision_code=EntryDecisionCode.RSI_BELOW_FLOOR)
    if macd_hist < macd_hist_prev and macd_hist < 0:
        return EntrySignal(
            False,
            "15m MACD histogram still weakening",
            decision_code=EntryDecisionCode.MACD_WEAKENING,
        )

    tolerance = fib_level * config.fib_tolerance_pct
    touched = candle.low <= fib_level + tolerance
    reclaimed = candle.close >= fib_level
    if not (touched and reclaimed):
        return EntrySignal(
            False,
            "no confirmed Fibonacci retest",
            decision_code=EntryDecisionCode.NO_CONFIRMED_FIB_RETEST,
        )

    hard_stop = candle.close * (1 - config.initial_stop_pct)
    technical_stop = candle.low
    stop_price = max(hard_stop, technical_stop)
    return EntrySignal(
        True,
        "confirmed Fibonacci retest",
        stop_price,
        decision_code=EntryDecisionCode.SIGNAL_CONFIRMED,
    )


def should_execute_entry(
    candles: list[Candle],
    index: int,
    next_candle: Candle,
    fib_level: float,
    regime: str,
    config: StrategyConfig,
) -> EntryExecution:
    candle = candles[index]
    if config.entry_execution == "direct_next_open":
        entry_price = next_candle.open
    elif config.entry_execution == "break_high":
        if next_candle.high <= candle.high:
            return EntryExecution(
                False,
                "next candle does not break signal high",
                decision_code=EntryDecisionCode.NEXT_CANDLE_DID_NOT_BREAK_SIGNAL_HIGH,
            )
        entry_price = max(next_candle.open, candle.high)
    elif config.entry_execution == "second_pullback":
        return EntryExecution(
            False,
            "waiting for second pullback",
            decision_code=EntryDecisionCode.WAITING_SECOND_PULLBACK,
        )
    else:
        raise ValueError(f"unsupported entry_execution: {config.entry_execution}")

    signal_range_pct = (candle.high - candle.low) / candle.close if candle.close else 0.0
    if config.max_signal_range_pct is not None and signal_range_pct > config.max_signal_range_pct:
        return EntryExecution(
            False,
            "signal candle too wide",
            decision_code=EntryDecisionCode.SIGNAL_CANDLE_TOO_WIDE,
        )

    entry_above_fib_pct = (entry_price / fib_level) - 1 if fib_level else 0.0
    max_entry_above_fib_pct = config.max_entry_above_fib_pct
    if regime == "yellow" and config.yellow_max_entry_above_fib_pct is not None:
        max_entry_above_fib_pct = config.yellow_max_entry_above_fib_pct
    if max_entry_above_fib_pct is not None and entry_above_fib_pct > max_entry_above_fib_pct:
        return EntryExecution(
            False,
            "entry too far above Fibonacci retest",
            decision_code=EntryDecisionCode.ENTRY_TOO_FAR_ABOVE_FIB,
        )

    entry_above_signal_close_pct = (entry_price / candle.close) - 1 if candle.close else 0.0
    if (
        config.max_entry_above_signal_close_pct is not None
        and entry_above_signal_close_pct > config.max_entry_above_signal_close_pct
    ):
        return EntryExecution(
            False,
            "entry too far above signal close",
            decision_code=EntryDecisionCode.ENTRY_TOO_FAR_ABOVE_SIGNAL_CLOSE,
        )

    reverse_level = reverse_fibonacci_resistance_level(candles, index, entry_price, config)
    if config.block_reverse_fib_resistance and reverse_level is not None:
        return EntryExecution(
            False,
            f"entry at reverse Fibonacci resistance {reverse_level:.6f}",
            decision_code=EntryDecisionCode.REVERSE_FIB_RESISTANCE,
        )

    return EntryExecution(
        True,
        "execution accepted",
        entry_price,
        decision_code=EntryDecisionCode.EXECUTION_ACCEPTED,
    )


def recent_swing(candles: list[Candle], end_index: int, lookback: int) -> tuple[float, float] | None:
    if end_index <= 0:
        return None
    start = max(0, end_index - lookback)
    sample = candles[start:end_index]
    if len(sample) < 2:
        return None
    low = min(bar.low for bar in sample)
    high = max(bar.high for bar in sample)
    if high <= low:
        return None
    return low, high


def nearest_fib_retest_level(candles: list[Candle], index: int, config: StrategyConfig) -> float | None:
    swing = recent_swing(candles, index, config.fib_lookback)
    if swing is None:
        return None
    low, high = swing
    levels = fibonacci_levels(low, high)
    candle = candles[index]
    candidates = [levels["0.382"], levels["0.5"], levels["0.618"]]
    viable = [level for level in candidates if candle.low <= level * (1 + config.fib_tolerance_pct) <= candle.high]
    if not viable:
        return None
    return min(viable, key=lambda level: abs(candle.close - level))


def reverse_fibonacci_resistance_level(
    candles: list[Candle],
    index: int,
    entry_price: float,
    config: StrategyConfig,
) -> float | None:
    if not config.block_reverse_fib_resistance:
        return None
    swing = recent_down_swing(candles, index, config.reverse_fib_lookback)
    if swing is None:
        return None
    high, low = swing
    span = high - low
    levels = [low + span * ratio for ratio in (0.382, 0.5, 0.618)]
    tolerance = entry_price * config.reverse_fib_tolerance_pct
    candidates = [level for level in levels if abs(entry_price - level) <= tolerance]
    if not candidates:
        return None
    return min(candidates, key=lambda level: abs(entry_price - level))


def recent_down_swing(candles: list[Candle], end_index: int, lookback: int) -> tuple[float, float] | None:
    if end_index <= 0:
        return None
    start = max(0, end_index - lookback)
    sample = candles[start:end_index]
    if len(sample) < 2:
        return None
    high_index, high_candle = max(enumerate(sample), key=lambda item: item[1].high)
    low_index, low_candle = min(enumerate(sample), key=lambda item: item[1].low)
    if high_index >= low_index:
        return None
    if high_candle.high <= low_candle.low:
        return None
    return high_candle.high, low_candle.low


def recent_higher_low(candles: list[Candle], index: int, lookback: int = 8) -> float | None:
    start = max(0, index - lookback)
    sample = candles[start:index]
    if len(sample) < 3:
        return None
    return min(bar.low for bar in sample[-4:])


def is_preferred_us_cash_window(open_time_ms: int, config: StrategyConfig) -> bool:
    try:
        eastern = ZoneInfo("America/New_York")
    except Exception:
        eastern = timezone.utc
    dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).astimezone(eastern)
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    for start, end in config.trading_windows_et:
        start_minutes = _parse_hhmm(start)
        end_minutes = _parse_hhmm(end)
        if start_minutes <= minutes <= end_minutes:
            return True
    return False


def _parse_hhmm(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)
