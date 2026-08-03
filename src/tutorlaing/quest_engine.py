from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .engine import normalize
from .quest_content import QuestChoice, QuestNode


@dataclass(frozen=True)
class QuestTransition:
    next_node: str
    outcome: str
    feedback_ru: str
    points: float
    state: dict[str, str]
    reference_answer: str = ""


def choose(
    node: QuestNode, choice_id: str, current_state: dict[str, Any]
) -> QuestTransition:
    if node.mode != "choice":
        raise ValueError(f"Quest node does not accept a choice: {node.id}")
    choice = next((item for item in node.choices if item.id == choice_id), None)
    if choice is None:
        raise KeyError(f"Quest choice unavailable: {choice_id}")
    return _choice_transition(choice, current_state)


def answer_free(
    node: QuestNode, response: str, current_state: dict[str, Any]
) -> QuestTransition:
    if node.mode != "free":
        raise ValueError(f"Quest node does not accept text: {node.id}")
    answer = f" {normalize(response)} "
    matched = 0
    for group in node.expected_groups:
        if any(f" {normalize(variant)} " in answer for variant in group):
            matched += 1
    score = matched / len(node.expected_groups)
    successful = score >= 0.6
    state = {str(key): str(value) for key, value in current_state.items()}
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


def _choice_transition(
    choice: QuestChoice, current_state: dict[str, Any]
) -> QuestTransition:
    state = {str(key): str(value) for key, value in current_state.items()}
    state.update(choice.effects)
    return QuestTransition(
        next_node=choice.next_node,
        outcome=choice.outcome,
        feedback_ru=choice.feedback_ru,
        points=choice.points,
        state=state,
    )
