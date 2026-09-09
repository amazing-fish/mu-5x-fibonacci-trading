"""Post-#122 behavior equivalence (standard library; no network or data writes).

Each source runs the unmodified real entry point in a fresh process. Audit uses
only input candles and returned Trade/Fill/curve records, never engine frames or
observers. `pair` compares the entire result exactly, including duplicate labels.
The #121 three-way fee-only diagnostic remains historical evidence at commit
1fc366c; do not apply it to data whose net equity already includes entry fees.
"""

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys


GENERATION = "f694d1d9a3fd46daa8d0dc12cb12aa0b"
ABS_TOL = 1e-9
REL_TOL = 1e-12


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def file_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def close(a, b):
    return math.isclose(a, b, abs_tol=ABS_TOL, rel_tol=REL_TOL)


def same(a, b, path="root", *, exact=False):
    if isinstance(a, float):
        assert (a == b if exact else close(a, b)), (path, a, b)
    elif isinstance(a, dict):
        assert a.keys() == b.keys(), path
        for key in a:
            same(a[key], b[key], f"{path}.{key}", exact=exact)
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for i, (left, right) in enumerate(zip(a, b)):
            same(left, right, f"{path}[{i}]", exact=exact)
    else:
        assert a == b, (path, a, b)


def audit_result(bars, result, fee_rate):
    """Reconcile the documented post-#122 sampling contract from public output.

    This ledger does not decide whether/when to enter, add or exit. Actual fills
    and exits come exclusively from returned trades. Within each candle label:
    fills, risk exit, observed close, then any terminal settlement. Equal labels
    and equal values are deliberately retained as separate observations.
    """
    if len(bars) < 4:
        assert not result.trades and not result.equity_curve
        assert result.starting_equity == result.ending_equity
        return []
    events = {}
    for trade in result.trades:
        for fill in trade.fills:
            events.setdefault(fill.time_ms, []).append(("fill", fill))
        kind = "settlement" if trade.exit_reason == "end_of_data" else "exit"
        events.setdefault(trade.exit_time_ms, []).append((kind, trade))
    gross_realized = 0.0
    fees = 0.0
    open_fills = []
    samples = []

    def sample(label, kind, price):
        unrealized = sum((price - f.price) * f.units for f in open_fills)
        samples.append(dict(time_ms=label, kind=kind, gross_realized=gross_realized,
                            unrealized=unrealized, incurred_fees=fees,
                            open_entry_fees=sum(f.fee for f in open_fills),
                            accounting_net=result.starting_equity + gross_realized + unrealized - fees))

    def settle(trade):
        nonlocal gross_realized, fees
        assert open_fills == trade.fills, "exit must settle exactly the executed fills"
        gross = sum((trade.exit_price - f.price) * f.units for f in open_fills)
        exit_fee = trade.exit_price * sum(f.units for f in open_fills) * fee_rate
        same(trade.fees, sum(f.fee for f in open_fills) + exit_fee, "trade fees")
        same(trade.pnl, gross - trade.fees, "net trade PnL")
        gross_realized += gross
        fees += exit_fee
        open_fills.clear()

    sample(bars[0].open_time_ms, "initial", bars[0].close)
    for candle in bars[1:]:
        terminal = []
        for kind, event in events.pop(candle.open_time_ms, []):
            if kind == "settlement":
                assert candle == bars[-1], "end_of_data must be on the last candle"
                terminal.append(event)
            elif kind == "fill":
                same(event.fee, event.notional * fee_rate, "fill fee")
                open_fills.append(event)
                fees += event.fee
                sample(candle.open_time_ms, kind, event.price)
            else:
                settle(event)
                sample(candle.open_time_ms, kind, event.exit_price)
        sample(candle.open_time_ms, "close", candle.close)
        for trade in terminal:
            settle(trade)
            sample(candle.open_time_ms, "settlement", trade.exit_price)
    assert not events and not open_fills, "unaccounted event or unsettled position"
    assert len(samples) == len(result.equity_curve), (len(samples), len(result.equity_curve))
    for index, (s, (label, value)) in enumerate(zip(samples, result.equity_curve)):
        assert s["time_ms"] == label, (index, s, label)
        same(s["accounting_net"], value, f"sample[{index}]")
        s["value"] = value
    same(fees, sum(t.fees for t in result.trades), "fee conservation")
    same(result.starting_equity + gross_realized - fees, result.ending_equity, "ending ledger")
    return samples


