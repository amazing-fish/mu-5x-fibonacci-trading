from __future__ import annotations

import argparse
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from mu_strategy.canonical import canonical_json
from mu_strategy.experiments.release_candidate import (
    HistoricalTrustedGenerationReader,
    run_release_experiment,
)
from mu_strategy.research.strategy_releases import (
    EXPERIMENT_PROTOCOL_ID,
    BacktestAssumptionsV1,
    ExperimentWindow,
    ExperimentWindowRole,
    FillModel,
    PartialFillModel,
    SelectionReasonCode,
    StrategyConfigPayloadV1,
    StrategyReleaseCandidateV1,
)
from mu_strategy.strategies.registry import baseline_strategy_group


@dataclass(frozen=True)
class GitState:
    head_sha: str
    is_clean: bool


class GenerationReader(Protocol):
    def read(self, *, run_id: str, symbol: str): ...


GitStateProvider = Callable[[Path], GitState]


@dataclass(frozen=True)
class CandidateGenerationRequest:
    repository_root: Path
    data_dir: Path
    run_id: str
    symbol: str
    evaluated_code_commit_sha: str
    windows: tuple[ExperimentWindow, ...]
    output_path: Path | None = None


def build_strategy_release_candidate(
    request: CandidateGenerationRequest,
    *,
    git_state_provider: GitStateProvider | None = None,
    generation_reader: GenerationReader | None = None,
) -> tuple[StrategyReleaseCandidateV1, Path]:
    provider = git_state_provider or read_git_state
    state = provider(request.repository_root)
    if not state.is_clean:
        raise ValueError("candidate generation requires a clean worktree")
    if state.head_sha != request.evaluated_code_commit_sha:
        raise ValueError("current HEAD must exactly equal evaluated_code_commit_sha")

    group = baseline_strategy_group(request.symbol)
    config_payload = StrategyConfigPayloadV1.from_config(group.config)
    assumptions = BacktestAssumptionsV1(
        starting_equity="10000",
        fee_profile=group.config.fee_profile,
        fee_rate=config_payload.values["fee_rate"],
        fill_model=FillModel.DETERMINISTIC_OHLC,
        slippage_bps="0",
        partial_fill_model=PartialFillModel.NONE,
    )
    reader = generation_reader or HistoricalTrustedGenerationReader(data_dir=request.data_dir)
    generation = reader.read(run_id=request.run_id, symbol=request.symbol)
    results = run_release_experiment(
        generation,
        config=group.config,
        windows=request.windows,
        assumptions=assumptions,
    )
    candidate = StrategyReleaseCandidateV1.create(
        strategy_rule_id=group.rule.strategy_rule_id,
        strategy_name=group.rule.strategy_name,
        supported_symbols=(request.symbol,),
        strategy_config=config_payload,
        evaluated_code_commit_sha=request.evaluated_code_commit_sha,
        dataset=generation.reference,
        windows=request.windows,
        experiment_protocol_id=EXPERIMENT_PROTOCOL_ID,
        assumptions=assumptions,
        results=results,
        selection_reason=SelectionReasonCode.REVALIDATED_BASELINE,
    )
    output_path = request.output_path or (
        request.repository_root
        / "data"
        / "strategy-release-candidates"
        / f"{candidate.candidate_fingerprint}.json"
    )
    _atomic_write_text(output_path, canonical_json(candidate.to_dict()))
    return candidate, output_path


def read_git_state(repository_root: Path) -> GitState:
    head = _run_git(repository_root, "rev-parse", "HEAD")
    status = _run_git(repository_root, "status", "--porcelain", "--untracked-files=normal")
    return GitState(head_sha=head, is_clean=not status)


def _run_git(repository_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a non-promoted strategy release candidate.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--evaluated-code-commit-sha", required=True)
    parser.add_argument("--train-start-ms", required=True, type=int)
    parser.add_argument("--train-end-ms", required=True, type=int)
    parser.add_argument("--validation-end-ms", required=True, type=int)
    parser.add_argument("--oos-end-ms", required=True, type=int)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repository_root = args.repository_root.resolve()
    windows = (
        ExperimentWindow(
            ExperimentWindowRole.TRAIN,
            args.train_start_ms,
            args.train_start_ms,
            args.train_end_ms,
        ),
        ExperimentWindow(
            ExperimentWindowRole.VALIDATION,
            args.train_end_ms,
            args.train_end_ms,
            args.validation_end_ms,
        ),
        ExperimentWindow(
            ExperimentWindowRole.OUT_OF_SAMPLE,
            args.validation_end_ms,
            args.validation_end_ms,
            args.oos_end_ms,
        ),
    )
    request = CandidateGenerationRequest(
        repository_root=repository_root,
        data_dir=(args.data_dir or repository_root / "data" / "live").resolve(),
        run_id=args.run_id,
        symbol=args.symbol,
        evaluated_code_commit_sha=args.evaluated_code_commit_sha,
        windows=windows,
        output_path=args.output.resolve() if args.output else None,
    )
    candidate, output_path = build_strategy_release_candidate(request)
    print(f"candidate_fingerprint={candidate.candidate_fingerprint}")
    print(f"result_fingerprint={candidate.result_fingerprint}")
    print(f"output_path={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
