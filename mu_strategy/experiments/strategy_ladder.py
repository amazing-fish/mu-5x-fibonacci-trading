from __future__ import annotations

import argparse
import html
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from mu_strategy.backtest import run_backtest
from mu_strategy.execution.instruments import OKXInstrumentSpec
from mu_strategy.experiments.walk_forward import WindowBacktest, run_evaluator_walk_forward_backtests
from mu_strategy.market_data.service import refresh_trusted_candle_bundle
from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.market_data.trusted_data.compat import trusted_bundle_error
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import BacktestResult, Candle, Fill, Trade
from mu_strategy.research.candidate_conclusions import (
    CandidateConclusion,
    CandidateConclusionError,
    CandidateConclusionIndex,
    CandidateRobustness,
    CandidateStatus,
    FeeAssumption,
    StressCellReturn,
    STRATEGY_LADDER_DEFAULT_FEE_BPS,
    STRATEGY_LADDER_FEE_GRID_BPS,
    STRATEGY_LADDER_PROTOCOL_VERSION,
    STRATEGY_LADDER_SLIPPAGE_GRID_TICKS,
    STRATEGY_LADDER_TOP_N,
    format_candidate_metric,
    validate_candidate_artifact_path,
    write_candidate_conclusion_index,
)
from mu_strategy.research.robustness import trade_concentration
from mu_strategy.strategies.registry import baseline_strategy_group


TRUSTED_REQUESTED_INTERVALS = ("15m", "1h")
FEE_GRID_BPS = STRATEGY_LADDER_FEE_GRID_BPS
SLIPPAGE_GRID_TICKS = STRATEGY_LADDER_SLIPPAGE_GRID_TICKS
MOMENTUM_LOOKBACK_HOURS = (24, 96, 168)
MOMENTUM_HISTORY_HOURS = max(MOMENTUM_LOOKBACK_HOURS) + 1
MOMENTUM_HISTORY_DAYS = (MOMENTUM_HISTORY_HOURS + 23) // 24
DEFAULT_FEE_BPS = STRATEGY_LADDER_DEFAULT_FEE_BPS
TOP_N_TRADES = STRATEGY_LADDER_TOP_N
PROTOCOL_VERSION = STRATEGY_LADDER_PROTOCOL_VERSION
LOCAL_CANDIDATE_LEVERAGE = 1.0
ACCOUNT_RETURN_BASIS = (
    "Account return = sum(window ending equity) / sum(window starting equity) - 1. "
    "Windows start independently; returns are not compounded across windows. "
    "This is not a trade's return on committed margin."
)
RANKING_BASIS = (
    "Ranks order raw account returns at each candidate's configured leverage; "
    "they are not leverage- or risk-normalized. "
    "Configured leverage is not constant exposure: baseline uses staged position sizing."
)
HOUR_MS = 3_600_000
DEFAULT_REPORT_PATH = Path("reports/live/mu_okx_strategy_ladder.md")
DEFAULT_HTML_REPORT_PATH = Path("reports/live/mu_okx_strategy_ladder.html")
DEFAULT_CONCLUSION_PATH = Path("reports/live/mu_okx_strategy_ladder_conclusions.json")
DEFAULT_MU_INSTRUMENT = OKXInstrumentSpec(
    "MU-USDT-SWAP",
    Decimal("0.1"),
    Decimal("0.01"),
    Decimal("1"),
)


class StrategyLadderDataError(RuntimeError):
    def __init__(self, reason: HealthReason, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or f"trusted data blocked: {reason.value}")


class StrategyLadderOutputError(ValueError):
    """Raised before a ladder run can write outside its artifact boundary."""


@dataclass(frozen=True)
class CandidateDefinition:
    candidate_id: str
    family: str
    label: str
    source: str
    lookback_hours: int | None = None


@dataclass(frozen=True)
class CandidateSummary:
    starting_equity: float
    ending_equity: float
    trades: tuple[Trade, ...]
    max_drawdown_pct: float

    @property
    def total_return_pct(self) -> float:
        return (self.ending_equity / self.starting_equity) - 1 if self.starting_equity else 0.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for trade in self.trades if trade.pnl > 0) / len(self.trades)


@dataclass(frozen=True)
class StressCell:
    fee_bps_per_side: int
    slippage_ticks: int
    windows: tuple[WindowBacktest, ...]
    summary: CandidateSummary


