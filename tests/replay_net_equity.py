"""Pinned real-data acceptance for #121 (standard library; no network or data writes).

Run `python -B tests/replay_net_equity.py pair --help`. Each source is imported
in a fresh process. Export wraps real fill/mark/settlement functions only to
observe accounting; fee-only is the explicitly labelled old-sampling diagnostic.
Full Trade/Fill and curve evidence is written to the requested ignored directory.
"""

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import inspect
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


def same(a, b, path="root"):
    if isinstance(a, float):
        assert close(a, b), (path, a, b)
    elif isinstance(a, dict):
        assert a.keys() == b.keys(), path
        for key in a:
            same(a[key], b[key], f"{path}.{key}")
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for i, (left, right) in enumerate(zip(a, b)):
            same(left, right, f"{path}[{i}]")
    else:
        assert a == b, (path, a, b)


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
    gross_realized = 0.0
    fees = 0.0
    latest_event = bars[0].open_time_ms
    samples = []

    def sample(label, kind, value, unrealized=0.0, open_fees=0.0):
        assert latest_event <= label, ("future event leaked into mark", latest_event, label)
        samples.append(dict(time_ms=label, kind=kind, value=value,
                            gross_realized=gross_realized, unrealized=unrealized,
                            incurred_fees=fees, open_entry_fees=open_fees,
                            accounting_net=10000 + gross_realized + unrealized - fees))

    sample(bars[0].open_time_ms, "initial", 10000.0)
    original_fill = engine._make_fill
    original_mark = engine._marked_equity
    original_exit = engine._close_position

    def fill(*values):
        nonlocal fees, latest_event
        result = original_fill(*values)
        fees += result.fee
        latest_event = result.time_ms
        return result

    def mark(equity, position, price):
        result = original_mark(equity, position, price)
        if args.mode == "fee-only" and position is not None:
            result -= position.fees
        caller = inspect.currentframe().f_back
        kind = "close" if args.mode != "after" or caller.f_code.co_name == "record_close" else "fill"
        label = caller.f_locals["candle"].open_time_ms if kind == "close" else position.fills[-1].time_ms
        unrealized = sum((price - f.price) * f.units for f in position.fills) if position else 0.0
        sample(label, kind, result, unrealized, position.fees if position else 0.0)
        return result

    def settle(position, candle, price, equity, reason, cfg):
        nonlocal gross_realized, fees, latest_event
        result = original_exit(position, candle, price, equity, reason, cfg)
        gross_realized += sum((price - f.price) * f.units for f in position.fills)
        fees += price * sum(f.units for f in position.fills) * cfg.fee_rate
        latest_event = candle.open_time_ms
        sample(candle.open_time_ms, "settlement" if reason == "end_of_data" else "exit", result[0])
        return result

    engine._make_fill, engine._marked_equity, engine._close_position = fill, mark, settle
    result = engine.run_backtest(bars, context, config=config)
    assert [(s["time_ms"], s["value"]) for s in samples] == result.equity_curve
    assert sorted(t for t, _ in result.equity_curve) == [t for t, _ in result.equity_curve]
    if args.mode == "after":
        assert [s["time_ms"] for s in samples if s["kind"] in ("initial", "close")] == [b.open_time_ms for b in bars]
        for s in samples:
            assert close(s["value"], s["accounting_net"]), s
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
    for mode, source in (("before", args.before_source), ("fee-only", args.before_source), ("after", args.after_source)):
        output = args.output_dir / f"{mode}.json"
        subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "export", "--source", str(source.resolve()),
                        "--data-dir", str(args.data_dir.resolve()), "--mode", mode, "--output", str(output.resolve())], check=True)
        runs[mode] = json.loads(output.read_text(encoding="utf-8"))
    before, fee_only, after = (runs[m] for m in ("before", "fee-only", "after"))
    for other in (fee_only, after):
        same(before["result"]["trades"], other["result"]["trades"], "all Trade/Fill fields")
        same(before["result"]["ending_equity"], other["result"]["ending_equity"], "ending equity")
        assert before["data_files"] == other["data_files"]
        assert before["environment"] == other["environment"]
        for key, value in before["provenance"].items():
            if key != "code_sha256":
                assert value == other["provenance"][key], key
    assert (before["summary"]["trades"], before["summary"]["buy_fills"]) == (57, 106)
    same(before["summary"]["ending_equity"], 29399.55932527934)
    same(before["summary"]["fees"], 2507.3578334614494)
    same(before["summary"]["max_drawdown_pct"], -24.185950262931833)

    def keyed(samples):
        counts = Counter()
        result = {}
        for s in samples:
            key = (s["time_ms"], s["kind"])
            counts[key] += 1
            result[(*key, counts[key])] = s
        return result

    old, diagnostic, new = (keyed(runs[m]["samples"]) for m in ("before", "fee-only", "after"))
    assert old.keys() == diagnostic.keys()
    assert old.keys() <= new.keys(), "old meaningful sample omitted"
    for key, s in diagnostic.items():
        same(s["value"], new[key]["value"], f"retained sample {key}")
    changed = [dict(before=s, after=new[k]) for k, s in old.items() if not close(s["value"], new[k]["value"])]
    added = [s for k, s in new.items() if k not in old]
    comparison = dict(
        tolerances=dict(abs_tol=ABS_TOL, rel_tol=REL_TOL, discrete="exact"),
        summaries={mode: run["summary"] for mode, run in runs.items()},
        hashes={mode: run["hashes"] for mode, run in runs.items()},
        exact_trade_equality=before["result"]["trades"] == after["result"]["trades"],
        exact_ending_equity_equality=before["result"]["ending_equity"] == after["result"]["ending_equity"],
        retained_samples=len(old), fee_changed_samples=len(changed),
        added_samples=len(added), added_by_kind=dict(Counter(s["kind"] for s in added)),
        first_fee_difference=changed[0] if changed else None,
        first_added_sample=added[0] if added else None,
        max_previously_unrecognized_entry_fees=max(s["open_entry_fees"] for s in before["samples"]),
        max_accounting_residual=max(abs(s["value"] - s["accounting_net"]) for s in after["samples"]),
        terminal_samples=after["samples"][-3:])
    (args.output_dir / "comparison.json").write_text(json.dumps(comparison, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(comparison, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("export")
    worker.add_argument("--source", type=Path, required=True)
    worker.add_argument("--data-dir", type=Path, required=True)
    worker.add_argument("--mode", choices=("before", "fee-only", "after"), required=True)
    worker.add_argument("--output", type=Path, required=True)
    paired = sub.add_parser("pair")
    paired.add_argument("--before-source", type=Path, required=True)
    paired.add_argument("--after-source", type=Path, required=True)
    paired.add_argument("--data-dir", type=Path, required=True)
    paired.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    (export if arguments.command == "export" else pair)(arguments)
