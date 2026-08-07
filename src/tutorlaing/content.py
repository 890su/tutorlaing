from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from .learning_cards import LearningCardSeed


@dataclass(frozen=True)
class ScenarioStep:
    id: str
    interlocutor_pl: str
    context_ru: str
    hint_ru: str
    expected_groups: tuple[tuple[str, ...], ...]
    target_chunk: str
    bottleneck_ru: str
    task_blocking: bool
    learning_cards: tuple[LearningCardSeed, ...] = ()


@dataclass(frozen=True)
class Scenario:
    id: str
    title_ru: str
    title_pl: str
    category: str
    objective_ru: str
    opening_ru: str
    steps: tuple[ScenarioStep, ...]


def _step_from_dict(data: dict[str, Any]) -> ScenarioStep:
    return ScenarioStep(
        id=data["id"],
        interlocutor_pl=data["interlocutor_pl"],
        context_ru=data["context_ru"],
        hint_ru=data["hint_ru"],
        expected_groups=tuple(
            tuple(str(variant) for variant in group)
            for group in data["expected_groups"]
        ),
        target_chunk=data["target_chunk"],
        bottleneck_ru=data["bottleneck_ru"],
        task_blocking=bool(data.get("task_blocking", False)),
        learning_cards=tuple(
            LearningCardSeed.from_dict(item)
            for item in data.get("learning_cards", [])
        ),
    )


def load_scenarios(target_language: str = "pl") -> dict[str, Scenario]:
    filenames = {"pl": "scenarios.json", "en": "scenarios.en.json"}
    try:
        filename = filenames[target_language]
    except KeyError as exc:
        raise ValueError(f"Unsupported scenario language: {target_language}") from exc
    path = files("tutorlaing").joinpath(f"content/{filename}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    scenarios: dict[str, Scenario] = {}
    for item in raw["scenarios"]:
        scenario = Scenario(
            id=item["id"],
            title_ru=item["title_ru"],
            title_pl=item["title_pl"],
            category=item["category"],
            objective_ru=item["objective_ru"],
            opening_ru=item["opening_ru"],
            steps=tuple(_step_from_dict(step) for step in item["steps"]),
        )
        if scenario.id in scenarios:
            raise ValueError(f"Duplicate scenario id: {scenario.id}")
        if not scenario.steps:
            raise ValueError(f"Scenario has no steps: {scenario.id}")
        scenarios[scenario.id] = scenario
    return scenarios