@dataclass(frozen=True)
class CandidateEvaluation:
    definition: CandidateDefinition
    stress_grid: tuple[StressCell, ...]
    configured_leverage: float

    @property
    def default_cell(self) -> StressCell:
        for cell in self.stress_grid:
            if cell.fee_bps_per_side == DEFAULT_FEE_BPS and cell.slippage_ticks == 0:
                return cell
        raise RuntimeError("default stress cell is missing")

    @property
    def survives_stress_grid(self) -> bool:
        return self.default_cell.summary.trade_count > 0 and all(
            Decimal(format_candidate_metric(cell.summary.total_return_pct)) >= 0
            for cell in self.stress_grid
        )


@dataclass(frozen=True)
class StrategyLadderResult:
    symbol: str
    run_id: str | None
    data_files: tuple[Path, ...]
    instrument: OKXInstrumentSpec
    evaluations: tuple[CandidateEvaluation, ...]
    conclusion_index: CandidateConclusionIndex


def candidate_definitions() -> tuple[CandidateDefinition, ...]:
    return (
        CandidateDefinition(
            "overnight_seasonality",
            "overnight_seasonality",
            "Overnight seasonality (22:00–00:00 UTC)",
            "issue-93 overnight seasonality hypothesis",
        ),
        *(CandidateDefinition(
            f"time_series_momentum_{lookback}h",
            "time_series_momentum",
            f"Time-series momentum ({lookback}h)",
            "issue-93 trailing-return-sign hypothesis",
            lookback,
        ) for lookback in MOMENTUM_LOOKBACK_HOURS),
        CandidateDefinition(
            "baseline",
            "baseline",
            "Registry baseline anchor",
            "mu_strategy.strategies.registry:baseline",
        ),
    )


def overnight_target_long(closed_candles: list[Candle], next_bar_open_time_ms: int) -> bool:
    """Return the scheduled long state without inspecting the bar to trade."""

    if closed_candles and closed_candles[-1].open_time_ms + HOUR_MS > next_bar_open_time_ms:
        raise ValueError("closed_candles contains a candle not closed before the trade bar")
    hour = datetime.fromtimestamp(next_bar_open_time_ms / 1000, tz=timezone.utc).hour
    return hour >= 22


def momentum_target_long(closed_candles: list[Candle], lookback_hours: int) -> bool:
    """Use only closed hourly candles to decide the next bar's long/flat state."""

    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    if len(closed_candles) <= lookback_hours:
        return False
    current_close = closed_candles[-1].close
    trailing_close = closed_candles[-1 - lookback_hours].close
    return trailing_close > 0 and current_close > trailing_close


def run_long_only_candidate(
    candles_1h: list[Candle],
    *,
    definition: CandidateDefinition,
    fee_bps_per_side: int,
    slippage_ticks: int,
    instrument: OKXInstrumentSpec,
    starting_equity: float = 10_000.0,
    execution_start_time_ms: int | None = None,
    execution_end_time_ms: int | None = None,
) -> BacktestResult:
    if fee_bps_per_side < 0 or slippage_ticks < 0:
        raise ValueError("fee and slippage values must be non-negative")
    ordered = sorted(candles_1h, key=lambda bar: bar.open_time_ms)
    execution_indices = [
        index
        for index, bar in enumerate(ordered)
        if (execution_start_time_ms is None or bar.open_time_ms >= execution_start_time_ms)
        and (execution_end_time_ms is None or bar.open_time_ms < execution_end_time_ms)
        and (execution_end_time_ms is None or bar.open_time_ms + HOUR_MS <= execution_end_time_ms)
    ]
    if len(ordered) < 2 or not execution_indices:
        return BacktestResult(starting_equity, starting_equity, [], [])

    fee_rate = fee_bps_per_side / 10_000
    slip = float(instrument.tick_size * slippage_ticks)
    equity = starting_equity
    first_execution_bar = ordered[execution_indices[0]]
    equity_curve: list[tuple[int, float]] = [(first_execution_bar.open_time_ms, equity)]
    trades: list[Trade] = []
    open_fill: Fill | None = None

    for index in execution_indices:
        if index == 0:
            continue
        trade_bar = ordered[index]
        closed = ordered[:index]
        if definition.family == "overnight_seasonality":
            target_long = overnight_target_long(closed, trade_bar.open_time_ms)
        elif definition.family == "time_series_momentum":
            if definition.lookback_hours is None:
                raise ValueError("momentum candidate requires lookback_hours")
            target_long = momentum_target_long(closed, definition.lookback_hours)
        else:
            raise ValueError(f"unsupported local candidate family: {definition.family}")

        if target_long and open_fill is None and equity > 0:
            entry_price = trade_bar.open + slip
            if entry_price <= 0:
                raise ValueError("slipped entry price must be positive")
            notional = equity * LOCAL_CANDIDATE_LEVERAGE
            units = notional / entry_price
            open_fill = Fill(
                time_ms=trade_bar.open_time_ms,
                price=entry_price,
                margin_fraction=1.0,
                notional=notional,
                units=units,
                fee=notional * fee_rate,
            )
        elif not target_long and open_fill is not None:
            exit_price = trade_bar.open - slip
            equity, trade = _close_long_only_trade(
                open_fill,
                exit_time_ms=trade_bar.open_time_ms,
                exit_price=exit_price,
                equity=equity,
                fee_rate=fee_rate,
                reason="signal_flat",
            )
            trades.append(trade)
            open_fill = None

        marked = equity
        if open_fill is not None:
            marked += (trade_bar.close - open_fill.price) * open_fill.units - open_fill.fee
        equity_curve.append((trade_bar.open_time_ms + HOUR_MS, marked))

    if open_fill is not None:
        final = ordered[execution_indices[-1]]
        equity, trade = _close_long_only_trade(
            open_fill,
            exit_time_ms=final.open_time_ms + HOUR_MS,
            exit_price=final.close - slip,
            equity=equity,
            fee_rate=fee_rate,
            reason="end_of_data",
        )
        trades.append(trade)
        equity_curve.append((final.open_time_ms + HOUR_MS, equity))

    return BacktestResult(starting_equity, equity, trades, equity_curve)


