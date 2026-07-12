from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from mu_strategy.canonical import canonical_json
from mu_strategy.research.strategy_releases import (
    ReleaseDecision,
    ScmReviewSnapshotV1,
    StrategyReleaseApprovalV1,
    StrategyReleaseCandidateV1,
    StrategyReleaseV1,
)


class ScmReviewVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class LiveScmReview:
    scm_provider: str
    repository: str
    pull_request_number: int
    review_record_id: str
    reviewer_id: str
    reviewed_at_ms: int
    decision: ReleaseDecision
    statement: str
    review_url: str


class ScmReviewProvider(Protocol):
    def current_actor_id(self) -> str: ...

    def fetch_review(
        self,
        *,
        repository: str,
        pull_request_number: int,
        review_record_id: str,
    ) -> LiveScmReview | None: ...


def approval_statement(candidate: StrategyReleaseCandidateV1) -> str:
    return "\n".join(
        (
            "APPROVED_STRATEGY_RELEASE_V1",
            f"candidate_fingerprint={candidate.candidate_fingerprint}",
            f"evaluated_code_commit_sha={candidate.evaluated_code_commit_sha}",
        )
    )


def capture_verified_approval(
    candidate: StrategyReleaseCandidateV1,
    *,
    repository: str,
    pull_request_number: int,
    review_record_id: str,
    provider: ScmReviewProvider,
) -> StrategyReleaseApprovalV1:
    author_id = provider.current_actor_id()
    live = provider.fetch_review(
        repository=repository,
        pull_request_number=pull_request_number,
        review_record_id=review_record_id,
    )
    if live is None:
        raise ScmReviewVerificationError("SCM review record is missing or deleted")
    if (
        live.repository != repository
        or live.pull_request_number != pull_request_number
        or live.review_record_id != review_record_id
    ):
        raise ScmReviewVerificationError("SCM review coordinates do not match the requested record")
    if not author_id or live.reviewer_id == author_id:
        raise ScmReviewVerificationError("SCM reviewer must be independent from the release author")
    if live.decision is not ReleaseDecision.APPROVED:
        raise ScmReviewVerificationError("SCM review decision is not APPROVED")
    if live.statement != approval_statement(candidate):
        raise ScmReviewVerificationError("SCM review statement does not bind the exact candidate and implementation")

    snapshot = ScmReviewSnapshotV1.create(
        scm_provider=live.scm_provider,
        repository=live.repository,
        pull_request_number=live.pull_request_number,
        review_record_id=live.review_record_id,
        reviewer_id=live.reviewer_id,
        author_id=author_id,
        reviewed_at_ms=live.reviewed_at_ms,
        decision=live.decision,
        candidate_fingerprint=candidate.candidate_fingerprint,
        evaluated_code_commit_sha=candidate.evaluated_code_commit_sha,
        statement=live.statement,
        review_url=live.review_url,
    )
    return StrategyReleaseApprovalV1.create(review_snapshot=snapshot)


def promote_strategy_release(
    candidate_path: Path,
    *,
    release_dir: Path,
    repository: str,
    pull_request_number: int,
    review_record_id: str,
    provider: ScmReviewProvider,
) -> tuple[StrategyReleaseV1, Path]:
    try:
        candidate = StrategyReleaseCandidateV1.from_dict(
            json.loads(candidate_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ScmReviewVerificationError(f"invalid candidate artifact: {exc}") from exc
    approval = capture_verified_approval(
        candidate,
        repository=repository,
        pull_request_number=pull_request_number,
        review_record_id=review_record_id,
        provider=provider,
    )
    release = StrategyReleaseV1.create(candidate=candidate, approval=approval)
    output_path = release_dir / f"{release.strategy_release_id}.json"
    _atomic_write_text(output_path, canonical_json(release.to_dict()))
    return release, output_path


class GitHubCliScmReviewProvider:
    def current_actor_id(self) -> str:
        payload = self._gh_json("user")
        return _required_text(payload, "login")

    def fetch_review(
        self,
        *,
        repository: str,
        pull_request_number: int,
        review_record_id: str,
    ) -> LiveScmReview | None:
        endpoint = f"repos/{repository}/pulls/{pull_request_number}/reviews/{review_record_id}"
        try:
            payload = self._gh_json(endpoint)
        except subprocess.CalledProcessError as exc:
            if "HTTP 404" in exc.stderr:
                return None
            raise
        state = _required_text(payload, "state")
        decision = ReleaseDecision.APPROVED if state == "APPROVED" else ReleaseDecision.REJECTED
        submitted_at = _required_text(payload, "submitted_at")
        reviewed_at_ms = int(datetime.fromisoformat(submitted_at.replace("Z", "+00:00")).timestamp() * 1000)
        user = payload.get("user")
        if not isinstance(user, dict):
            raise ScmReviewVerificationError("SCM review user is missing")
        return LiveScmReview(
            scm_provider="github",
            repository=repository,
            pull_request_number=pull_request_number,
            review_record_id=str(payload.get("id", "")),
            reviewer_id=_required_text(user, "login"),
            reviewed_at_ms=reviewed_at_ms,
            decision=decision,
            statement=_required_text(payload, "body"),
            review_url=_required_text(payload, "html_url"),
        )

    @staticmethod
    def _gh_json(endpoint: str) -> dict:
        completed = subprocess.run(
            ("gh", "api", endpoint),
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ScmReviewVerificationError("SCM response must be an object")
        return payload


def _required_text(payload: dict, field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ScmReviewVerificationError(f"SCM {field_name} must be non-empty text")
    return value


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
    parser = argparse.ArgumentParser(description="Promote a reviewed strategy release candidate.")
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request-number", required=True, type=int)
    parser.add_argument("--review-record-id", required=True)
    parser.add_argument("--release-dir", type=Path, default=Path("config/strategy-releases"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    release, output_path = promote_strategy_release(
        args.candidate,
        release_dir=args.release_dir,
        repository=args.repository,
        pull_request_number=args.pull_request_number,
        review_record_id=args.review_record_id,
        provider=GitHubCliScmReviewProvider(),
    )
    print(f"strategy_release_id={release.strategy_release_id}")
    print(f"output_path={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
