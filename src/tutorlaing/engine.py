from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .content import Scenario, ScenarioStep


_NON_WORD = re.compile(r"[^a-z0-9ąćęłńóśźż\s]+", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Normalize Polish text while accepting answers typed without diacritics."""
    lowered = text.casefold().replace("ł", "l")
    decomposed = unicodedata.normalize("NFKD", lowered)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    cleaned = _NON_WORD.sub(" ", without_marks)
    return _SPACE.sub(" ", cleaned).strip()


def _contains_variant(answer: str, variant: str) -> bool:
    normalized_variant = normalize(variant)
    if not normalized_variant:
        return False
    padded_answer = f" {answer} "
    return f" {normalized_variant} " in padded_answer


@dataclass(frozen=True)
class Evaluation:
    score: float
    matched_groups: tuple[int, ...]
    missing_groups: tuple[int, ...]

    @property
    def successful(self) -> bool:
        return self.score >= 0.6


def evaluate_response(step: ScenarioStep, response: str) -> Evaluation:
    answer = normalize(response)
    matched: list[int] = []
    missing: list[int] = []
    for index, variants in enumerate(step.expected_groups):
        if any(_contains_variant(answer, variant) for variant in variants):
            matched.append(index)
        else:
            missing.append(index)
    score = len(matched) / len(step.expected_groups) if step.expected_groups else 1.0
    return Evaluation(score, tuple(matched), tuple(missing))


def select_bottleneck(
    scenario: Scenario, step_scores: Iterable[tuple[int, float]]
) -> int:
    scores = list(step_scores)
    if not scores:
        return 0

    def priority(item: tuple[int, float]) -> tuple[float, float, int]:
        index, score = item
        blocking_bonus = 0.25 if scenario.steps[index].task_blocking else 0.0
        return (score - blocking_bonus, score, index)

    return min(scores, key=priority)[0]


def review_interval_days(score: float, previous_interval: int | None = None) -> int:
    if score < 0.5:
        return 2
    if score < 0.8:
        return 4
    if previous_interval:
        return min(30, max(7, previous_interval * 2))
    return 7


def review_due_at(
    score: float,
    previous_interval: int | None = None,
    now: datetime | None = None,
) -> datetime:
    current = now or datetime.now(timezone.utc)
    return current + timedelta(days=review_interval_days(score, previous_interval))
