"""Read model for resumable learning activities.

Legacy flow tables remain the source of truth during the migration.  This
application service projects them into one stable contract for Telegram and
future clients, so presentation code does not need to understand every table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ActivityStore(Protocol):
    def get_user(self, chat_id: int) -> Any: ...

    def active_scenario_sessions(self, chat_id: int) -> list[Any]: ...

    def active_quest_sessions(self, chat_id: int) -> list[Any]: ...

    def active_drill_sessions(self, chat_id: int) -> list[Any]: ...


@dataclass(frozen=True)
class LearningActivity:
    kind: str
    session_id: str
    content_id: str
    title_hint: str
    target_language: str
    current: int
    total: int | None
    position_label: str
    is_foreground: bool
    started_at: str


class ActivityService:
    """Return all open work while keeping exactly one foreground activity."""

    def __init__(self, store: ActivityStore):
        self.store = store

    def list_open(self, chat_id: int) -> list[LearningActivity]:
        user = self.store.get_user(chat_id)
        activities: list[LearningActivity] = []
        for row in self.store.active_scenario_sessions(chat_id):
            current = max(0, int(row["current_step"]))
            activities.append(
                LearningActivity(
                    kind="scenario",
                    session_id=str(row["id"]),
                    content_id=str(row["scenario_id"]),
                    title_hint=str(row["scenario_id"]),
                    target_language=str(row["target_language"]),
                    current=current,
                    total=None,
                    position_label=str(current + 1),
                    is_foreground=(
                        str(user["current_session"] or "") == str(row["id"])
                    ),
                    started_at=str(row["started_at"]),
                )
            )
        for row in self.store.active_quest_sessions(chat_id):
            steps = max(0, int(row["steps_taken"]))
            activities.append(
                LearningActivity(
                    kind="quest",
                    session_id=str(row["id"]),
                    content_id=str(row["quest_id"]),
                    title_hint=str(row["quest_id"]),
                    target_language=str(row["target_language"]),
                    current=steps,
                    total=None,
                    position_label=str(row["current_node"]),
                    is_foreground=(
                        str(user["current_quest"] or "") == str(row["id"])
                    ),
                    started_at=str(row["started_at"]),
                )
            )
        for row in self.store.active_drill_sessions(chat_id):
            current = max(0, int(row["current_index"]))
            total = max(1, int(row["total_items"]))
            activities.append(
                LearningActivity(
                    kind="drill",
                    session_id=str(row["id"]),
                    content_id=str(row["mode"]),
                    title_hint=str(row["title"]),
                    target_language=str(row["target_language"]),
                    current=current,
                    total=total,
                    position_label=f"{min(current + 1, total)}/{total}",
                    is_foreground=(
                        str(user["current_drill"] or "") == str(row["id"])
                    ),
                    started_at=str(row["started_at"]),
                )
            )
        return sorted(
            activities,
            key=lambda activity: (activity.is_foreground, activity.started_at),
            reverse=True,
        )
