from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .difficulty import shifted_level


MIN_DOWN_WEIGHT = 10.0
MIN_UP_WEIGHT = 12.0
MIN_DOWN_PRODUCTION = 6
MIN_UP_PRODUCTION = 8
COOLDOWN_DAYS = 7


class AdaptiveDifficultyStore(Protocol):
    def difficulty_evidence(self, chat_id: int, limit: int = 40) -> dict[str, Any]: ...

    def pending_difficulty_proposal(self, chat_id: int) -> Any | None: ...

    def difficulty_cooldown_until(self, chat_id: int) -> str | None: ...

    def save_skill_states(
        self, chat_id: int, target_language: str, states: list[dict[str, Any]]
    ) -> None: ...

    def create_difficulty_proposal(
        self,
        chat_id: int,
        target_language: str,
        direction: int,
        skill: str,
        evidence: dict[str, Any],
    ) -> Any: ...

    def resolve_difficulty_proposal(
        self, chat_id: int, proposal_id: int, accepted: bool, cooldown_until: datetime
    ) -> Any: ...


@dataclass(frozen=True)
class SkillSnapshot:
    skill: str
    attempts: int
    weighted_attempts: float
    average_score: float
    severe_rate: float
    hint_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "attempts": self.attempts,
            "weighted_attempts": self.weighted_attempts,
            "average_score": self.average_score,
            "severe_rate": self.severe_rate,
            "hint_rate": self.hint_rate,
        }


@dataclass(frozen=True)
class DifficultyProposal:
    id: int
    direction: int
    skill: str
    average_score: float
    severe_rate: float
    hint_rate: float
    production_attempts: int
    distinct_days: int


class AdaptiveDifficultyService:
    """Makes conservative, explainable practice-level suggestions."""

    def __init__(self, store: AdaptiveDifficultyStore):
        self.store = store

    def assess(self, chat_id: int) -> DifficultyProposal | None:
        raw = self.store.difficulty_evidence(chat_id)
        if not bool(raw.get("adaptive_level_enabled", True)):
            return None
        pending = self.store.pending_difficulty_proposal(chat_id)
        if pending is not None:
            return self._proposal_from_row(pending)
        cooldown = self.store.difficulty_cooldown_until(chat_id)
        if cooldown and datetime.fromisoformat(cooldown) > datetime.now(timezone.utc):
            return None

        # Do not ratchet the temporary challenge repeatedly. After accepting a
        # recommendation, the learner keeps that working level until changing
        # the profile level or a later explicit recalibration flow.
        if int(raw.get("practice_offset", 0)) != 0:
            return None
        attempts = raw["attempts"]
        if not attempts:
            return None
        states = self._skill_states(attempts)
        self.store.save_skill_states(chat_id, raw["target_language"], [s.to_dict() for s in states])
        total = self._aggregate(attempts)
        direction = self._direction(total)
        if direction == 0:
            return None
        if shifted_level(str(raw["profile_level"]), direction) == str(
            raw["profile_level"]
        ):
            return None
        skill = (
            min(states, key=lambda state: state.average_score).skill
            if direction < 0 and states
            else "mixed_production"
        )
        evidence = {
            **total,
            "skills": [state.to_dict() for state in states],
            "rule_version": "adaptive-v1",
        }
        row = self.store.create_difficulty_proposal(
            chat_id,
            raw["target_language"],
            direction,
            skill,
            evidence,
        )
        return self._proposal_from_row(row)

    def resolve(self, chat_id: int, proposal_id: int, accepted: bool) -> Any:
        return self.store.resolve_difficulty_proposal(
            chat_id,
            proposal_id,
            accepted,
            datetime.now(timezone.utc) + timedelta(days=COOLDOWN_DAYS),
        )

    @staticmethod
    def _direction(total: dict[str, Any]) -> int:
        if (
            total["weighted_attempts"] >= MIN_UP_WEIGHT
            and total["production_attempts"] >= MIN_UP_PRODUCTION
            and total["distinct_days"] >= 2
            and total["average_score"] >= 0.9
            and total["severe_rate"] <= 0.05
            and total["hint_rate"] <= 0.1
        ):
            return 1
        if (
            total["weighted_attempts"] >= MIN_DOWN_WEIGHT
            and total["production_attempts"] >= MIN_DOWN_PRODUCTION
            and total["distinct_days"] >= 2
            and (
                total["average_score"] < 0.55
                or total["severe_rate"] > 0.3
            )
        ):
            return -1
        return 0

    @staticmethod
    def _aggregate(attempts: list[dict[str, Any]]) -> dict[str, Any]:
        weighted = 0.0
        score_sum = 0.0
        severe = 0.0
        hinted = 0.0
        production = 0
        days: set[str] = set()
        for item in attempts:
            weight = float(item["weight"])
            score = max(0.0, min(1.0, float(item["score"])))
            weighted += weight
            score_sum += score * weight
            severe += weight if score < 0.4 else 0.0
            hinted += weight if item.get("hinted") else 0.0
            production += int(bool(item.get("production")))
            days.add(str(item["occurred_at"])[:10])
        return {
            "weighted_attempts": round(weighted, 3),
            "average_score": round(score_sum / weighted, 3) if weighted else 0.0,
            "severe_rate": round(severe / weighted, 3) if weighted else 0.0,
            "hint_rate": round(hinted / weighted, 3) if weighted else 0.0,
            "production_attempts": production,
            "distinct_days": len(days),
        }

    @classmethod
    def _skill_states(cls, attempts: list[dict[str, Any]]) -> list[SkillSnapshot]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in attempts:
            grouped.setdefault(str(item["skill"] or "general"), []).append(item)
        states = []
        for skill, values in grouped.items():
            total = cls._aggregate(values)
            states.append(
                SkillSnapshot(
                    skill=skill,
                    attempts=len(values),
                    weighted_attempts=total["weighted_attempts"],
                    average_score=total["average_score"],
                    severe_rate=total["severe_rate"],
                    hint_rate=total["hint_rate"],
                )
            )
        return states

    @staticmethod
    def _proposal_from_row(row: Any) -> DifficultyProposal:
        import json

        evidence = json.loads(row["evidence_json"])
        return DifficultyProposal(
            id=int(row["id"]),
            direction=int(row["direction"]),
            skill=str(row["skill"]),
            average_score=float(evidence["average_score"]),
            severe_rate=float(evidence["severe_rate"]),
            hint_rate=float(evidence["hint_rate"]),
            production_attempts=int(evidence["production_attempts"]),
            distinct_days=int(evidence["distinct_days"]),
        )
