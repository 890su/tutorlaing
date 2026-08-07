from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .engine import normalize
from .quest_content import QuestNode


@dataclass(frozen=True)
class QuestTransition:
    next_node: str
    outcome: str
    feedback_ru: str
    points: float
    state: dict[str, str]
    reference_answer: str = ""


def answer_free(
    node: QuestNode,
    response: str,
    current_state: dict[str, Any],
    *,
    score: float | None = None,
    successful: bool | None = None,
) -> QuestTransition:
    if node.mode != "free":
        raise ValueError(f"Quest node does not accept text: {node.id}")
    answer = f" {normalize(response)} "
    matched = 0
    for group in node.expected_groups:
        if any(f" {normalize(variant)} " in answer for variant in group):
            matched += 1
    rule_score = matched / len(node.expected_groups)
    score = rule_score if score is None else max(0.0, min(1.0, score))
    successful = score >= 0.6 if successful is None else successful
    state = {str(key): str(value) for key, value in current_state.items()}
    effects = node.success_effects if successful else node.retry_effects
    state.update({str(key): str(value) for key, value in (effects or {}).items()})
    state[f"answer:{node.id}"] = response.strip()[:500]
    return QuestTransition(
        next_node=node.success_next if successful else node.retry_next,
        outcome="good" if successful else "problem",
        feedback_ru=(
            node.success_feedback_ru if successful else node.retry_feedback_ru
        ),
        points=round(score, 3),
        state=state,
        reference_answer="" if successful else node.reference_answer,
    )
