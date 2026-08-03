from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .content import Scenario
from .contracts import ProgressStore
from .difficulty import shifted_level


@dataclass(frozen=True)
class ProgressSnapshot:
    level: str
    practice_level: str
    mastered: tuple[str, ...]
    focus: tuple[str, ...]
    planned: tuple[str, ...]
    first_planned_scenario_id: str | None
    total_scenarios: int


class ProgressService:
    """Derives a learner plan from stored evidence without presentation logic."""

    def __init__(self, store: ProgressStore):
        self.store = store

    def build(
        self, chat_id: int, scenarios: dict[str, Scenario]
    ) -> ProgressSnapshot:
        evidence = self.store.progress_evidence(chat_id)
        session_by_id = self._by_scenario(evidence["sessions"])
        review_by_id = self._by_scenario(evidence["reviews"])
        mastered: list[tuple[str, str]] = []
        focus: list[tuple[str, str]] = []
        untouched: list[tuple[str, str]] = []

        for scenario_id, scenario in scenarios.items():
            session = session_by_id.get(scenario_id)
            review = review_by_id.get(scenario_id)
            entry = (scenario_id, scenario.title_pl)
            if self._is_mastered(review):
                mastered.append(entry)
            elif session:
                focus.append(entry)
            else:
                untouched.append(entry)

        plan = (focus + untouched)[:3]
        return ProgressSnapshot(
            level=str(evidence["level"]),
            practice_level=shifted_level(
                str(evidence["level"]), int(evidence.get("practice_offset", 0))
            ),
            mastered=tuple(title for _, title in mastered),
            focus=tuple(title for _, title in focus),
            planned=tuple(title for _, title in plan),
            first_planned_scenario_id=plan[0][0] if plan else None,
            total_scenarios=len(scenarios),
        )

    @staticmethod
    def _by_scenario(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {str(row["scenario_id"]): row for row in rows}

    @staticmethod
    def _is_mastered(review: dict[str, Any] | None) -> bool:
        return bool(
            review
            and int(review["completed"] or 0) > 0
            and float(review["best_score"] or 0) >= 0.6
        )
