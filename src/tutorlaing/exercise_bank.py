from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .ai import ADAPTIVE_DRILL_ITEMS, DrillPack, FLASHCARD_ITEMS, TOPIC_DRILL_ITEMS


class ExerciseBankStore(Protocol):
    def exercise_candidates(self, chat_id: int, **filters: Any) -> list[Any]: ...

    def save_exercise_pack(self, chat_id: int, **values: Any) -> list[int]: ...

    def mark_exercises_used(self, chat_id: int, exercise_ids: list[int]) -> None: ...


@dataclass(frozen=True)
class BankPack:
    pack: DrillPack
    exercise_ids: tuple[int, ...]
    source: str

    def drill_items(self) -> list[dict[str, Any]]:
        return [
            {**item.to_dict(), "exercise_id": exercise_id}
            for item, exercise_id in zip(
                self.pack.items, self.exercise_ids, strict=True
            )
        ]


class ExerciseBank:
    """Selects reusable exercises while keeping persistence behind a port."""

    def __init__(self, store: ExerciseBankStore):
        self.store = store

    @staticmethod
    def required_items(mode: str) -> int:
        return {
            "adaptive": ADAPTIVE_DRILL_ITEMS,
            "toolkit_cards": FLASHCARD_ITEMS,
            "toolkit_topic": TOPIC_DRILL_ITEMS,
        }[mode]

    def find_pack(
        self,
        chat_id: int,
        *,
        target_language: str,
        instruction_language: str,
        translation_language: str,
        learner_level: str,
        mode: str,
        scenario_id: str = "",
    ) -> BankPack | None:
        expected = self.required_items(mode)
        rows = self.store.exercise_candidates(
            chat_id,
            target_language=target_language,
            instruction_language=instruction_language,
            translation_language=translation_language,
            learner_level=learner_level,
            mode=mode,
            scenario_id=scenario_id,
            limit=max(100, expected * 8),
        )
        selected = self._select(rows, expected, flashcards=mode == "toolkit_cards")
        if len(selected) != expected:
            return None
        if all(int(row["learner_seen_count"] or 0) >= 2 for row in selected):
            return None
        raw = {
            "title": str(selected[0]["pack_title"]),
            "focus": str(selected[0]["pack_focus"]),
            "items": [self._row_item(row) for row in selected],
        }
        try:
            pack = DrillPack.from_dict(
                raw, expected_items=expected, flashcard_mode=mode == "toolkit_cards"
            )
        except Exception:
            return None
        ids = tuple(int(row["id"]) for row in selected)
        self.store.mark_exercises_used(chat_id, list(ids))
        return BankPack(pack, ids, "bank")

    def add_pack(
        self,
        chat_id: int,
        pack: DrillPack,
        *,
        target_language: str,
        instruction_language: str,
        translation_language: str,
        learner_level: str,
        mode: str,
        scenario_id: str = "",
        source: str = "ai",
        provider: str = "",
        model: str = "",
        prompt_version: str = "",
        private: bool = False,
        tags: list[str] | None = None,
    ) -> BankPack:
        item_dicts = [item.to_dict() for item in pack.items]
        ids = self.store.save_exercise_pack(
            chat_id,
            target_language=target_language,
            instruction_language=instruction_language,
            translation_language=translation_language,
            learner_level=learner_level,
            mode=mode,
            scenario_id=scenario_id,
            title=pack.title,
            focus=pack.focus,
            items=item_dicts,
            source=source,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            private=private,
            tags=tags or [],
        )
        self.store.mark_exercises_used(chat_id, ids)
        return BankPack(pack, tuple(ids), source)

    def _select(self, rows: list[Any], expected: int, *, flashcards: bool) -> list[Any]:
        if flashcards:
            eligible = [row for row in rows if row["item_type"] == "flashcard"]
            return eligible[:expected]

        minimum_types = 4 if expected >= ADAPTIVE_DRILL_ITEMS else 3
        minimum_recall = 3 if expected >= ADAPTIVE_DRILL_ITEMS else 2
        selected: list[Any] = []
        used_ids: set[int] = set()

        for row in rows:
            if len({item["item_type"] for item in selected}) >= minimum_types:
                break
            if row["item_type"] not in {item["item_type"] for item in selected}:
                selected.append(row)
                used_ids.add(int(row["id"]))
        for row in rows:
            if sum(not self._has_options(item) for item in selected) >= minimum_recall:
                break
            if int(row["id"]) not in used_ids and not self._has_options(row):
                selected.append(row)
                used_ids.add(int(row["id"]))
        for row in rows:
            if len(selected) >= expected:
                break
            if int(row["id"]) not in used_ids:
                selected.append(row)
                used_ids.add(int(row["id"]))
        return selected[:expected]

    @staticmethod
    def _has_options(row: Any) -> bool:
        return bool(json.loads(row["options_json"]))

    @staticmethod
    def _row_item(row: Any) -> dict[str, Any]:
        return {
            "type": row["item_type"],
            "skill": row["skill"],
            "prompt": row["prompt"],
            "context": row["context"],
            "options": json.loads(row["options_json"]),
            "correct_answer": row["correct_answer"],
            "accepted_answers": json.loads(row["accepted_answers_json"]),
            "explanation": row["explanation"],
            "hint": row["hint"],
            "difficulty": row["difficulty"],
        }


def material_signature(material: Any) -> str:
    """Build a non-reversible stable key for a learner-material revision."""
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]