def export(args):
    source = args.source.resolve()
    sys.path.insert(0, str(source))
    import mu_strategy.backtest as engine
    from mu_strategy.core.market_context import build_hourly_context
    from mu_strategy.research.historical_data import load_historical_window
    from mu_strategy.research.robustness import trade_concentration, stage_distribution
    from mu_strategy.research.strategy_releases import StrategyConfigPayloadV2
    from mu_strategy.strategies.registry import selected_strategy_groups

    data_before = file_hashes(args.data_dir)
    window = load_historical_window(data_dir=args.data_dir, generation_id=GENERATION,
                                    symbol="MU-USDT-SWAP", days=179)
    assert (window.start_ms, window.end_ms) == (1773334800000, 1788800400000)
    bars = list(window.candles_by_interval["15m"])
    context = build_hourly_context(bars, list(window.candles_by_interval["1h"]))
    config = selected_strategy_groups("MU-USDT-SWAP", ["baseline"])[0].config
    assert (config.leverage, config.fee_rate, config.margin_steps) == (5, .0005, (.2, .2, .2, .4))
    result = engine.run_backtest(bars, context, config=config)
    samples = audit_result(bars, result, config.fee_rate)
    data_after = file_hashes(args.data_dir)
    assert data_before == data_after, "data changed during read-only replay"
    config_payload = dict(strategy="baseline", strategy_config=StrategyConfigPayloadV2.from_config(config).to_dict(),
                          days=179, starting_equity=10000, slippage="not modeled", partial_fills="not modeled")
    payload = dict(mode=args.mode, source_head=subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True).strip(),
        environment=dict(python=sys.version, executable=sys.executable, platform=platform.platform()),
        provenance=json.loads(window.provenance(config_payload)), data_files=data_after,
        result=asdict(result), samples=samples,
        summary=dict(return_pct=result.total_return_pct * 100, ending_equity=result.ending_equity,
                     max_drawdown_pct=result.max_drawdown_pct * 100, trades=result.trade_count,
                     buy_fills=sum(len(t.fills) for t in result.trades), fees=sum(t.fees for t in result.trades),
                     win_rate_pct=result.win_rate * 100, curve_points=len(result.equity_curve),
                     concentration=asdict(trade_concentration(result.trades)),
                     stages=asdict(stage_distribution(result.trades))),
        hashes=dict(trades=digest(asdict(result)["trades"]), curve=digest(result.equity_curve)))
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))


def pair(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runs = {}
    for mode, source in (("before", args.before_source), ("after", args.after_source)):
        output = args.output_dir / f"{mode}.json"
        subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "export", "--source", str(source.resolve()),
                        "--data-dir", str(args.data_dir.resolve()), "--mode", mode, "--output", str(output.resolve())], check=True)
        runs[mode] = json.loads(output.read_text(encoding="utf-8"))
    before, after = runs["before"], runs["after"]
    same(before["result"], after["result"], "complete BacktestResult", exact=True)
    same(before["summary"], after["summary"], "derived summary", exact=True)
    assert before["data_files"] == after["data_files"]
    assert before["environment"] == after["environment"]
    for key in before["provenance"].keys() | after["provenance"].keys():
        if key != "code_sha256":
            assert before["provenance"][key] == after["provenance"][key], key
    assert before["hashes"] == after["hashes"] == dict(
        trades="5ff2b75c6fcec5b8c30335380dcddc1e773c282fc9a7c083fa2bd6bac78336f9",
        curve="1e853d61937a960f4f4f86ea23ba76e32d3d92b0c7a1d0995ea2694e2e561bbe")
    summary = before["summary"]
    assert (summary["trades"], summary["buy_fills"], summary["curve_points"]) == (57, 106, 17347)
    assert summary["ending_equity"] == 29399.55932527934
    assert summary["fees"] == 2507.3578334614494
    assert summary["max_drawdown_pct"] == -24.188368164054207
    comparison = dict(
        comparison="exact complete result and summary; no tolerance used for equivalence",
        accounting_tolerances=dict(abs_tol=ABS_TOL, rel_tol=REL_TOL),
        source_heads={mode: run["source_head"] for mode, run in runs.items()},
        code_sha256={mode: run["provenance"]["code_sha256"] for mode, run in runs.items()},
        summaries={mode: run["summary"] for mode, run in runs.items()},
        hashes={mode: run["hashes"] for mode, run in runs.items()},
        exact_result_equality=True,
        max_accounting_residual={mode: max(abs(s["value"] - s["accounting_net"]) for s in run["samples"])
                                 for mode, run in runs.items()},
        terminal_samples=after["samples"][-3:])
    (args.output_dir / "comparison.json").write_text(json.dumps(comparison, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(comparison, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("export")
    worker.add_argument("--source", type=Path, required=True)
    worker.add_argument("--data-dir", type=Path, required=True)
    worker.add_argument("--mode", choices=("before", "after"), required=True)
    worker.add_argument("--output", type=Path, required=True)
    paired = sub.add_parser("pair")
    paired.add_argument("--before-source", type=Path, required=True)
    paired.add_argument("--after-source", type=Path, required=True)
    paired.add_argument("--data-dir", type=Path, required=True)
    paired.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    (export if arguments.command == "export" else pair)(arguments)