def evaluate_strategy_ladder(
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    *,
    symbol: str,
    instrument: OKXInstrumentSpec,
    window_days: int = 14,
    windows: int = 2,
) -> tuple[CandidateEvaluation, ...]:
    definitions = candidate_definitions()
    baseline_config = baseline_strategy_group(symbol).config
    evaluations: list[CandidateEvaluation] = []

    for definition in definitions:
        cells: list[StressCell] = []
        for fee_bps in FEE_GRID_BPS:
            for slippage_ticks in SLIPPAGE_GRID_TICKS:
                if definition.family == "baseline":
                    config = replace(
                        baseline_config,
                        fee_profile="market",
                        fee_rate=fee_bps / 10_000,
                    )

                    def evaluator(
                        segment_15m: list[Candle],
                        _segment_1h: list[Candle],
                        context: dict[int, str],
                        *,
                        _config=config,
                        _ticks=slippage_ticks,
                    ) -> BacktestResult:
                        result = run_backtest(segment_15m, context, config=_config)
                        return apply_adverse_tick_slippage(
                            result,
                            instrument.tick_size,
                            _ticks,
                            leverage=_config.leverage,
                        )
                else:

                    def evaluator(
                        _segment_15m: list[Candle],
                        segment_1h: list[Candle],
                        _context: dict[int, str],
                        *,
                        _definition=definition,
                        _fee_bps=fee_bps,
                        _ticks=slippage_ticks,
                    ) -> BacktestResult:
                        return run_long_only_candidate(
                            segment_1h,
                            definition=_definition,
                            fee_bps_per_side=_fee_bps,
                            slippage_ticks=_ticks,
                            instrument=instrument,
                            execution_start_time_ms=_segment_15m[0].open_time_ms,
                            execution_end_time_ms=(
                                _segment_15m[-1].open_time_ms
                                + _infer_interval_ms(_segment_15m)
                            ),
                        )

                window_results = tuple(run_evaluator_walk_forward_backtests(
                    candles_15m,
                    candles_1h,
                    evaluator=evaluator,
                    window_days=window_days,
                    windows=windows,
                    history_hours=MOMENTUM_HISTORY_HOURS,
                ))
                cells.append(StressCell(
                    fee_bps,
                    slippage_ticks,
                    window_results,
                    summarize_windows(window_results),
                ))
        configured_leverage = (
            baseline_config.leverage if definition.family == "baseline" else LOCAL_CANDIDATE_LEVERAGE
        )
        evaluations.append(CandidateEvaluation(definition, tuple(cells), configured_leverage))
    return tuple(evaluations)


