import json
import unittest
from datetime import datetime, timezone
from typing import Any

from tutorlaing.adaptive_difficulty import AdaptiveDifficultyService
from tutorlaing.difficulty import practice_level, shifted_level


class FakeDifficultyStore:
    def __init__(self, attempts: list[dict[str, Any]], *, level: str = "B1"):
        self.attempts = attempts
        self.level = level
        self.offset = 0
        self.pending: dict[str, Any] | None = None
        self.cooldown: str | None = None
        self.states: list[dict[str, Any]] = []

    def difficulty_evidence(self, _chat_id: int, limit: int = 40) -> dict[str, Any]:
        return {
            "profile_level": self.level,
            "practice_offset": self.offset,
            "target_language": "pl",
            "attempts": self.attempts[-limit:],
        }

    def pending_difficulty_proposal(self, _chat_id: int) -> Any | None:
        return self.pending

    def difficulty_cooldown_until(self, _chat_id: int) -> str | None:
        return self.cooldown

    def save_skill_states(
        self, _chat_id: int, _target_language: str, states: list[dict[str, Any]]
    ) -> None:
        self.states = states

    def create_difficulty_proposal(
        self,
        _chat_id: int,
        _target_language: str,
        direction: int,
        skill: str,
        evidence: dict[str, Any],
    ) -> Any:
        self.pending = {
            "id": 7,
            "direction": direction,
            "skill": skill,
            "evidence_json": json.dumps(evidence),
        }
        return self.pending

    def resolve_difficulty_proposal(
        self,
        _chat_id: int,
        _proposal_id: int,
        accepted: bool,
        cooldown_until: datetime,
    ) -> Any:
        if self.pending is None:
            raise KeyError("stale")
        if accepted:
            self.offset = int(self.pending["direction"])
        self.cooldown = cooldown_until.isoformat()
        result = self.pending
        self.pending = None
        return result


def evidence(
    count: int, score: float, *, hinted: int = 0, skill: str = "production"
) -> list[dict[str, Any]]:
    return [
        {
            "skill": skill,
            "score": score,
            "weight": 1.0,
            "production": True,
            "hinted": index < hinted,
            "occurred_at": f"2026-08-{1 + index % 2:02d}T10:00:00+00:00",
        }
        for index in range(count)
    ]


class AdaptiveDifficultyTests(unittest.TestCase):
    def test_does_not_react_to_a_short_run(self) -> None:
        store = FakeDifficultyStore(evidence(6, 1.0))
        self.assertIsNone(AdaptiveDifficultyService(store).assess(1))
        self.assertTrue(store.states)

    def test_suggests_higher_working_level_after_strong_unassisted_recall(self) -> None:
        store = FakeDifficultyStore(evidence(12, 0.96))
        proposal = AdaptiveDifficultyService(store).assess(1)
        self.assertIsNotNone(proposal)
        self.assertEqual(1, proposal.direction)
        self.assertEqual(2, proposal.distinct_days)

    def test_hints_prevent_premature_raise(self) -> None:
        store = FakeDifficultyStore(evidence(12, 0.96, hinted=2))
        self.assertIsNone(AdaptiveDifficultyService(store).assess(1))

    def test_suggests_lower_working_level_after_sustained_severe_errors(self) -> None:
        store = FakeDifficultyStore(evidence(10, 0.3, skill="cases"))
        proposal = AdaptiveDifficultyService(store).assess(1)
        self.assertIsNotNone(proposal)
        self.assertEqual(-1, proposal.direction)
        self.assertEqual("cases", proposal.skill)

    def test_accepted_suggestion_changes_practice_not_profile_level(self) -> None:
        store = FakeDifficultyStore(evidence(12, 0.95), level="B1")
        service = AdaptiveDifficultyService(store)
        proposal = service.assess(1)
        service.resolve(1, proposal.id, True)
        user = {"learner_level": store.level, "practice_difficulty_offset": store.offset}
        self.assertEqual("B1", user["learner_level"])
        self.assertEqual("B2", practice_level(user))
        self.assertIsNone(service.assess(1))

    def test_boundary_level_is_not_offered_an_impossible_raise(self) -> None:
        store = FakeDifficultyStore(evidence(12, 1.0), level="C1")
        self.assertIsNone(AdaptiveDifficultyService(store).assess(1))
        self.assertEqual("C1", shifted_level("C1", 1))


if __name__ == "__main__":
    unittest.main()
