from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def rank_candidates(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for row in rows:
        candidate = dict(row)
        total_return_pct = float(candidate.get("total_return_pct", candidate.get("total_return", 0.0)))
        max_drawdown_pct = float(candidate.get("max_drawdown_pct", candidate.get("max_drawdown", 0.0)))
        profit_factor = float(candidate.get("profit_factor", 0.0))
        candidate["score"] = total_return_pct + max_drawdown_pct + (profit_factor * 0.01)
        ranked.append(candidate)
    return sorted(
        ranked,
        key=lambda row: (
            row["score"],
            float(row.get("total_return_pct", row.get("total_return", 0.0))),
            float(row.get("profit_factor", 0.0)),
        ),
        reverse=True,
    )
