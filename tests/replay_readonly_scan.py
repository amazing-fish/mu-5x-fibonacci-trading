"""Exact before/after scan evidence using temporary publications and strict readers.

Run `pair --before-source PATH --after-source PATH --output-dir PATH` with Python
3.12. Both processes use the same temporary input paths; no fields, timestamps,
IDs, diagnostics or order are removed from the comparison. No real market cache,
account, refresh provider or SMTP connection is used.
"""

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


CASES = ("fresh", "ready", "strategy_block", "missing_cache", "corrupt_cache", "stale",
         "missing_manifest", "invalid_manifest", "scanner_error", "unknown", "invalid",
         "generation_switch", "write_error")
SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")


def digest_files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def capture(data_root):
    from mu_strategy.demo_trading import DemoTradingConfig, run_once
    from mu_strategy.market_data.trusted_data.contracts import SystemClock
    from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle
    from mu_strategy.market_data.trusted_data.store import TrustedDataStore
    from mu_strategy.models import EntryDecisionCode
    from mu_strategy.scan_cycle import ScanCycle
    from mu_strategy.signal_service import ServiceConfig, scan_once
    from tests.factories.scan_cycle import scan_result
    from tests.factories.trusted_publication import write_generation_manifest_and_caches

    outputs = {}
    for case in CASES:
        outputs[case] = {}
        for surface in ("service", "demo"):
            root = data_root / case / surface
            root.mkdir(parents=True, exist_ok=True)
            # Re-create only these test-owned fixture files for the second process.
            (root / "current.json").unlink(missing_ok=True)
            for generation in ("run-1", "run-2"):
                for index, symbol in enumerate(SYMBOLS):
                    write_generation_manifest_and_caches(root, symbol=symbol, days=2, run_id=generation,
                                                         universe_symbols=SYMBOLS if index == len(SYMBOLS) - 1 else ())
            store = TrustedDataStore(data_dir=root)
            store.replace_current("run-1")
            if case == "missing_manifest":
                (root / "current.json").unlink()
            elif case == "invalid_manifest":
                (root / "generations" / "run-1" / "manifest.json").write_text("{", encoding="utf-8")
            elif case == "missing_cache":
                store.generation_cache_path("run-1", SYMBOLS[0], "1h").unlink()
            elif case == "corrupt_cache":
                path = store.generation_cache_path("run-1", SYMBOLS[0], "15m")
                path.write_text(path.read_text(encoding="utf-8").replace("100.0", "100.5", 1), encoding="utf-8")
            market_before = digest_files(root)
            events, recorded = [], []
            clock_values = iter(range(172_800_000 + (864_000_000 if case == "stale" else 0), 1_900_000_000))
            ids = iter(("cycle", "observation-1", "observation-2"))
            repository = Mock()

            def append(cycle):
                recorded.append(cycle.to_dict())
                events.append(["persist", cycle.cycle_id])
                if case == "write_error":
                    raise OSError("disk full")
            repository.append_cycle.side_effect = append

            original_scan = ScanCycle.scan_symbol
            original_init = ScanCycle.__init__
            original_load = LoadTrustedBundle.execute
            def initialize(cycle, **kwargs):
                original_init(cycle, **dict(kwargs, id_factory=ids.__next__))
            def load(reader, query, policy, **kwargs):
                result = original_load(reader, query, policy, **kwargs)
                events.append(["load", query.symbol, result.run_id])
                return result

            def scan(cycle, **kwargs):
                original_scanner = kwargs["scanner"]
                def scanner(symbol, *args, **query):
                    events.append(["scan", symbol])
                    if case == "generation_switch" and symbol == SYMBOLS[0]:
                        store.replace_current("run-2")
                    if case == "scanner_error":
                        raise RuntimeError("api_key=private-marker scanner unavailable")
                    if case == "invalid":
                        return None
                    if case == "fresh":
                        return original_scanner(symbol, *args, **query)
                    code = {"strategy_block": EntryDecisionCode.REGIME_BLOCKED,
                            "unknown": EntryDecisionCode.UNKNOWN}.get(case, EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY)
                    return scan_result(code, symbol=symbol)
                return original_scan(cycle, **dict(kwargs, scanner=scanner))

            with ExitStack() as guards:
                guards.enter_context(patch.object(SystemClock, "now_ms", side_effect=lambda: next(clock_values)))
                guards.enter_context(patch.object(ScanCycle, "__init__", autospec=True, side_effect=initialize))
                guards.enter_context(patch.object(LoadTrustedBundle, "execute", autospec=True, side_effect=load))
                guards.enter_context(patch.object(ScanCycle, "scan_symbol", autospec=True, side_effect=scan))
                blocked = [guards.enter_context(patch(target, side_effect=AssertionError(target))) for target in (
                    "socket.create_connection", "urllib.request.urlopen", "smtplib.SMTP", "smtplib.SMTP_SSL",
                    "mu_strategy.live.okx.OKXCredentials.from_env",
                    "mu_strategy.market_data.trusted_data.refresh.RefreshTrustedMarketData.execute",
                )]
                health = payload = exception = None
                try:
                    if surface == "service":
                        health = scan_once(ServiceConfig(data_dir=root, scan_days=2,
                                           symbols=(SYMBOLS[0], "BTC", SYMBOLS[1])), repository=repository).to_dict()
                    else:
                        payload = run_once(DemoTradingConfig(data_dir=root, days=2, universe_limit=2,
                                           watchlist_symbols=("BTC", SYMBOLS[1], SYMBOLS[0])),
                                           broker=None, observation_repository=repository)
                except Exception as exc:
                    exception = [type(exc).__name__, str(exc),
                                 type(exc.__cause__).__name__ if exc.__cause__ else None,
                                 str(exc.__cause__) if exc.__cause__ else None]
                for guard in blocked:
                    guard.assert_not_called()
            assert len(recorded) == 1, (case, surface, exception, health)
            expected_count = 0 if surface == "demo" and case in {"missing_manifest", "invalid_manifest"} else 2
            assert len(recorded[0]["observations"]) == expected_count, (case, surface)
            if surface == "service":
                assert health["status"] == "succeeded", (case, health)
                assert health["persistence"] == ("failed" if case == "write_error" else "succeeded")
                assert health["cycle"] == recorded[0]
            if case != "write_error" or surface == "service":
                assert exception is None, (case, surface, exception)
            if case == "generation_switch":
                assert [item[2] for item in events if item[0] == "load"] == ["run-1", "run-1"]
                assert all(row["trusted_run_id"] == "run-1" for row in recorded[0]["observations"])
            market_after = digest_files(root)
            if case == "generation_switch":
                assert {k: v for k, v in market_before.items() if k != "current.json"} == {
                    k: v for k, v in market_after.items() if k != "current.json"}
            else:
                assert market_before == market_after, (case, surface, "scan wrote market data")
            outputs[case][surface] = dict(cycles=recorded, health=health, payload=payload, exception=exception,
                                           events=events, market_before=market_before, market_after=market_after)
    return outputs


