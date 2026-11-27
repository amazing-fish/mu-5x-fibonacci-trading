"""Phase 2 decoupled walk-forward strategy discovery engine over the Phase 1 Simulator."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
import math
import time
from types import MappingProxyType
from typing import Any, Literal

from mu_strategy.core.instrument import CostModel, InstrumentSpec, spot_spec
from mu_strategy.engine.metrics import (
    MS_PER_YEAR,
    CostAttribution,
    SimulationResult,
    _compute_curve_stats,
    compute_buy_and_hold_benchmark,
    compute_simulation_metrics,
)
from mu_strategy.engine.risk_overlay import DrawdownCircuitBreaker, RiskOverlay, VolTargetOverlay
from mu_strategy.engine.simulator import Simulator, SimulatorConfig
from mu_strategy.lab.gates import (
    GateConfig,
    SixGateEvaluation,
    compute_null_distribution_pvalue,
    evaluate_six_gates,
)
from mu_strategy.market_data.bar_series import BarSeries
from mu_strategy.market_data.funding import FundingSeries
from mu_strategy.strategies.families import (
    DonchianATRParams,
    DonchianATRStrategy,
    FibonacciPullbackParams,
    FibonacciPullbackStrategy,
    MeanReversionParams,
    MeanReversionStrategy,
    TSMomentumParams,
    TSMomentumStrategy,
)
from mu_strategy.strategies.features import FeatureCache
from mu_strategy.strategies.protocol import ParamSpace, ParamSpec, Strategy

__all__ = [
    "BenchmarkSummary",
    "CandidateFoldResult",
    "CandidateResult",
    "DiscoveryConfig",
    "DiscoveryReport",
    "StrategyCatalogEntry",
    "StressScenarioResult",
    "WalkForwardFold",
    "build_walk_forward_folds",
    "default_strategy_catalog",
    "discover_strategies",
    "export_winner_recipe",
    "overlay_presets",
]

_MS_PER_DAY = 86_400_000
_VERDICT_RANK = {"GO": 0, "CONDITIONAL": 1, "NO_GO": 2}
_STRESS_SCENARIO_IDS = ("slippage_10bp", "funding_0pct_apr", "funding_15pct_apr", "funding_30pct_apr")
_FUNDING_STRESS_SPECS = (
    ("funding_0pct_apr", "Funding 0% APR", 0.0),
    ("funding_15pct_apr", "Funding +15% APR", 0.15),
    ("funding_30pct_apr", "Funding +30% APR", 0.30),
)
_PROVENANCE_KEYS = (
    "dataset_sha256",
    "funding_sha256",
    "code_revision",
    "fee_profile",
    "data_source",
    "generation_id",
    "days",
)


def _jnum(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    r = round(float(value), digits)
    return 0.0 if r == 0.0 else r


def _attr_dict(attr: CostAttribution) -> dict[str, float | None]:
    return {
        "gross_pnl": _jnum(attr.gross_pnl, 2),
        "fees": _jnum(attr.fees, 2),
        "slippage": _jnum(attr.slippage, 2),
        "funding_pnl": _jnum(attr.funding_pnl, 2),
        "net_pnl": _jnum(attr.net_pnl, 2),
    }


def _downsample_daily_curve(curve: Sequence[tuple[int, float]]) -> tuple[tuple[int, float], ...]:
    """Keep first point, UTC day-end points, and final point for compact SVG rendering."""
    if len(curve) <= 2:
        return tuple((int(ts), round(float(eq), 2)) for ts, eq in curve)
    out: list[tuple[int, float]] = [(int(curve[0][0]), round(float(curve[0][1]), 2))]
    last_day = int(curve[0][0]) // _MS_PER_DAY
    pending: tuple[int, float] | None = None
    for ts, eq in curve[1:]:
        day = int(ts) // _MS_PER_DAY
        pt = (int(ts), round(float(eq), 2))
        if day != last_day:
            if pending is not None and pending[0] != out[-1][0]:
                out.append(pending)
            last_day = day
        pending = pt
    if pending is not None and pending[0] != out[-1][0]:
        out.append(pending)
    return tuple(out)


def _return_moments(curve: Sequence[tuple[int, float]]) -> tuple[int, float, float]:
    """Compute (n_obs, skewness, excess_kurtosis) of bar-to-bar equity returns."""
    if len(curve) < 4:
        return max(0, len(curve) - 1), 0.0, 0.0
    rets: list[float] = []
    prev_eq = curve[0][1]
    for _, eq in curve[1:]:
        rets.append(((eq - prev_eq) / prev_eq) if prev_eq > 0.0 else 0.0)
        prev_eq = eq
    n = len(rets)
    mean_r = math.fsum(rets) / n
    m2 = math.fsum((r - mean_r) ** 2 for r in rets) / n
    if m2 <= 1e-24:
        return n, 0.0, 0.0
    std = math.sqrt(m2)
    m3 = math.fsum((r - mean_r) ** 3 for r in rets) / n
    m4 = math.fsum((r - mean_r) ** 4 for r in rets) / n
    skew = m3 / (std * std * std)
    kurt = (m4 / (m2 * m2)) - 3.0
    return n, (skew if math.isfinite(skew) else 0.0), (kurt if math.isfinite(kurt) else 0.0)


def _slice_bar_series(series: BarSeries, start: int, end: int) -> BarSeries:
    return BarSeries(
        symbol=series.symbol,
        interval=series.interval,
        interval_ms=series.interval_ms,
        open_times=series.open_times[start:end],
        close_times=None if series.is_uniform_close_times else series.close_times[start:end],
        opens=series.opens[start:end],
        highs=series.highs[start:end],
        lows=series.lows[start:end],
        closes=series.closes[start:end],
        volumes=series.volumes[start:end],
    )


@dataclass(frozen=True, slots=True)
class StressScenarioResult:
    """Per-scenario execution or funding stress outcome (or explicit N/A for spot)."""

    scenario_id: str
    label: str
    available: bool
    passed: bool | None
    total_return: float | None
    liquidations: int | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "label": self.label,
            "available": self.available,
            "passed": self.passed,
            "total_return": _jnum(self.total_return, 6),
            "liquidations": self.liquidations,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class StrategyCatalogEntry:
    """Decoupled strategy family registration for Phase 2 walk-forward discovery."""

    family_id: str
    display_name: str
    factory: Callable[[Mapping[str, Any]], Strategy]
    param_space: ParamSpace
    description: str = ""
    fixed_params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.fixed_params, MappingProxyType):
            object.__setattr__(self, "fixed_params", MappingProxyType(dict(self.fixed_params)))


def default_strategy_catalog(
    *,
    symbol: str = "MU-USDT-SWAP",
    interval: str = "1h",
    allow_short: bool = True,
) -> tuple[StrategyCatalogEntry, ...]:
    """Return the 4 Phase 1 strategy families with compact 20-point grids and explicit fixed_params."""
    side: Literal["buy", "long_short"] = "long_short" if allow_short else "buy"
    ts_fixed: dict[str, Any] = {"interval": interval, "side_mode": side, "target_weight": 1.0}
    don_fixed: dict[str, Any] = {
        "interval": interval,
        "exit_period": 12,
        "atr_period": 14,
        "side_mode": side,
        "target_weight": 1.0,
    }
    mr_fixed: dict[str, Any] = {
        "interval": interval,
        "rsi_period": 14,
        "exit_z": 0.25,
        "rsi_oversold": 35.0,
        "rsi_overbought": 65.0,
        "max_ann_vol": 1.5,
        "stop_atr_mult": 3.0,
        "side_mode": side,
        "target_weight": 1.0,
    }
    fib_fixed: dict[str, Any] = {
        "symbol": symbol,
        "primary_interval": interval,
        "regime_interval": interval,
        "entry_execution": "second_pullback",
        "second_pullback_wait_bars": 8,
        "initial_stop_pct": 0.03,
        "min_stop_pct": 0.008,
        "leverage": 2.0,
        "use_session_window": False,
    }
    return (
        StrategyCatalogEntry(
            "ts_momentum",
            "Time-Series Momentum",
            lambda p, fp=ts_fixed: TSMomentumStrategy(TSMomentumParams(**{**fp, **p})),
            ParamSpace((
                ParamSpec("lookback_bars", "int", 96, choices=(48, 96, 168)),
                ParamSpec("deadband_return", "float", 0.0, choices=(0.0, 0.02)),
            )),
            "Trailing return momentum with neutral deadband (6 grid points).",
            fixed_params=ts_fixed,
        ),
        StrategyCatalogEntry(
            "donchian_atr",
            "Donchian Breakout + ATR Stop",
            lambda p, fp=don_fixed: DonchianATRStrategy(DonchianATRParams(**{**fp, **p})),
            ParamSpace((
                ParamSpec("entry_period", "int", 48, choices=(24, 48, 72)),
                ParamSpec("atr_stop_mult", "float", 3.5, choices=(2.0, 3.5)),
            )),
            "Donchian channel breakout with monotone ATR trailing stop (6 grid points).",
            fixed_params=don_fixed,
        ),
        StrategyCatalogEntry(
            "mean_reversion",
            "RSI + Z-Score Mean Reversion",
            lambda p, fp=mr_fixed: MeanReversionStrategy(MeanReversionParams(**{**fp, **p})),
            ParamSpace((
                ParamSpec("zscore_period", "int", 20, choices=(20, 40)),
                ParamSpec("entry_z", "float", 2.5, choices=(1.75, 2.5)),
            )),
            "RSI + rolling Z-score mean reversion with ATR stop (4 grid points).",
            fixed_params=mr_fixed,
        ),
        StrategyCatalogEntry(
            "fibonacci_pullback",
            "Fibonacci Pullback (Multi-Stage)",
            lambda p, fp=fib_fixed: FibonacciPullbackStrategy(FibonacciPullbackParams(**{**fp, **p})),
            ParamSpace((
                ParamSpec("fib_lookback", "int", 24, choices=(24, 48)),
                ParamSpec("atr_stop_multiplier", "float", 1.0, choices=(1.0, 2.0)),
            )),
            "Multi-stage Fibonacci pullback with ATR stop floor (4 grid points).",
            fixed_params=fib_fixed,
        ),
    )


def overlay_presets() -> dict[str, tuple[RiskOverlay, ...]]:
    """Return fresh instances of standard Phase 2 composable risk-overlay presets."""
    return {
        "none": (),
        "dd20_vol25": (
            VolTargetOverlay(target_ann_vol=0.25, vol_lookback=20, max_leverage=2.0, rebalance_tolerance=0.25),
            DrawdownCircuitBreaker(max_drawdown_pct=0.20, action="flatten_and_halt", cooldown_bars=48),
        ),
        "dd15_vol25": (
            VolTargetOverlay(target_ann_vol=0.25, vol_lookback=20, max_leverage=2.0, rebalance_tolerance=0.25),
            DrawdownCircuitBreaker(max_drawdown_pct=0.15, action="flatten_and_halt", cooldown_bars=72),
        ),
    }


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """Strictly causal train/purge/test index range for a single walk-forward fold."""

    fold_index: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int

    def to_dict(self) -> dict[str, int]:
        return {"fold_index": self.fold_index, "is_start": self.is_start, "is_end": self.is_end, "oos_start": self.oos_start, "oos_end": self.oos_end}


def build_walk_forward_folds(
    n_bars: int,
    n_folds: int = 3,
    is_ratio: float = 0.5,
    purge_bars: int = 24,
    mode: Literal["anchored", "rolling"] = "anchored",
) -> tuple[WalkForwardFold, ...]:
    """Partition ``n_bars`` into strictly causal, purged, non-overlapping walk-forward folds."""
    if isinstance(n_bars, bool) or not isinstance(n_bars, int) or n_bars < 4:
        raise ValueError(f"n_bars must be an int >= 4, got {n_bars!r}")
    if isinstance(n_folds, bool) or not isinstance(n_folds, int) or n_folds < 1:
        raise ValueError(f"n_folds must be an int >= 1, got {n_folds!r}")
    if isinstance(is_ratio, bool) or not isinstance(is_ratio, (int, float)) or not math.isfinite(is_ratio) or not (0.1 <= float(is_ratio) <= 0.9):
        raise ValueError(f"is_ratio must be in [0.1, 0.9], got {is_ratio!r}")
    if isinstance(purge_bars, bool) or not isinstance(purge_bars, int) or purge_bars < 0:
        raise ValueError(f"purge_bars must be a non-negative int, got {purge_bars!r}")
    if mode not in ("anchored", "rolling"):
        raise ValueError(f"mode must be 'anchored' or 'rolling', got {mode!r}")

    first_oos_start = int(round(n_bars * float(is_ratio)))
    if first_oos_start <= purge_bars + 1:
        raise ValueError(f"insufficient IS bars ({first_oos_start}) for purge_bars={purge_bars}")
    oos_total = n_bars - first_oos_start
    if oos_total < n_folds:
        raise ValueError(f"insufficient OOS bars ({oos_total}) for n_folds={n_folds}")

    rolling_is_len = first_oos_start - purge_bars
    folds: list[WalkForwardFold] = []
    for k in range(n_folds):
        oos_start = first_oos_start + ((k * oos_total) // n_folds)
        oos_end = first_oos_start + (((k + 1) * oos_total) // n_folds)
        is_end = oos_start - purge_bars
        is_start = 0 if mode == "anchored" else max(0, is_end - rolling_is_len)
        if not (0 <= is_start < is_end and is_end + purge_bars <= oos_start < oos_end <= n_bars):
            raise ValueError(f"degenerate walk-forward fold {k} for n_bars={n_bars}")
        folds.append(WalkForwardFold(k, is_start, is_end, oos_start, oos_end))
    return tuple(folds)


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    """Configuration for a deterministic Phase 2 walk-forward strategy discovery run."""

    n_folds: int = 3
    is_ratio: float = 0.5
    purge_bars: int = 24
    walk_forward_mode: Literal["anchored", "rolling"] = "anchored"
    starting_equity: float = 10_000.0
    overlay_names: tuple[str, ...] = ("none", "dd20_vol25")
    family_ids: tuple[str, ...] | None = None
    stress_apr: float = 0.20
    top_curves: int = 10
    gate_config: GateConfig = GateConfig()

    def __post_init__(self) -> None:
        if isinstance(self.starting_equity, bool) or not isinstance(self.starting_equity, (int, float)) or not math.isfinite(self.starting_equity) or self.starting_equity <= 0.0:
            raise ValueError("starting_equity must be positive finite")
        if isinstance(self.stress_apr, bool) or not isinstance(self.stress_apr, (int, float)) or not math.isfinite(self.stress_apr) or self.stress_apr <= 0.0:
            raise ValueError("stress_apr must be positive finite")
        if isinstance(self.top_curves, bool) or not isinstance(self.top_curves, int) or self.top_curves < 0:
            raise ValueError("top_curves must be a non-negative int")
        if not self.overlay_names:
            raise ValueError("overlay_names must not be empty")
        known = overlay_presets()
        for name in self.overlay_names:
            if name not in known:
                raise ValueError(f"unknown overlay preset {name!r}; expected one of {sorted(known)}")


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    """OOS buy-and-hold benchmark metrics and daily-downsampled equity curve."""

    label: str
    starting_equity: float
    ending_equity: float
    total_return: float
    max_drawdown: float
    sharpe: float
    sortino: float
    calmar: float
    attribution: CostAttribution
    curve: tuple[tuple[int, float], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "starting_equity": _jnum(self.starting_equity, 2),
            "ending_equity": _jnum(self.ending_equity, 2),
            "total_return": _jnum(self.total_return, 6),
            "max_drawdown": _jnum(self.max_drawdown, 6),
            "sharpe": _jnum(self.sharpe, 4),
            "sortino": _jnum(self.sortino, 4),
            "calmar": _jnum(self.calmar, 4),
            "attribution": _attr_dict(self.attribution),
            "curve": [[ts, eq] for ts, eq in self.curve],
        }


@dataclass(frozen=True, slots=True)
class CandidateFoldResult:
    """In-sample and out-of-sample metrics for a single walk-forward fold."""

    fold: WalkForwardFold
    is_sharpe: float
    is_total_return: float
    oos_sharpe: float
    oos_total_return: float
    oos_max_drawdown: float
    oos_trades: int
    liquidations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.fold.to_dict(),
            "is_sharpe": _jnum(self.is_sharpe, 4),
            "is_total_return": _jnum(self.is_total_return, 6),
            "oos_sharpe": _jnum(self.oos_sharpe, 4),
            "oos_total_return": _jnum(self.oos_total_return, 6),
            "oos_max_drawdown": _jnum(self.oos_max_drawdown, 6),
            "oos_trades": self.oos_trades,
            "liquidations": self.liquidations,
        }


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Complete OOS walk-forward evaluation, attribution, stress matrix, and 6-Gate verdict."""

    candidate_id: str
    family_id: str
    display_name: str
    overlay_preset: str
    params: dict[str, Any]
    starting_equity: float
    ending_equity: float
    oos_total_return: float
    oos_max_drawdown: float
    oos_sharpe: float
    oos_sortino: float
    oos_calmar: float
    is_sharpe: float
    oos_trades: int
    win_rate: float
    profit_factor: float
    turnover: float
    time_in_market: float
    liquidations: int
    positive_fold_ratio: float
    neighbor_pass_ratio: float
    stress_passed: bool
    stress_liquidations: int
    stress_worst_return: float
    return_skew: float
    return_excess_kurtosis: float
    attribution: CostAttribution
    gates: SixGateEvaluation
    folds: tuple[CandidateFoldResult, ...]
    curve: tuple[tuple[int, float], ...] = ()
    stress_scenarios: tuple[StressScenarioResult, ...] = ()
    swept_params: dict[str, Any] = field(default_factory=dict)
    catalog_entry_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "family_id": self.family_id,
            "catalog_entry_id": self.catalog_entry_id or self.family_id,
            "display_name": self.display_name,
            "overlay_preset": self.overlay_preset,
            "params": dict(self.params),
            "swept_params": dict(self.swept_params),
            "metrics": {
                "starting_equity": _jnum(self.starting_equity, 2),
                "ending_equity": _jnum(self.ending_equity, 2),
                "oos_total_return": _jnum(self.oos_total_return, 6),
                "oos_max_drawdown": _jnum(self.oos_max_drawdown, 6),
                "oos_sharpe": _jnum(self.oos_sharpe, 4),
                "oos_sortino": _jnum(self.oos_sortino, 4),
                "oos_calmar": _jnum(self.oos_calmar, 4),
                "is_sharpe": _jnum(self.is_sharpe, 4),
                "oos_trades": self.oos_trades,
                "win_rate": _jnum(self.win_rate, 4),
                "profit_factor": _jnum(self.profit_factor, 4),
                "turnover": _jnum(self.turnover, 4),
                "time_in_market": _jnum(self.time_in_market, 4),
                "liquidations": self.liquidations,
                "dsr": _jnum(self.gates.dsr, 4),
                "null_p_value": _jnum(self.gates.null_p_value, 4),
                "null_passed": self.gates.null_passed,
                "positive_fold_ratio": _jnum(self.positive_fold_ratio, 4),
                "neighbor_pass_ratio": _jnum(self.neighbor_pass_ratio, 4),
                "stress_passed": self.stress_passed,
                "stress_liquidations": self.stress_liquidations,
                "stress_worst_return": _jnum(self.stress_worst_return, 6),
                "return_skew": _jnum(self.return_skew, 4),
                "return_excess_kurtosis": _jnum(self.return_excess_kurtosis, 4),
            },
            "attribution": _attr_dict(self.attribution),
            "gates": self.gates.to_dict(),
            "stress_scenarios": [sc.to_dict() for sc in self.stress_scenarios],
            "folds": [f.to_dict() for f in self.folds],
            "curve": [[ts, eq] for ts, eq in self.curve],
        }


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """Top-level Phase 2 walk-forward discovery output consumed by CLI and HTML workbench."""

    symbol: str
    interval: str
    starting_equity: float
    oos_start_ms: int
    oos_end_ms: int
    oos_days: float
    n_folds: int
    n_trials: int
    runtime_ms: float
    funding_verified: bool
    stress_apr: float
    spot_1x: BenchmarkSummary
    perp_1x: BenchmarkSummary
    folds: tuple[WalkForwardFold, ...]
    candidates: tuple[CandidateResult, ...]
    schema_version: str = "phase2.discovery.v1"
    provenance: dict[str, Any] = field(default_factory=dict)
    effective_config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        counts = {"GO": 0, "CONDITIONAL": 0, "NO_GO": 0}
        for cand in self.candidates:
            counts[cand.gates.verdict] += 1
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "interval": self.interval,
            "starting_equity": _jnum(self.starting_equity, 2),
            "oos_start_ms": self.oos_start_ms,
            "oos_end_ms": self.oos_end_ms,
            "oos_days": _jnum(self.oos_days, 2),
            "n_folds": self.n_folds,
            "n_trials": self.n_trials,
            "runtime_ms": _jnum(self.runtime_ms, 2),
            "funding_verified": self.funding_verified,
            "stress_apr": _jnum(self.stress_apr, 4),
            "provenance": dict(self.provenance),
            "effective_config": dict(self.effective_config),
            "verdict_counts": counts,
            "benchmarks": {"spot_1x": self.spot_1x.to_dict(), "perp_1x": self.perp_1x.to_dict()},
            "folds": [f.to_dict() for f in self.folds],
            "candidates": [c.to_dict() for c in self.candidates],
            "winner_recipe": export_winner_recipe(self),
        }


