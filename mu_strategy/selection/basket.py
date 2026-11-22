from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

MAX_PROFIT_FACTOR_FOR_SCORE = 10.0


def _finite_float(value: Any, *, default: float = 0.0) -> float:
    number = float(value)
    return number if math.isfinite(number) else default


def _bounded_profit_factor(value: Any) -> float:
    number = float(value)
    if math.isnan(number) or number < 0.0:
        return 0.0
    if math.isinf(number):
        return MAX_PROFIT_FACTOR_FOR_SCORE
    return min(number, MAX_PROFIT_FACTOR_FOR_SCORE)


def rank_candidates(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for row in rows:
        candidate = dict(row)
        total_return_pct = _finite_float(candidate.get("total_return_pct", candidate.get("total_return", 0.0)))
        max_drawdown_pct = _finite_float(candidate.get("max_drawdown_pct", candidate.get("max_drawdown", 0.0)))
        bounded_pf = _bounded_profit_factor(candidate.get("profit_factor", 0.0))
        candidate["score"] = total_return_pct + max_drawdown_pct + (bounded_pf * 0.01)
        ranked.append(candidate)
    return sorted(
        ranked,
        key=lambda row: (
            row["score"],
            _finite_float(row.get("total_return_pct", row.get("total_return", 0.0))),
            _bounded_profit_factor(row.get("profit_factor", 0.0)),
        ),
        reverse=True,
    )