def pair(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    with TemporaryDirectory(prefix="mu-readonly-pair-") as directory:
        for label, source in (("before", args.before_source), ("after", args.after_source)):
            output = args.output_dir / f"{label}.json"
            subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "export", "--source", str(source.resolve()),
                            "--data-dir", directory, "--output", str(output.resolve())], check=True)
            outputs.append(json.loads(output.read_text(encoding="utf-8")))
    assert outputs[0]["cases"] == outputs[1]["cases"], "full outputs differ; inspect before.json and after.json"
    assert outputs[0]["environment"] == outputs[1]["environment"]
    result = dict(exact_equal=True, cases=len(CASES), surfaces=2, environment=outputs[1]["environment"],
                  source_heads=[item["source_head"] for item in outputs])
    (args.output_dir / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--data-dir", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    paired = commands.add_parser("pair")
    paired.add_argument("--before-source", type=Path, required=True)
    paired.add_argument("--after-source", type=Path, required=True)
    paired.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "pair":
        pair(args)
    else:
        sys.path.insert(0, str(args.source.resolve()))
        value = dict(source_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.source, text=True).strip(),
                     source_files={name: hashlib.sha256((args.source / name).read_bytes()).hexdigest() for name in (
                         "mu_strategy/demo_trading.py", "mu_strategy/signal_service.py", "mu_strategy/readonly_scan.py",
                         "mu_strategy/market_data/service.py", "mu_strategy/market_data/trusted_data/load.py",
                     ) if (args.source / name).is_file()},
                     environment=dict(platform=platform.platform(), python=platform.python_version()),
                     cases=capture(args.data_dir.resolve()))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
