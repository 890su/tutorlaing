from __future__ import annotations

from dataclasses import dataclass
from typing import Any


LEVELS = ("A0", "A1", "A2", "B1", "B2", "C1")


@dataclass(frozen=True)
class LevelPolicy:
    level: str
    item_difficulty: int
    choice_items: int
    ai_instruction: str


POLICIES = {
    "A0": LevelPolicy(
        "A0",
        1,
        3,
        "Use recognition and very short survival phrases. Show strong scaffolding and accept a correct two-to-five-word reply.",
    ),
    "A1": LevelPolicy(
        "A1",
        1,
        3,
        "Use one short practical sentence, high-frequency vocabulary and visible phrase scaffolding.",
    ),
    "A2": LevelPolicy(
        "A2",
        2,
        2,
        "Use short independent replies, common inflection and one simple variation of the learned phrase.",
    ),
    "B1": LevelPolicy(
        "B1",
        2,
        2,
        "Require one or two independent sentences, a relevant detail, and transfer to a nearby real-life context.",
    ),
    "B2": LevelPolicy(
        "B2",
        3,
        1,
        "Require natural register, paraphrasing and precise form choices without displaying the target phrase as a scaffold.",
    ),
    "C1": LevelPolicy(
        "C1",
        3,
        1,
        "Test nuance, pragmatic register, reformulation and compact idiomatic production with minimal scaffolding.",
    ),
}


def level_policy(level: str) -> LevelPolicy:
    return POLICIES.get(level, POLICIES["A1"])


def shifted_level(level: str, offset: int) -> str:
    """Apply a temporary practice challenge without changing the CEFR profile."""
    current = LEVELS.index(level) if level in LEVELS else LEVELS.index("A1")
    return LEVELS[max(0, min(len(LEVELS) - 1, current + max(-1, min(1, offset))))]


def practice_level(user: Any) -> str:
    return shifted_level(
        str(user["learner_level"]), int(user["practice_difficulty_offset"] or 0)
    )
