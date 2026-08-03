from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


@dataclass(frozen=True)
class QuestChoice:
    id: str
    text: str
    next_node: str
    outcome: str
    feedback_ru: str
    points: float
    effects: dict[str, str]


@dataclass(frozen=True)
class QuestNode:
    id: str
    mode: str
    speaker: str
    message: str
    task_ru: str
    hint_ru: str
    choices: tuple[QuestChoice, ...] = ()
    expected_groups: tuple[tuple[str, ...], ...] = ()
    reference_answer: str = ""
    success_next: str = ""
    retry_next: str = ""
    success_feedback_ru: str = ""
    retry_feedback_ru: str = ""
    ending: str = ""
    summary_ru: str = ""


@dataclass(frozen=True)
class Quest:
    id: str
    title_ru: str
    title_target: str
    category: str
    goal_ru: str
    briefing_ru: str
    start_node: str
    nodes: dict[str, QuestNode]


def _choice(raw: dict[str, Any]) -> QuestChoice:
    return QuestChoice(
        id=str(raw["id"]),
        text=str(raw["text"]),
        next_node=str(raw["next_node"]),
        outcome=str(raw.get("outcome", "neutral")),
        feedback_ru=str(raw["feedback_ru"]),
        points=max(0.0, min(1.0, float(raw.get("points", 0.5)))),
        effects={str(key): str(value) for key, value in raw.get("effects", {}).items()},
    )


def _node(raw: dict[str, Any]) -> QuestNode:
    return QuestNode(
        id=str(raw["id"]),
        mode=str(raw["mode"]),
        speaker=str(raw.get("speaker", "")),
        message=str(raw.get("message", "")),
        task_ru=str(raw.get("task_ru", "")),
        hint_ru=str(raw.get("hint_ru", "")),
        choices=tuple(_choice(item) for item in raw.get("choices", [])),
        expected_groups=tuple(
            tuple(str(value) for value in group)
            for group in raw.get("expected_groups", [])
        ),
        reference_answer=str(raw.get("reference_answer", "")),
        success_next=str(raw.get("success_next", "")),
        retry_next=str(raw.get("retry_next", "")),
        success_feedback_ru=str(raw.get("success_feedback_ru", "")),
        retry_feedback_ru=str(raw.get("retry_feedback_ru", "")),
        ending=str(raw.get("ending", "")),
        summary_ru=str(raw.get("summary_ru", "")),
    )


def load_quests(target_language: str = "pl") -> dict[str, Quest]:
    if target_language != "pl":
        return {}
    path = files("tutorlaing").joinpath("content/quests.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    quests: dict[str, Quest] = {}
    for item in raw["quests"]:
        nodes = {node.id: node for node in (_node(value) for value in item["nodes"])}
        if len(nodes) != len(item["nodes"]):
            raise ValueError(f"Duplicate quest node: {item['id']}")
        quest = Quest(
            id=str(item["id"]),
            title_ru=str(item["title_ru"]),
            title_target=str(item["title_target"]),
            category=str(item["category"]),
            goal_ru=str(item["goal_ru"]),
            briefing_ru=str(item["briefing_ru"]),
            start_node=str(item["start_node"]),
            nodes=nodes,
        )
        _validate(quest)
        if quest.id in quests:
            raise ValueError(f"Duplicate quest id: {quest.id}")
        quests[quest.id] = quest
    return quests


class QuestCatalog:
    """Read-only curated quest catalog. V0 intentionally ships Polish only."""

    def __init__(self, courses: dict[str, dict[str, Quest]] | None = None):
        self._courses = courses or {"pl": load_quests("pl"), "en": {}}

    def for_language(self, target_language: str) -> dict[str, Quest]:
        return self._courses.get(target_language, {})

    def for_user(self, user: Any) -> dict[str, Quest]:
        return self.for_language(str(user["target_language"]))


def _validate(quest: Quest) -> None:
    if quest.start_node not in quest.nodes:
        raise ValueError(f"Quest start node is missing: {quest.id}")
    endings = 0
    for node in quest.nodes.values():
        if node.mode == "choice":
            if not 2 <= len(node.choices) <= 4:
                raise ValueError(f"Quest choice node needs 2-4 choices: {node.id}")
            if len({choice.id for choice in node.choices}) != len(node.choices):
                raise ValueError(f"Duplicate quest choice: {node.id}")
            destinations = [choice.next_node for choice in node.choices]
        elif node.mode == "free":
            if not node.expected_groups or not node.reference_answer:
                raise ValueError(f"Quest free node has no rubric: {node.id}")
            destinations = [node.success_next, node.retry_next]
        elif node.mode == "ending":
            endings += 1
            if node.ending not in {"success", "partial", "failure"}:
                raise ValueError(f"Invalid quest ending: {node.id}")
            destinations = []
        else:
            raise ValueError(f"Unsupported quest node mode: {node.mode}")
        missing = [value for value in destinations if value not in quest.nodes]
        if missing:
            raise ValueError(f"Unknown quest transition from {node.id}: {missing}")
    if endings < 2:
        raise ValueError(f"Quest needs branching endings: {quest.id}")
