from __future__ import annotations

from mu_strategy.observations import (
    ObservationCycleInvalidError,
    ObservationRepository,
    Stage0ObservationCycle,
)


def persist_observation_cycle(repository: ObservationRepository, cycle: Stage0ObservationCycle) -> None:
    """Persist already classified outcomes without evaluating or changing them."""
    try:
        repository.append_cycle(cycle)
    except Exception as exc:
        raise ObservationCycleInvalidError(
            "observation persistence failed; cycle is not promotion evidence"
        ) from exc