def apply_adverse_tick_slippage(
    result: BacktestResult,
    tick_size: Decimal,
    slippage_ticks: int,
    *,
    leverage: float = 1.0,
) -> BacktestResult:
    """Apply equal adverse entry/exit ticks to a fixed baseline trade path."""

    if slippage_ticks < 0 or leverage <= 0:
        raise ValueError("slippage_ticks must be non-negative and leverage must be positive")
    if slippage_ticks == 0:
        return result
    slip = float(tick_size * slippage_ticks)
    adjusted_trades: list[Trade] = []
    equity = result.starting_equity
    for trade in result.trades:
        if trade.exit_price - slip <= 0:
            raise ValueError("slipped exit price must be positive")
        units = sum(fill.units for fill in trade.fills)
        slippage_cost = 2 * slip * units
        committed_margin = sum(fill.notional for fill in trade.fills) / leverage
        adjusted_pnl = trade.pnl - slippage_cost
        adjusted_return = adjusted_pnl / committed_margin if committed_margin else 0.0
        adjusted_fills = [replace(fill, price=fill.price + slip) for fill in trade.fills]
        adjusted_trade = replace(
            trade,
            entry_price=trade.entry_price + slip,
            exit_price=trade.exit_price - slip,
            fills=adjusted_fills,
            pnl=adjusted_pnl,
            return_pct=adjusted_return,
        )
        adjusted_trades.append(adjusted_trade)
        equity += adjusted_pnl
    equity_curve = [
        (time_ms, marked_equity - _slippage_cost_at(result.trades, time_ms, slip))
        for time_ms, marked_equity in result.equity_curve
    ]
    return BacktestResult(result.starting_equity, equity, adjusted_trades, equity_curve)


def summarize_windows(windows: tuple[WindowBacktest, ...]) -> CandidateSummary:
    if not windows:
        return CandidateSummary(10_000.0, 10_000.0, (), 0.0)
    return CandidateSummary(
        starting_equity=sum(window.result.starting_equity for window in windows),
        ending_equity=sum(window.result.ending_equity for window in windows),
        trades=tuple(trade for window in windows for trade in window.result.trades),
        max_drawdown_pct=min((window.result.max_drawdown_pct for window in windows), default=0.0),
    )


def build_conclusion_index(
    evaluations: tuple[CandidateEvaluation, ...],
    instrument: OKXInstrumentSpec,
    *,
    trusted_generation: str,
    window_days: int,
    windows: int,
) -> CandidateConclusionIndex:
    fee_assumption = FeeAssumption(
        default_fee_bps_per_side=DEFAULT_FEE_BPS,
        fee_grid_bps_per_side=FEE_GRID_BPS,
        slippage_grid_ticks=SLIPPAGE_GRID_TICKS,
        tick_size=format(instrument.tick_size, "f"),
    )
    entries: list[CandidateConclusion] = []
    for family in ("overnight_seasonality", "time_series_momentum", "baseline"):
        family_evaluations = tuple(item for item in evaluations if item.definition.family == family)
        metrics = tuple(_candidate_robustness(item) for item in family_evaluations)
        entries.append(CandidateConclusion(
            family=family,
            source=family_evaluations[0].definition.source,
            protocol_version=PROTOCOL_VERSION,
            fee_assumption=fee_assumption,
            robustness_metrics=metrics,
            status=(
                CandidateStatus.CANDIDATE
                if any(item.survives_stress_grid for item in family_evaluations)
                else CandidateStatus.STRESS_FAILED
            ),
        ))
    return CandidateConclusionIndex(
        tuple(entries),
        instrument_id=instrument.inst_id,
        trusted_generation=trusted_generation,
        window_days=window_days,
        windows=windows,
    )


