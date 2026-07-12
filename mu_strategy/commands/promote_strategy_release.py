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


TRUSTED_SCM_REPOSITORY = "amazing-fish/mu-5x-fibonacci-trading"


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
    includes_created_edit: bool
    last_edited_at_ms: int | None


@dataclass(frozen=True)
class LiveScmCommit:
    sha: str
    author_id: str | None
    committer_id: str | None


@dataclass(frozen=True)
class LiveScmPullRequest:
    repository: str
    pull_request_number: int
    author_id: str
    commits: tuple[LiveScmCommit, ...]


class ScmReviewProvider(Protocol):
    def fetch_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
    ) -> LiveScmPullRequest | None: ...

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
    if repository != TRUSTED_SCM_REPOSITORY:
        raise ScmReviewVerificationError("promotion requires the trusted repository")
    pull_request = provider.fetch_pull_request(
        repository=repository,
        pull_request_number=pull_request_number,
    )
    if pull_request is None:
        raise ScmReviewVerificationError("SCM pull request is missing or deleted")
    if (
        pull_request.repository != repository
        or pull_request.pull_request_number != pull_request_number
    ):
        raise ScmReviewVerificationError("SCM pull request coordinates do not match")
    evaluated_commit = next(
        (commit for commit in pull_request.commits if commit.sha == candidate.evaluated_code_commit_sha),
        None,
    )
    if evaluated_commit is None:
        raise ScmReviewVerificationError("SCM pull request does not contain the evaluated commit")
    if not evaluated_commit.author_id or not evaluated_commit.committer_id:
        raise ScmReviewVerificationError("SCM evaluated commit identity is incomplete")
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
    authors = {
        pull_request.author_id,
        evaluated_commit.author_id,
        evaluated_commit.committer_id,
    }
    if not pull_request.author_id or live.reviewer_id in authors:
        raise ScmReviewVerificationError("SCM reviewer must be independent from the release author")
    if live.decision is not ReleaseDecision.APPROVED:
        raise ScmReviewVerificationError("SCM review decision is not APPROVED")
    if live.includes_created_edit:
        raise ScmReviewVerificationError("SCM review statement was edited after creation")
    if live.last_edited_at_ms is not None:
        raise ScmReviewVerificationError("SCM review statement has a last-edited timestamp")
    if live.statement != approval_statement(candidate):
        raise ScmReviewVerificationError("SCM review statement does not bind the exact candidate and implementation")

    snapshot = ScmReviewSnapshotV1.create(
        scm_provider=live.scm_provider,
        repository=live.repository,
        pull_request_number=live.pull_request_number,
        review_record_id=live.review_record_id,
        reviewer_id=live.reviewer_id,
        author_id=evaluated_commit.author_id,
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
    def fetch_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
    ) -> LiveScmPullRequest | None:
        endpoint = f"repos/{repository}/pulls/{pull_request_number}"
        try:
            payload = self._gh_json(endpoint)
            commits = self._gh_paginated_json_list(f"{endpoint}/commits?per_page=100")
        except subprocess.CalledProcessError as exc:
            if "HTTP 404" in exc.stderr:
                return None
            raise
        user = payload.get("user")
        if not isinstance(user, dict):
            raise ScmReviewVerificationError("SCM pull request user is missing")
        commit_records = tuple(
            LiveScmCommit(
                sha=_required_text(item, "sha"),
                author_id=_optional_actor_login(item.get("author")),
                committer_id=_optional_actor_login(item.get("committer")),
            )
            for item in commits
        )
        if not commit_records:
            raise ScmReviewVerificationError("SCM pull request commits are missing")
        return LiveScmPullRequest(
            repository=repository,
            pull_request_number=pull_request_number,
            author_id=_required_text(user, "login"),
            commits=commit_records,
        )

    def fetch_review(
        self,
        *,
        repository: str,
        pull_request_number: int,
        review_record_id: str,
    ) -> LiveScmReview | None:
        endpoint = f"repos/{repository}/pulls/{pull_request_number}/reviews/{review_record_id}"
        try:
            rest_payload = self._gh_json(endpoint)
        except subprocess.CalledProcessError as exc:
            if "HTTP 404" in exc.stderr:
                return None
            raise
        payload = self._gh_graphql_review(_required_text(rest_payload, "node_id"))
        state = _required_text(payload, "state")
        decision = ReleaseDecision.APPROVED if state == "APPROVED" else ReleaseDecision.REJECTED
        submitted_at = _required_text(payload, "submittedAt")
        reviewed_at_ms = int(datetime.fromisoformat(submitted_at.replace("Z", "+00:00")).timestamp() * 1000)
        user = payload.get("author")
        if not isinstance(user, dict):
            raise ScmReviewVerificationError("SCM review user is missing")
        return LiveScmReview(
            scm_provider="github",
            repository=repository,
            pull_request_number=pull_request_number,
            review_record_id=str(_required_int(payload, "databaseId")),
            reviewer_id=_required_text(user, "login"),
            reviewed_at_ms=reviewed_at_ms,
            decision=decision,
            statement=_required_text(payload, "body"),
            review_url=_required_text(payload, "url"),
            includes_created_edit=_required_bool(payload, "includesCreatedEdit"),
            last_edited_at_ms=_required_nullable_datetime_ms(payload, "lastEditedAt"),
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

    @staticmethod
    def _gh_paginated_json_list(endpoint: str) -> list[dict]:
        completed = subprocess.run(
            ("gh", "api", "--paginate", "--slurp", endpoint),
            check=True,
            capture_output=True,
            text=True,
        )
        pages = json.loads(completed.stdout)
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise ScmReviewVerificationError("SCM paginated response must contain list pages")
        items = [item for page in pages for item in page]
        if not all(isinstance(item, dict) for item in items):
            raise ScmReviewVerificationError("SCM paginated response items must be objects")
        return items

    @staticmethod
    def _gh_graphql_review(node_id: str) -> dict:
        query = """
        query($id: ID!) {
          node(id: $id) {
            ... on PullRequestReview {
              databaseId
              body
              state
              submittedAt
              url
              includesCreatedEdit
              lastEditedAt
              author { login }
            }
          }
        }
        """
        completed = subprocess.run(
            ("gh", "api", "graphql", "-f", f"query={query}", "-F", f"id={node_id}"),
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise ScmReviewVerificationError("SCM GraphQL errors prevent review verification")
        data = payload.get("data") if isinstance(payload, dict) else None
        node = data.get("node") if isinstance(data, dict) else None
        if not isinstance(node, dict):
            raise ScmReviewVerificationError("SCM GraphQL review node is missing")
        return node


def _required_text(payload: dict, field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ScmReviewVerificationError(f"SCM {field_name} must be non-empty text")
    return value


def _required_int(payload: dict, field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScmReviewVerificationError(f"SCM {field_name} must be an integer")
    return value


def _required_bool(payload: dict, field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise ScmReviewVerificationError(f"SCM {field_name} must be a boolean")
    return value


def _optional_actor_login(payload: object) -> str | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ScmReviewVerificationError("SCM commit actor must be an object or null")
    return _required_text(payload, "login")


def _required_nullable_datetime_ms(payload: dict, field_name: str) -> int | None:
    if field_name not in payload:
        raise ScmReviewVerificationError(f"SCM {field_name} is required")
    value = payload[field_name]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ScmReviewVerificationError(f"SCM {field_name} must be an ISO timestamp or null")
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError as exc:
        raise ScmReviewVerificationError(f"SCM {field_name} must be an ISO timestamp or null") from exc


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
