"""Activity-linked micro practice delivered without mutating the main flow."""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Any

from .ai import AIClient, AIError, DrillItem
from .contracts import BackgroundLearningStore
from .engine import normalize
from .learning_cards import LearningCardSeed, learning_card_seeds


CARD_TYPES = ("recall", "cloze", "word_order", "paraphrase")
SOURCE_WEIGHTS = {
    "current_activity": 0.60,
    "related_activity": 0.25,
    "older_due": 0.15,
}


@dataclass(frozen=True)
class BackgroundCardDraft:
    activity_kind: str
    activity_id: str
    topic: str
    card_type: str
    context: str
    correct_answer: str
    accepted_answers: tuple[str, ...]
    explanation: str
    source_step: str
    reason: str = "current_activity"


class BackgroundLearningService:
    """Builds one varied card from the foreground activity context."""

    def __init__(
        self, store: BackgroundLearningStore, ai: AIClient | None = None
    ) -> None:
        self.store = store
        self.ai = ai

    def build(
        self,
        chat_id: int,
        activity_kind: str,
        activity_id: str,
        context: dict[str, Any],
    ) -> BackgroundCardDraft | None:
        reason, selected = self._select_source(chat_id, context)
        reference = " ".join(str(selected.get("reference") or "").split())
        if not reference:
            return None
        recent = self.store.recent_background_card_types(chat_id, activity_id, 3)
        semantic_cards = learning_card_seeds(selected.get("learning_cards"))
        card_type = self._next_type(
            recent, reference, tuple(card.kind for card in semantic_cards)
        )
        semantic = self._semantic_material(card_type, semantic_cards, len(recent))
        if semantic is None:
            rendered_context, correct = self._material(
                card_type,
                reference,
                activity_id,
                len(recent),
                str(selected.get("task") or selected.get("interlocutor") or ""),
            )
            accepted = (correct,)
            explanation = reference
        else:
            rendered_context = semantic.cue
            correct = semantic.answer
            accepted = semantic.accepted_answers
            explanation = semantic.explanation
        return BackgroundCardDraft(
            activity_kind=activity_kind,
            activity_id=activity_id,
            topic=str(selected.get("title") or context.get("title") or activity_kind),
            card_type=card_type,
            context=rendered_context,
            correct_answer=correct,
            accepted_answers=accepted,
            explanation=explanation,
            source_step=str(
                selected["step"]
                if "step" in selected
                else selected.get("node", "current")
            ),
            reason=reason,
        )

    def _select_source(
        self, chat_id: int, context: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        pools: dict[str, list[dict[str, Any]]] = {
            "current_activity": [context],
            "related_activity": self._candidate_list(
                context.get("related_candidates")
            ),
            "older_due": self._candidate_list(context.get("due_candidates")),
        }
        available = [reason for reason, candidates in pools.items() if candidates]
        total_weight = sum(SOURCE_WEIGHTS[reason] for reason in available)
        weights = {
            reason: SOURCE_WEIGHTS[reason] / total_weight for reason in available
        }
        recent = [
            reason
            for reason in self.store.recent_background_card_reasons(chat_id, 20)
            if reason in available
        ]
        next_total = len(recent) + 1
        reason = max(
            available,
            key=lambda candidate: (
                weights[candidate] * next_total - recent.count(candidate),
                weights[candidate],
            ),
        )
        candidates = pools[reason]
        index = recent.count(reason) % len(candidates)
        return reason, candidates[index]

    @staticmethod
    def _candidate_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, (list, tuple)):
            return []
        return [candidate for candidate in value if isinstance(candidate, dict)]

    def evaluate(
        self,
        row: Any,
        response: str,
        instruction_language: str,
        target_language: str,
    ) -> tuple[float, str]:
        correct = str(row["correct_answer"])
        accepted = tuple(str(value) for value in self._accepted(row))
        if normalize(response) in {normalize(value) for value in accepted}:
            return 1.0, correct
        if self.ai is None:
            return 0.0, correct
        item_type = {
            "recall": "free_recall",
            "cloze": "reconstruction",
            "word_order": "word_order",
            "paraphrase": "constrained_paraphrase",
            "synonym": "constrained_paraphrase",
            "antonym": "free_recall",
            "definition_to_word": "free_recall",
            "meaning_in_context": "free_recall",
            "grammar_transform": "transform",
            "translation_to_target": "free_recall",
            "translation_from_target": "free_recall",
        }.get(str(row["card_type"]), "free_recall")
        item = DrillItem(
            type=item_type,
            skill=str(row["card_type"]),
            prompt=str(row["prompt"]),
            context=str(row["context"]),
            options=(),
            correct_answer=correct,
            accepted_answers=accepted,
            explanation=str(row["explanation"]),
            hint="",
            difficulty=1,
        )
        try:
            result = self.ai.evaluate_drill_answer(
                item, response, instruction_language, target_language
            )
        except (AIError, AttributeError):
            return 0.0, correct
        return result.score, result.corrected_answer or correct

    @staticmethod
    def _accepted(row: Any) -> list[str]:
        import json

        values = json.loads(str(row["accepted_answers_json"]))
        return values if isinstance(values, list) else []

    @staticmethod
    def _next_type(
        recent: list[str], reference: str, semantic_kinds: tuple[str, ...] = ()
    ) -> str:
        base = list(CARD_TYPES)
        if len(re.findall(r"\w+", reference, flags=re.UNICODE)) < 2:
            base = ["recall", "paraphrase"]
        unique_semantic = list(dict.fromkeys(semantic_kinds))
        allowed: list[str] = []
        for index, card_type in enumerate(base):
            allowed.append(card_type)
            if index < len(unique_semantic):
                allowed.append(unique_semantic[index])
        allowed.extend(unique_semantic[len(base) :])
        if not recent or recent[0] not in allowed:
            return allowed[0]
        return allowed[(allowed.index(recent[0]) + 1) % len(allowed)]

    @staticmethod
    def _semantic_material(
        card_type: str,
        cards: tuple[LearningCardSeed, ...],
        salt: int,
    ) -> LearningCardSeed | None:
        matching = [card for card in cards if card.kind == card_type]
        return matching[salt % len(matching)] if matching else None

    @staticmethod
    def _material(
        card_type: str,
        reference: str,
        activity_id: str,
        salt: int,
        recall_context: str,
    ) -> tuple[str, str]:
        words = reference.split()
        if card_type == "cloze":
            candidates = [
                (index, re.sub(r"[^\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ-]", "", word))
                for index, word in enumerate(words)
            ]
            index, answer = max(candidates, key=lambda item: len(item[1]))
            hidden = list(words)
            hidden[index] = "_____"
            return " ".join(hidden), answer
        if card_type == "word_order":
            shuffled = list(words)
            seed = int(hashlib.sha256(f"{activity_id}:{salt}".encode()).hexdigest()[:8], 16)
            random.Random(seed).shuffle(shuffled)
            if shuffled == words and len(shuffled) > 1:
                shuffled = shuffled[1:] + shuffled[:1]
            return " · ".join(shuffled), reference
        if card_type == "recall":
            context = " ".join(recall_context.split())
            return context or "…", reference
        return reference, reference