def _resolve_funding_scenarios(
    series: BarSeries,
    instrument: InstrumentSpec,
    funding: FundingSeries | None,
    *,
    first_oos_start: int,
) -> tuple[bool, bool, FundingSeries | None, tuple[tuple[str, str, float, FundingSeries], ...]]:
    """Return (funding_verified, base_is_synth_zero, base_funding, funding_stress_series)."""
    if not instrument.is_perp or instrument.funding_interval_hours is None:
        return True, False, None, ()
    hours = instrument.funding_interval_hours
    full_start, oos_start, end_ms = int(series.open_times[0]), int(series.open_times[first_oos_start]), int(series.close_times[-1])

    synth_scenarios = tuple(
        (
            sc_id,
            sc_label,
            apr,
            FundingSeries.synthetic_constant_apr(
                instrument.instrument_id,
                start_ms=full_start,
                end_ms=end_ms,
                interval_hours=hours,
                apr=apr,
            ),
        )
        for sc_id, sc_label, apr in _FUNDING_STRESS_SPECS
    )

    if funding is None or len(funding) == 0:
        return False, True, synth_scenarios[0][3], synth_scenarios

    if funding.instrument_id != instrument.instrument_id:
        raise ValueError(f"FundingSeries instrument_id {funding.instrument_id!r} != {instrument.instrument_id!r}")
    funding_verified = len(funding.coverage_gaps(oos_start, end_ms, hours)) == 0
    base_series = funding
    if funding.coverage_gaps(full_start, end_ms, hours):
        try:
            base_series = funding.fill_gaps_with_apr(start_ms=full_start, end_ms=end_ms, interval_hours=hours, apr=0.0)
        except ValueError:
            base_series = funding.fill_gaps_with_apr(start_ms=full_start, end_ms=end_ms, interval_hours=hours, apr=0.0, phase_ms=0)
    return funding_verified, False, base_series, synth_scenarios


