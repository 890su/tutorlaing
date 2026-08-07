"""Learner context and adaptation preferences.

The profile is deliberately separate from Telegram transport state.  It contains
only information that improves task selection and may later be supplied by a
different client without changing learning flows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


AGE_BANDS = ("unset", "under_18", "18_24", "25_34", "35_49", "50_plus")
LIFE_ROLES = ("unset", "studying", "working", "both", "other")


class LearnerProfileStore(Protocol):
    def learner_profile(self, chat_id: int) -> Any: ...

    def update_learner_profile(self, chat_id: int, **values: Any) -> Any: ...

    def event(
        self,
        chat_id: int | None,
        event_name: str,
        properties: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class LearnerProfile:
    age_band: str = "unset"
    life_role: str = "unset"
    weekly_context: str = ""
    current_goal: str = ""
    adaptive_level_enabled: bool = True


class LearnerProfileService:
    """Validated application boundary for optional learner context."""

    def __init__(self, store: LearnerProfileStore):
        self.store = store

    def get(self, chat_id: int) -> LearnerProfile:
        row = self.store.learner_profile(chat_id)
        return LearnerProfile(
            age_band=str(row["age_band"]),
            life_role=str(row["life_role"]),
            weekly_context=str(row["weekly_context"] or ""),
            current_goal=str(row["current_goal"] or ""),
            adaptive_level_enabled=bool(row["adaptive_level_enabled"]),
        )

    def set_age_band(self, chat_id: int, value: str) -> LearnerProfile:
        if value not in AGE_BANDS:
            raise ValueError(f"Unsupported age band: {value}")
        return self._update(chat_id, "age_band", value)

    def set_life_role(self, chat_id: int, value: str) -> LearnerProfile:
        if value not in LIFE_ROLES:
            raise ValueError(f"Unsupported life role: {value}")
        return self._update(chat_id, "life_role", value)

    def set_weekly_context(self, chat_id: int, value: str) -> LearnerProfile:
        return self._update(chat_id, "weekly_context", self._clean_text(value))

    def set_current_goal(self, chat_id: int, value: str) -> LearnerProfile:
        return self._update(chat_id, "current_goal", self._clean_text(value))

    def set_adaptive_level(self, chat_id: int, enabled: bool) -> LearnerProfile:
        return self._update(chat_id, "adaptive_level_enabled", int(enabled))

    def _update(self, chat_id: int, field: str, value: Any) -> LearnerProfile:
        self.store.update_learner_profile(chat_id, **{field: value})
        self.store.event(
            chat_id,
            "learner_profile_updated",
            {"field": field, "has_value": bool(value)},
        )
        return self.get(chat_id)

    @staticmethod
    def _clean_text(value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("Profile text must not be empty")
        return cleaned[:1000]