def render_strategy_ladder_report(result: StrategyLadderResult) -> str:
    ranked = sorted(
        result.evaluations,
        key=lambda item: (-item.default_cell.summary.total_return_pct, item.definition.candidate_id),
    )
    lines = [
        f"# {result.symbol} Strategy Candidate Ladder",
        "",
        f"- trusted generation: {result.run_id or '-'}",
        f"- protocol: {PROTOCOL_VERSION}",
        f"- default cost: {DEFAULT_FEE_BPS} bp per side, 0 slippage ticks",
        f"- tick size: {format(result.instrument.tick_size, 'f')}",
        f"- momentum pre-window history: {MOMENTUM_HISTORY_HOURS} closed-hour slots; executions remain window-local",
        f"- data files: {', '.join(str(path) for path in result.data_files) if result.data_files else '-'}",
        f"- {ACCOUNT_RETURN_BASIS}",
        f"- {RANKING_BASIS}",
        "- status rule: candidate only when at least one family variant trades and has non-negative return in every stress cell; otherwise stress_failed.",
        "",
        "## Raw account-return ranking",
        "",
        "| Rank | Candidate | Family | Configured leverage | Account return | Max drawdown | Trades | Win rate | Top-5 trade concentration | Stress status |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for rank, evaluation in enumerate(ranked, start=1):
        summary = evaluation.default_cell.summary
        concentration = trade_concentration(summary.trades, top_n=TOP_N_TRADES)
        lines.append(
            f"| {rank} | {evaluation.definition.candidate_id} | {evaluation.definition.family} | "
            f"{evaluation.configured_leverage:g}x | "
            f"{summary.total_return_pct:.4%} | {summary.max_drawdown_pct:.4%} | {summary.trade_count} | "
            f"{summary.win_rate:.4%} | {_format_optional_pct(concentration.top_n_share_of_net_pnl)} | "
            f"{'candidate' if evaluation.survives_stress_grid else 'stress_failed'} |"
        )

    for evaluation in result.evaluations:
        lines.extend(_candidate_report_lines(evaluation))
    lines.extend(["", "Research artifact only; not financial advice."])
    return "\n".join(lines)


def render_strategy_ladder_html(result: StrategyLadderResult) -> str:
    ranked = sorted(
        result.evaluations,
        key=lambda item: (-item.default_cell.summary.total_return_pct, item.definition.candidate_id),
    )
    ranking_rows = "\n".join(_ranking_html_row(rank, item) for rank, item in enumerate(ranked, start=1))
    candidate_sections = "\n".join(_candidate_html_section(item) for item in result.evaluations)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result.symbol)} Strategy Candidate Ladder</title>
  <style>
    :root {{ color-scheme: light; --bg:#f4f6f8; --panel:#fff; --ink:#172033; --muted:#667085; --line:#d9e0e8; --good:#067647; --bad:#b42318; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font-family:"Segoe UI",Arial,sans-serif; }}
    main {{ max-width:1320px; margin:auto; padding:28px; }}
    h1 {{ margin:0 0 6px; }} h2 {{ margin-top:28px; }} h3 {{ margin:20px 0 8px; }}
    .meta {{ color:var(--muted); margin-bottom:18px; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow-x:auto; margin-bottom:16px; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ border-bottom:1px solid var(--line); padding:9px 10px; text-align:left; white-space:nowrap; }}
    th {{ background:#eef2f6; }} td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
    .good {{ color:var(--good); }} .bad {{ color:var(--bad); }}
  </style>
</head>
<body><main>
  <h1>{html.escape(result.symbol)} Strategy Candidate Ladder</h1>
  <div class="meta">Trusted generation: {html.escape(result.run_id or '-')} · Protocol: {PROTOCOL_VERSION} · Tick size: {html.escape(format(result.instrument.tick_size, 'f'))} · Momentum history: {MOMENTUM_HISTORY_HOURS} hours, window-local executions</div>
  <p>{html.escape(ACCOUNT_RETURN_BASIS)}</p>
  <p>{html.escape(RANKING_BASIS)}</p>
  <h2>Raw account-return ranking</h2>
  <div class="panel"><table><thead><tr><th>Rank</th><th>Candidate</th><th>Family</th><th>Configured leverage</th><th>Account return</th><th>Max drawdown</th><th>Trades</th><th>Win rate</th><th>Top-5 concentration</th><th>Status</th></tr></thead><tbody>{ranking_rows}</tbody></table></div>
  {candidate_sections}
  <p class="meta">Research artifact only; not financial advice.</p>
</main></body></html>"""


def run_strategy_ladder(
    *,
    symbol: str = "MU-USDT-SWAP",
    window_days: int = 14,
    windows: int = 2,
    data_dir: Path = Path("data/live"),
    report_path: Path | None = None,
    html_report_path: Path | None = None,
    conclusion_path: Path | None = None,
    instrument: OKXInstrumentSpec = DEFAULT_MU_INSTRUMENT,
) -> StrategyLadderResult:
    report_path, html_report_path, conclusion_path = build_cli_output_paths(
        instrument.inst_id,
        report_path=report_path,
        html_report_path=html_report_path,
        conclusion_path=conclusion_path,
    )
    report_path, html_report_path, conclusion_path = _validate_output_paths(
        data_dir=data_dir,
        report_path=report_path,
        html_report_path=html_report_path,
        conclusion_path=conclusion_path,
    )
    bundle = refresh_trusted_candle_bundle(
        symbol,
        intervals=TRUSTED_REQUESTED_INTERVALS,
        days=(window_days * windows) + MOMENTUM_HISTORY_DAYS,
        data_dir=data_dir,
        refresh=False,
    )
    status_error = trusted_bundle_error(bundle, requested_intervals=TRUSTED_REQUESTED_INTERVALS)
    if status_error:
        decision = bundle.trust_decision
        reason = decision.reason if decision is not None else HealthReason.MANIFEST_BLOCKED
        raise StrategyLadderDataError(reason, status_error)
    incomplete_intervals = tuple(
        interval
        for interval in TRUSTED_REQUESTED_INTERVALS
        if bundle.statuses_by_interval[interval].coverage_state != "complete"
    )
    if incomplete_intervals:
        raise StrategyLadderDataError(
            HealthReason.INSUFFICIENT_COVERAGE,
            f"trusted data blocked: insufficient_coverage:{','.join(incomplete_intervals)}",
        )
    if instrument.inst_id != bundle.symbol.inst_id:
        raise ValueError("instrument metadata does not match trusted symbol")

    evaluations = evaluate_strategy_ladder(
        bundle.candles_by_interval["15m"],
        bundle.candles_by_interval["1h"],
        symbol=bundle.symbol.inst_id,
        instrument=instrument,
        window_days=window_days,
        windows=windows,
    )
    conclusion_index = build_conclusion_index(
        evaluations,
        instrument,
        trusted_generation=bundle.run_id or "",
        window_days=window_days,
        windows=windows,
    )
    result = StrategyLadderResult(
        symbol=bundle.symbol.inst_id,
        run_id=bundle.run_id,
        data_files=(bundle.files_by_interval["15m"], bundle.files_by_interval["1h"]),
        instrument=instrument,
        evaluations=evaluations,
        conclusion_index=conclusion_index,
    )
    report = render_strategy_ladder_report(result)
    dashboard = render_strategy_ladder_html(result)

    for path in (report_path, html_report_path, conclusion_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8", newline="\n")
    html_report_path.write_text(dashboard, encoding="utf-8", newline="\n")
    write_candidate_conclusion_index(conclusion_path, conclusion_index)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the trusted cache-only strategy candidate ladder.")
    parser.add_argument("--symbol", default="MU-USDT-SWAP")
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--windows", type=int, default=2)
    parser.add_argument("--data-dir", type=Path, help="Trusted data store directory. Defaults to data/live.")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--html-report", type=Path)
    parser.add_argument(
        "--conclusion-index",
        type=Path,
    )
    parser.add_argument("--tick-size", type=Decimal)
    args = parser.parse_args()
    try:
        instrument = build_cli_instrument(args.symbol, args.tick_size)
        report_path, html_report_path, conclusion_path = build_cli_output_paths(
            instrument.inst_id,
            report_path=args.report,
            html_report_path=args.html_report,
            conclusion_path=args.conclusion_index,
        )
        result = run_strategy_ladder(
            symbol=args.symbol,
            window_days=args.window_days,
            windows=args.windows,
            data_dir=args.data_dir or Path("data/live"),
            report_path=report_path,
            html_report_path=html_report_path,
            conclusion_path=conclusion_path,
            instrument=instrument,
        )
    except (StrategyLadderDataError, StrategyLadderOutputError, ValueError) as exc:
        parser.error(str(exc))
    print(render_strategy_ladder_report(result))


def _close_long_only_trade(
    fill: Fill,
    *,
    exit_time_ms: int,
    exit_price: float,
    equity: float,
    fee_rate: float,
    reason: str,
) -> tuple[float, Trade]:
    if exit_price <= 0:
        raise ValueError("slipped exit price must be positive")
    gross_pnl = (exit_price - fill.price) * fill.units
    exit_fee = exit_price * fill.units * fee_rate
    fees = fill.fee + exit_fee
    net_pnl = gross_pnl - fees
    trade = Trade(
        entry_time_ms=fill.time_ms,
        exit_time_ms=exit_time_ms,
        entry_price=fill.price,
        exit_price=exit_price,
        fills=[fill],
        pnl=net_pnl,
        fees=fees,
        return_pct=net_pnl / fill.notional if fill.notional else 0.0,
        max_stage=1,
        exit_reason=reason,
    )
    return equity + net_pnl, trade


def build_cli_instrument(symbol: str, tick_size: Decimal | None) -> OKXInstrumentSpec:
    resolved = resolve_okx_swap_symbol(symbol)
    if tick_size is None:
        if resolved.inst_id != DEFAULT_MU_INSTRUMENT.inst_id:
            raise ValueError("--tick-size is required when --symbol is not MU-USDT-SWAP")
        tick_size = DEFAULT_MU_INSTRUMENT.tick_size
    return OKXInstrumentSpec(resolved.inst_id, tick_size, Decimal("1"), Decimal("1"))


def build_cli_output_paths(
    symbol: str,
    *,
    report_path: Path | None,
    html_report_path: Path | None,
    conclusion_path: Path | None,
) -> tuple[Path, Path, Path]:
    resolved = resolve_okx_swap_symbol(symbol)
    if resolved.inst_id == DEFAULT_MU_INSTRUMENT.inst_id:
        defaults = (DEFAULT_REPORT_PATH, DEFAULT_HTML_REPORT_PATH, DEFAULT_CONCLUSION_PATH)
    else:
        stem = resolved.inst_id.lower().replace("-", "_")
        defaults = (
            Path(f"reports/live/{stem}_strategy_ladder.md"),
            Path(f"reports/live/{stem}_strategy_ladder.html"),
            Path(f"reports/live/{stem}_strategy_ladder_conclusions.json"),
        )
    return tuple(
        supplied or default
        for supplied, default in zip((report_path, html_report_path, conclusion_path), defaults)
    )


def _slippage_cost_at(trades: list[Trade], time_ms: int, slip: float) -> float:
    cost = 0.0
    for trade in trades:
        if time_ms >= trade.exit_time_ms:
            cost += 2 * slip * sum(fill.units for fill in trade.fills)
            continue
        cost += slip * sum(fill.units for fill in trade.fills if fill.time_ms <= time_ms)
    return cost


def _candidate_robustness(evaluation: CandidateEvaluation) -> CandidateRobustness:
    summary = evaluation.default_cell.summary
    concentration = trade_concentration(summary.trades, top_n=TOP_N_TRADES)
    total_return_pct = _decimal_metric(summary.total_return_pct)
    return CandidateRobustness(
        candidate_id=evaluation.definition.candidate_id,
        total_return_pct=total_return_pct,
        max_drawdown_pct=_decimal_metric(summary.max_drawdown_pct),
        trade_count=summary.trade_count,
        win_rate=_decimal_metric(summary.win_rate),
        top_n=TOP_N_TRADES,
        top_n_trade_concentration=(
            _decimal_metric(concentration.top_n_share_of_net_pnl)
            if Decimal(total_return_pct) > 0 and concentration.top_n_share_of_net_pnl is not None
            else None
        ),
        stress_grid_returns=tuple(
            StressCellReturn(
                fee_bps_per_side=cell.fee_bps_per_side,
                slippage_ticks=cell.slippage_ticks,
                total_return_pct=_decimal_metric(cell.summary.total_return_pct),
            )
            for cell in evaluation.stress_grid
        ),
        survives_stress_grid=evaluation.survives_stress_grid,
    )


def _candidate_report_lines(evaluation: CandidateEvaluation) -> list[str]:
    lines = [
        "",
        f"## {evaluation.definition.candidate_id}",
        "",
        f"- family: {evaluation.definition.family}",
        f"- source: {evaluation.definition.source}",
        f"- Configured leverage: {evaluation.configured_leverage:g}x",
        f"- stress status: {'candidate' if evaluation.survives_stress_grid else 'stress_failed'}",
        "",
        "### Per-window results (default cost)",
        "",
        "| Window | UTC range | Candles | Account return | Max drawdown | Trades | Win rate |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for window in evaluation.default_cell.windows:
        lines.append(
            f"| {window.index} | {_format_window(window)} | {window.candle_count} | "
            f"{window.result.total_return_pct:.4%} | {window.result.max_drawdown_pct:.4%} | "
            f"{window.result.trade_count} | {window.result.win_rate:.4%} |"
        )
    lines.extend([
        "",
        "### Fee × slippage stress grid",
        "",
        "| Fee (bp/side) | Slippage (ticks/side) | Account return | Max drawdown | Trades | Win rate |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    for cell in evaluation.stress_grid:
        lines.append(
            f"| {cell.fee_bps_per_side} | {cell.slippage_ticks} | {cell.summary.total_return_pct:.4%} | "
            f"{cell.summary.max_drawdown_pct:.4%} | {cell.summary.trade_count} | {cell.summary.win_rate:.4%} |"
        )
    return lines


def _ranking_html_row(rank: int, evaluation: CandidateEvaluation) -> str:
    summary = evaluation.default_cell.summary
    concentration = trade_concentration(summary.trades, top_n=TOP_N_TRADES)
    return (
        "<tr>"
        f"<td class=\"num\">{rank}</td><td>{html.escape(evaluation.definition.candidate_id)}</td>"
        f"<td>{html.escape(evaluation.definition.family)}</td>"
        f"<td class=\"num\">{evaluation.configured_leverage:g}x</td>"
        f"<td class=\"num\">{summary.total_return_pct:.4%}</td>"
        f"<td class=\"num\">{summary.max_drawdown_pct:.4%}</td>"
        f"<td class=\"num\">{summary.trade_count}</td><td class=\"num\">{summary.win_rate:.4%}</td>"
        f"<td class=\"num\">{_format_optional_pct(concentration.top_n_share_of_net_pnl)}</td>"
        f"<td class=\"{'good' if evaluation.survives_stress_grid else 'bad'}\">"
        f"{'candidate' if evaluation.survives_stress_grid else 'stress_failed'}</td></tr>"
    )


def _candidate_html_section(evaluation: CandidateEvaluation) -> str:
    window_rows = "".join(
        f"<tr><td class=\"num\">{window.index}</td><td>{html.escape(_format_window(window))}</td>"
        f"<td class=\"num\">{window.candle_count}</td><td class=\"num\">{window.result.total_return_pct:.4%}</td>"
        f"<td class=\"num\">{window.result.max_drawdown_pct:.4%}</td>"
        f"<td class=\"num\">{window.result.trade_count}</td><td class=\"num\">{window.result.win_rate:.4%}</td></tr>"
        for window in evaluation.default_cell.windows
    )
    stress_rows = "".join(
        f"<tr><td class=\"num\">{cell.fee_bps_per_side}</td><td class=\"num\">{cell.slippage_ticks}</td>"
        f"<td class=\"num\">{cell.summary.total_return_pct:.4%}</td>"
        f"<td class=\"num\">{cell.summary.max_drawdown_pct:.4%}</td>"
        f"<td class=\"num\">{cell.summary.trade_count}</td><td class=\"num\">{cell.summary.win_rate:.4%}</td></tr>"
        for cell in evaluation.stress_grid
    )
    return (
        f"<h2>{html.escape(evaluation.definition.candidate_id)}</h2>"
        f"<div class=\"meta\">{html.escape(evaluation.definition.label)} · Source: {html.escape(evaluation.definition.source)} · Configured leverage: {evaluation.configured_leverage:g}x</div>"
        "<h3>Per-window results (default cost)</h3><div class=\"panel\"><table><thead><tr>"
        "<th>Window</th><th>UTC range</th><th>Candles</th><th>Account return</th><th>Max drawdown</th><th>Trades</th><th>Win rate</th>"
        f"</tr></thead><tbody>{window_rows}</tbody></table></div>"
        "<h3>Fee × slippage stress grid</h3><div class=\"panel\"><table><thead><tr>"
        "<th>Fee (bp/side)</th><th>Slippage (ticks/side)</th><th>Account return</th><th>Max drawdown</th><th>Trades</th><th>Win rate</th>"
        f"</tr></thead><tbody>{stress_rows}</tbody></table></div>"
    )


def _format_window(window: WindowBacktest) -> str:
    if window.end_time_ms <= window.start_time_ms:
        return "-"
    start = datetime.fromtimestamp(window.start_time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(window.end_time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"{start} ~ {end}"


def _format_optional_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.4%}"


def _decimal_metric(value: float) -> str:
    return format_candidate_metric(value)


def _infer_interval_ms(candles: list[Candle]) -> int:
    if len(candles) < 2:
        return 0
    diffs = [
        candles[index].open_time_ms - candles[index - 1].open_time_ms
        for index in range(1, len(candles))
        if candles[index].open_time_ms > candles[index - 1].open_time_ms
    ]
    return min(diffs) if diffs else 0


def _validate_output_paths(
    *,
    data_dir: Path,
    report_path: Path,
    html_report_path: Path,
    conclusion_path: Path,
) -> tuple[Path, Path, Path]:
    data_root = Path(data_dir).resolve()
    try:
        outputs = tuple(
            validate_candidate_artifact_path(path)
            for path in (report_path, html_report_path, conclusion_path)
        )
    except CandidateConclusionError as exc:
        raise StrategyLadderOutputError(str(exc)) from exc
    if len(set(outputs)) != len(outputs):
        raise StrategyLadderOutputError("strategy ladder output paths must be distinct")
    if any(path == data_root or path.is_relative_to(data_root) for path in outputs):
        raise StrategyLadderOutputError("strategy ladder outputs cannot write inside the trusted data store")
    return outputs


if __name__ == "__main__":
    main()