def _stitch_fold_results(
    fold_results: Sequence[SimulationResult],
    *,
    starting_equity: float,
    interval_ms: int,
    symbol: str,
    interval: str,
    strategy_id: str,
) -> SimulationResult:
    """Compound sequential OOS fold equity curves, attribution, and trade amounts cleanly."""
    running = float(starting_equity)
    curve: list[tuple[int, float]] = []
    trades = []
    gross = fees = slip = fund = notional = 0.0
    in_mkt = scored = liqs = 0
    for res in fold_results:
        scale = (running / starting_equity) if starting_equity > 0.0 else 0.0
        for ts, eq in res.equity_curve:
            curve.append((ts, eq * scale))
        gross += res.attribution.gross_pnl * scale
        fees += res.attribution.fees * scale
        slip += res.attribution.slippage * scale
        fund += res.attribution.funding_pnl * scale
        notional += res.turnover * starting_equity * scale
        in_mkt += int(round(res.time_in_market_pct * res.bars_scored))
        scored += res.bars_scored
        liqs += res.liquidations
        if scale > 0.0:
            for tr in res.trades:
                s_gross = tr.gross_pnl * scale
                s_fees = tr.fees * scale
                s_slip = tr.slippage * scale
                s_fund = tr.funding_pnl * scale
                s_net = math.fsum((s_gross, -s_fees, -s_slip, s_fund))
                trades.append(
                    replace(
                        tr,
                        qty=tr.qty * scale,
                        gross_pnl=s_gross,
                        fees=s_fees,
                        slippage=s_slip,
                        funding_pnl=s_fund,
                        net_pnl=s_net,
                    )
                )
        if running > 0.0 and starting_equity > 0.0:
            running = max(0.0, running * (res.ending_equity / starting_equity))
    return compute_simulation_metrics(
        starting_equity=starting_equity,
        ending_equity=running,
        equity_curve=curve,
        trades=trades,
        attribution=CostAttribution.from_components(gross_pnl=gross, fees=fees, slippage=slip, funding_pnl=fund),
        total_traded_notional=notional,
        bars_in_market=in_mkt,
        bars_scored=scored,
        bars_processed=scored,
        liquidations=liqs,
        interval_ms=interval_ms,
        symbol=symbol,
        interval=interval,
        strategy_id=strategy_id,
    )


def _anchored_is_metrics(
    full_is_res: SimulationResult,
    series: BarSeries,
    folds: Sequence[WalkForwardFold],
) -> list[tuple[float, float]]:
    """Slice a single causal anchored IS equity curve at each fold's ``is_end``."""
    curve = full_is_res.equity_curve
    out: list[tuple[float, float]] = []
    for fold in folds:
        cutoff_ms = int(series.close_times[fold.is_end - 1])
        sub = [pt for pt in curve if pt[0] <= cutoff_ms]
        if len(sub) < 2:
            out.append((0.0, 0.0))
            continue
        start_eq, end_eq = full_is_res.starting_equity, sub[-1][1]
        _, _, sh, _, _ = _compute_curve_stats(start_eq, end_eq, sub, series.interval_ms)
        ret = ((end_eq / start_eq) - 1.0) if start_eq > 0.0 else 0.0
        out.append((sh, ret))
    return out


def _benchmark_from_sim(label: str, res: SimulationResult) -> BenchmarkSummary:
    return BenchmarkSummary(
        label, res.starting_equity, res.ending_equity, res.total_return_pct, res.max_drawdown_pct,
        res.sharpe_ratio, res.sortino_ratio, res.calmar_ratio, res.attribution, _downsample_daily_curve(res.equity_curve),
    )


def _normalize_provenance(provenance: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {key: None for key in _PROVENANCE_KEYS}
    if provenance:
        for key, value in provenance.items():
            out[str(key)] = value
    return out


@dataclass(slots=True)
class _RawCandidate:
    candidate_id: str
    family_id: str
    catalog_entry_id: str
    display_name: str
    overlay_preset: str
    params: dict[str, Any]
    swept_params: dict[str, Any]
    grid_coord: tuple[int, ...]
    stitched: SimulationResult
    is_sharpe: float
    positive_fold_ratio: float
    stress_scenarios: tuple[StressScenarioResult, ...]
    stress_passed: bool
    stress_liquidations: int
    stress_worst_return: float
    null_p_value: float
    n_obs: int
    return_skew: float
    return_excess_kurtosis: float
    folds: tuple[CandidateFoldResult, ...]


def _compute_neighbor_ratios(raw_candidates: Sequence[_RawCandidate], gate_cfg: GateConfig) -> list[float]:
    """Compute exact 1-step grid neighbor stability ratios within each (family_id, overlay_preset)."""
    def _core_ok(c: _RawCandidate) -> bool:
        s = c.stitched
        return s.total_return_pct > 0.0 and s.sharpe_ratio >= gate_cfg.min_oos_sharpe and s.liquidations == 0 and abs(s.max_drawdown_pct) <= gate_cfg.max_drawdown

    groups: dict[tuple[str, str], list[int]] = {}
    for idx, cand in enumerate(raw_candidates):
        groups.setdefault((cand.family_id, cand.overlay_preset), []).append(idx)

    ratios = [1.0] * len(raw_candidates)
    for indices in groups.values():
        if len(indices) <= 1:
            continue
        for i in indices:
            ci = raw_candidates[i].grid_coord
            nbr_total = nbr_pass = 0
            for j in indices:
                if i == j or len(raw_candidates[j].grid_coord) != len(ci):
                    continue
                cj = raw_candidates[j].grid_coord
                diffs = [abs(ci[d] - cj[d]) for d in range(len(ci)) if ci[d] != cj[d]]
                if diffs == [1]:
                    nbr_total += 1
                    if _core_ok(raw_candidates[j]):
                        nbr_pass += 1
            ratios[i] = (nbr_pass / nbr_total) if nbr_total > 0 else 1.0
    return ratios


def _evaluate_stress_suite(
    *,
    strategy: Strategy,
    overlays: Sequence[RiskOverlay],
    oos_sims: Sequence[Simulator],
    slippage_sims: Sequence[Simulator],
    oos_slices: Sequence[BarSeries],
    oos_caches: Sequence[FeatureCache],
    base_funding: FundingSeries | None,
    base_is_synth_zero: bool,
    funding_stress_specs: Sequence[tuple[str, str, float, FundingSeries]],
    stitched: SimulationResult,
    starting_equity: float,
    interval_ms: int,
    symbol: str,
    interval: str,
    family_id: str,
) -> tuple[tuple[StressScenarioResult, ...], bool, int, float]:
    """Evaluate all 4 canonical stress scenarios (10bp slippage + 0%/15%/30% APR funding)."""
    n_folds = len(oos_slices)
    scenarios: list[StressScenarioResult] = []

    slip_res = _stitch_fold_results(
        [slippage_sims[k].run(strategy, oos_slices[k], funding=base_funding, overlays=overlays, feature_cache=oos_caches[k]) for k in range(n_folds)],
        starting_equity=starting_equity,
        interval_ms=interval_ms,
        symbol=symbol,
        interval=interval,
        strategy_id=family_id,
    )
    slip_pass = slip_res.liquidations == 0 and slip_res.total_return_pct > 0.0
    scenarios.append(
        StressScenarioResult(
            scenario_id="slippage_10bp",
            label="Slippage +10bp",
            available=True,
            passed=slip_pass,
            total_return=slip_res.total_return_pct,
            liquidations=slip_res.liquidations,
            detail=f"Return {slip_res.total_return_pct * 100.0:+.2f}%, Liq={slip_res.liquidations}",
        )
    )

    if funding_stress_specs:
        for sc_id, sc_label, apr, f_series in funding_stress_specs:
            if apr == 0.0 and base_is_synth_zero:
                sc_res = stitched
            else:
                sc_res = _stitch_fold_results(
                    [oos_sims[k].run(strategy, oos_slices[k], funding=f_series, overlays=overlays, feature_cache=oos_caches[k]) for k in range(n_folds)],
                    starting_equity=starting_equity,
                    interval_ms=interval_ms,
                    symbol=symbol,
                    interval=interval,
                    strategy_id=family_id,
                )
            sc_pass = sc_res.liquidations == 0 and sc_res.total_return_pct > 0.0
            scenarios.append(
                StressScenarioResult(
                    scenario_id=sc_id,
                    label=sc_label,
                    available=True,
                    passed=sc_pass,
                    total_return=sc_res.total_return_pct,
                    liquidations=sc_res.liquidations,
                    detail=f"Return {sc_res.total_return_pct * 100.0:+.2f}%, Liq={sc_res.liquidations}",
                )
            )
    else:
        for sc_id, sc_label, _ in _FUNDING_STRESS_SPECS:
            scenarios.append(
                StressScenarioResult(
                    scenario_id=sc_id,
                    label=sc_label,
                    available=False,
                    passed=None,
                    total_return=None,
                    liquidations=None,
                    detail="N/A — spot instrument",
                )
            )

    avail = [sc for sc in scenarios if sc.available]
    stress_passed = bool(avail) and all(sc.passed is True for sc in avail)
    stress_liqs = sum(sc.liquidations or 0 for sc in avail)
    stress_worst = min(sc.total_return for sc in avail if sc.total_return is not None)
    return tuple(scenarios), stress_passed, stress_liqs, stress_worst


def discover_strategies(
    series: BarSeries,
    instrument: InstrumentSpec,
    cost_model: CostModel,
    funding: FundingSeries | None = None,
    catalog: Sequence[StrategyCatalogEntry] | None = None,
    config: DiscoveryConfig | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> DiscoveryReport:
    """Run purged walk-forward strategy discovery across families, overlays, and stress suite."""
    t0 = time.perf_counter()
    cfg = config if config is not None else DiscoveryConfig()
    if series.symbol != instrument.symbol:
        raise ValueError(f"BarSeries symbol {series.symbol!r} != InstrumentSpec symbol {instrument.symbol!r}")

    folds = build_walk_forward_folds(len(series), cfg.n_folds, cfg.is_ratio, cfg.purge_bars, cfg.walk_forward_mode)
    first_oos_start = folds[0].oos_start
    oos_start_ms, oos_end_ms = int(series.open_times[first_oos_start]), int(series.close_times[-1])
    oos_days = max(0.0, (oos_end_ms - oos_start_ms) / float(_MS_PER_DAY))
    annualization_factor = MS_PER_YEAR / float(series.interval_ms)

    funding_verified, base_is_synth_zero, base_funding, funding_stress_specs = _resolve_funding_scenarios(
        series, instrument, funding, first_oos_start=first_oos_start,
    )

    is_anchored = cfg.walk_forward_mode == "anchored"
    anchored_is_slice = _slice_bar_series(series, 0, folds[-1].is_end) if is_anchored else None
    anchored_is_cache = FeatureCache() if is_anchored else None
    rolling_is_slices = () if is_anchored else tuple(_slice_bar_series(series, f.is_start, f.is_end) for f in folds)
    rolling_is_caches = () if is_anchored else tuple(FeatureCache() for _ in folds)
    oos_slices = tuple(series if f.oos_end == len(series) else _slice_bar_series(series, 0, f.oos_end) for f in folds)
    oos_caches = tuple(FeatureCache() for _ in folds)

    is_sim = Simulator(instrument, cost_model, SimulatorConfig(starting_equity=cfg.starting_equity, close_on_end=True, funding_gap_policy="allow_partial"))
    oos_sims = tuple(Simulator(instrument, cost_model, SimulatorConfig(starting_equity=cfg.starting_equity, warmup_bars=f.oos_start, close_on_end=True)) for f in folds)
    slippage_cost = replace(cost_model, half_spread_bps=cost_model.half_spread_bps + 10.0)
    slippage_sims = tuple(
        Simulator(instrument, slippage_cost, SimulatorConfig(starting_equity=cfg.starting_equity, warmup_bars=f.oos_start, close_on_end=True))
        for f in folds
    )

    spot_inst = spot_spec(
        instrument.symbol, venue=instrument.venue, tick_size=instrument.tick_size,
        lot_size=instrument.lot_size, min_size=instrument.min_size, contract_value=instrument.contract_value,
    )
    spot_1x = _benchmark_from_sim(
        "Spot 1x Buy & Hold",
        compute_buy_and_hold_benchmark(
            series, starting_equity=cfg.starting_equity, leverage=1.0, side="long",
            instrument=spot_inst, cost_model=cost_model, funding=None, warmup_bars=first_oos_start, close_on_end=True,
        ),
    )
    perp_1x = (
        _benchmark_from_sim(
            "Perp 1x Buy & Hold",
            compute_buy_and_hold_benchmark(
                series, starting_equity=cfg.starting_equity, leverage=1.0, side="long",
                instrument=instrument, cost_model=cost_model, funding=base_funding, warmup_bars=first_oos_start, close_on_end=True,
            ),
        )
        if instrument.is_perp else spot_1x
    )

    entries = tuple(catalog) if catalog is not None else default_strategy_catalog(symbol=instrument.symbol, interval=series.interval, allow_short=instrument.is_perp)
    if cfg.family_ids is not None:
        allowed = set(cfg.family_ids)
        entries = tuple(e for e in entries if e.family_id in allowed)
        if not entries:
            raise ValueError(f"no catalog entries matched family_ids={cfg.family_ids!r}")

    raw_candidates: list[_RawCandidate] = []
    for entry in entries:
        spec_names = tuple(spec.name for spec in entry.param_space.specs)
        val_idx = tuple({val: idx for idx, val in enumerate(spec.grid_values())} for spec in entry.param_space.specs)
        for p_idx, params in enumerate(entry.param_space.iter_valid_grid()):
            coord = tuple(val_idx[d][params[spec_names[d]]] for d in range(len(spec_names)))
            swept_params = dict(params)
            effective_params = {**entry.fixed_params, **swept_params}
            for overlay_name in cfg.overlay_names:
                overlays = overlay_presets()[overlay_name]
                strategy = entry.factory(swept_params)

                if is_anchored and anchored_is_slice is not None and anchored_is_cache is not None:
                    full_is = is_sim.run(strategy, anchored_is_slice, funding=base_funding, overlays=overlays, feature_cache=anchored_is_cache)
                    is_fold_metrics = _anchored_is_metrics(full_is, series, folds)
                else:
                    is_fold_metrics = []
                    for k in range(len(folds)):
                        ir = is_sim.run(strategy, rolling_is_slices[k], funding=base_funding, overlays=overlays, feature_cache=rolling_is_caches[k])
                        is_fold_metrics.append((ir.sharpe_ratio, ir.total_return_pct))

                fold_oos_results = [
                    oos_sims[k].run(strategy, oos_slices[k], funding=base_funding, overlays=overlays, feature_cache=oos_caches[k])
                    for k in range(len(folds))
                ]
                fold_summaries = tuple(
                    CandidateFoldResult(fold, is_sh, is_ret, oos.sharpe_ratio, oos.total_return_pct, oos.max_drawdown_pct, oos.trade_count, oos.liquidations)
                    for fold, (is_sh, is_ret), oos in zip(folds, is_fold_metrics, fold_oos_results, strict=True)
                )
                stitched = _stitch_fold_results(
                    fold_oos_results, starting_equity=cfg.starting_equity, interval_ms=series.interval_ms,
                    symbol=instrument.symbol, interval=series.interval, strategy_id=entry.family_id,
                )
                mean_is_sharpe = math.fsum(sh for sh, _ in is_fold_metrics) / len(is_fold_metrics)
                pos_fold_ratio = sum(1 for f in fold_summaries if f.oos_total_return > 0.0) / len(fold_summaries)
                n_obs, skew, excess_kurt = _return_moments(stitched.equity_curve)
                null_p = compute_null_distribution_pvalue(stitched.equity_curve)

                stress_scenarios, stress_passed, stress_liqs, stress_worst_ret = _evaluate_stress_suite(
                    strategy=strategy,
                    overlays=overlays,
                    oos_sims=oos_sims,
                    slippage_sims=slippage_sims,
                    oos_slices=oos_slices,
                    oos_caches=oos_caches,
                    base_funding=base_funding,
                    base_is_synth_zero=base_is_synth_zero,
                    funding_stress_specs=funding_stress_specs,
                    stitched=stitched,
                    starting_equity=cfg.starting_equity,
                    interval_ms=series.interval_ms,
                    symbol=instrument.symbol,
                    interval=series.interval,
                    family_id=entry.family_id,
                )

                raw_candidates.append(_RawCandidate(
                    f"{entry.family_id}#{p_idx:02d}|{overlay_name}", entry.family_id, entry.family_id, entry.display_name, overlay_name,
                    effective_params, swept_params, coord, stitched, mean_is_sharpe, pos_fold_ratio,
                    stress_scenarios, stress_passed, stress_liqs, stress_worst_ret,
                    null_p, n_obs, skew, excess_kurt, fold_summaries,
                ))

    n_trials = len(raw_candidates)
    gate_cfg = replace(cfg.gate_config, stress_apr=cfg.stress_apr)
    neighbor_ratios = _compute_neighbor_ratios(raw_candidates, gate_cfg)

    evaluated: list[tuple[CandidateResult, tuple[tuple[int, float], ...]]] = []
    for raw, nbr_ratio in zip(raw_candidates, neighbor_ratios, strict=True):
        st = raw.stitched
        gates = evaluate_six_gates(
            oos_days=oos_days, oos_trades=st.trade_count, oos_total_return=st.total_return_pct,
            oos_sharpe=st.sharpe_ratio, oos_max_drawdown=st.max_drawdown_pct, oos_calmar=st.calmar_ratio,
            liquidations=st.liquidations, n_obs=raw.n_obs, return_skew=raw.return_skew,
            return_excess_kurtosis=raw.return_excess_kurtosis, n_trials=max(1, n_trials),
            positive_fold_ratio=raw.positive_fold_ratio, is_sharpe=raw.is_sharpe, is_perp=instrument.is_perp,
            funding_verified=funding_verified, stress_passed=raw.stress_passed, stress_liquidations=raw.stress_liquidations,
            spot_oos_sharpe=spot_1x.sharpe, spot_oos_max_drawdown=spot_1x.max_drawdown,
            neighbor_pass_ratio=nbr_ratio, null_p_value=raw.null_p_value,
            annualization_factor=annualization_factor, config=gate_cfg,
        )
        cand = CandidateResult(
            raw.candidate_id, raw.family_id, raw.display_name, raw.overlay_preset, raw.params,
            st.starting_equity, st.ending_equity, st.total_return_pct, st.max_drawdown_pct,
            st.sharpe_ratio, st.sortino_ratio, st.calmar_ratio, raw.is_sharpe, st.trade_count,
            st.win_rate, st.profit_factor, st.turnover, st.time_in_market_pct, st.liquidations,
            raw.positive_fold_ratio, nbr_ratio, raw.stress_passed, raw.stress_liquidations, raw.stress_worst_return,
            raw.return_skew, raw.return_excess_kurtosis, st.attribution, gates, raw.folds,
            stress_scenarios=raw.stress_scenarios,
            swept_params=raw.swept_params,
            catalog_entry_id=raw.catalog_entry_id,
        )
        evaluated.append((cand, st.equity_curve))

    evaluated.sort(
        key=lambda item: (
            _VERDICT_RANK[item[0].gates.verdict], -item[0].gates.passed_count,
            -item[0].gates.dsr, -item[0].oos_sharpe, -item[0].oos_total_return, item[0].candidate_id,
        )
    )
    final_candidates = tuple(
        replace(cand, curve=_downsample_daily_curve(raw_curve) if rank < cfg.top_curves else ())
        for rank, (cand, raw_curve) in enumerate(evaluated)
    )
    effective_config: dict[str, Any] = {
        "n_folds": cfg.n_folds,
        "is_ratio": cfg.is_ratio,
        "purge_bars": cfg.purge_bars,
        "walk_forward_mode": cfg.walk_forward_mode,
        "starting_equity": _jnum(cfg.starting_equity, 2),
        "overlay_names": list(cfg.overlay_names),
        "family_ids": list(cfg.family_ids) if cfg.family_ids is not None else [e.family_id for e in entries],
        "stress_apr": _jnum(cfg.stress_apr, 4),
        "stress_scenarios": list(_STRESS_SCENARIO_IDS),
        "cost_model": cost_model.to_dict(),
        "gate_config": asdict(gate_cfg),
        "instrument": instrument.to_dict(),
    }
    return DiscoveryReport(
        symbol=instrument.symbol, interval=series.interval, starting_equity=cfg.starting_equity,
        oos_start_ms=oos_start_ms, oos_end_ms=oos_end_ms, oos_days=oos_days,
        n_folds=len(folds), n_trials=n_trials, runtime_ms=(time.perf_counter() - t0) * 1000.0,
        funding_verified=funding_verified, stress_apr=cfg.stress_apr,
        spot_1x=spot_1x, perp_1x=perp_1x, folds=folds, candidates=final_candidates,
        provenance=_normalize_provenance(provenance),
        effective_config=effective_config,
    )


def export_winner_recipe(report: DiscoveryReport) -> dict[str, Any]:
    """Export the top GO/CONDITIONAL candidate as a strictly dry-run recipe, or a NO_GO warning."""
    top = report.candidates[0] if report.candidates else None
    if top is None or top.gates.verdict == "NO_GO":
        return {
            "schema_version": "phase2.recipe.v1",
            "dry_run_only": True,
            "live_orders_allowed": False,
            "symbol": report.symbol,
            "interval": report.interval,
            "status": "NO_GO_WARNING",
            "verdict": "NO_GO",
            "warning": "No candidate passed Phase 2 GO or CONDITIONAL gates; do NOT deploy.",
            "strategy": None,
            "best_rejected_candidate": (
                {
                    "candidate_id": top.candidate_id,
                    "family_id": top.family_id,
                    "catalog_entry_id": top.catalog_entry_id or top.family_id,
                    "overlay_preset": top.overlay_preset,
                    "params": dict(top.params),
                    "swept_params": dict(top.swept_params),
                    "passed_count": top.gates.passed_count,
                    "failed_gates": list(top.gates.failed_gates),
                    "oos_total_return": _jnum(top.oos_total_return, 6),
                    "oos_sharpe": _jnum(top.oos_sharpe, 4),
                    "dsr": _jnum(top.gates.dsr, 4),
                }
                if top is not None else None
            ),
        }

    is_go = top.gates.verdict == "GO"
    return {
        "schema_version": "phase2.recipe.v1",
        "dry_run_only": True,
        "live_orders_allowed": False,
        "symbol": report.symbol,
        "interval": report.interval,
        "status": "READY_DRY_RUN" if is_go else "CONDITIONAL_DRY_RUN",
        "verdict": top.gates.verdict,
        "warning": None if is_go else f"CONDITIONAL verdict: passed {top.gates.passed_count}/6, failed={list(top.gates.failed_gates)}; review before paper/dry-run.",
        "strategy": {
            "candidate_id": top.candidate_id,
            "family_id": top.family_id,
            "catalog_entry_id": top.catalog_entry_id or top.family_id,
            "display_name": top.display_name,
            "overlay_preset": top.overlay_preset,
            "params": dict(top.params),
            "swept_params": dict(top.swept_params),
        },
        "oos_summary": {
            "oos_days": _jnum(report.oos_days, 2), "n_folds": report.n_folds, "n_trials": report.n_trials,
            "oos_total_return": _jnum(top.oos_total_return, 6), "oos_max_drawdown": _jnum(top.oos_max_drawdown, 6),
            "oos_sharpe": _jnum(top.oos_sharpe, 4), "oos_calmar": _jnum(top.oos_calmar, 4),
            "oos_trades": top.oos_trades, "liquidations": top.liquidations,
            "dsr": _jnum(top.gates.dsr, 4), "null_p_value": _jnum(top.gates.null_p_value, 4),
        },
        "gates": top.gates.to_dict(),
    }
