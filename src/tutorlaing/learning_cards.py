"""Validated, content-owned material for activity-linked micro practice."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


LEARNING_CARD_KINDS = (
    "synonym",
    "antonym",
    "definition_to_word",
    "meaning_in_context",
    "grammar_transform",
    "translation_to_target",
    "translation_from_target",
)


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))


@dataclass(frozen=True)
class LearningCardSeed:
    """One reviewed free-text exercise supplied by content or an AI adapter.

    The selector consumes this DTO but never invents semantic relationships.
    That keeps linguistic authoring independent from scheduling and delivery.
    """

    kind: str
    cue: str
    answer: str
    accepted_answers: tuple[str, ...]
    explanation: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LearningCardSeed":
        kind = str(raw.get("kind", "")).strip()
        if kind not in LEARNING_CARD_KINDS:
            raise ValueError(f"Unsupported learning card kind: {kind}")
        cue = " ".join(str(raw.get("cue", "")).split())
        answer = " ".join(str(raw.get("answer", "")).split())
        if not cue or not answer:
            raise ValueError("Learning card needs both cue and answer")
        normalized_cue = _normalized(cue)
        normalized_answer = _normalized(answer)
        if (
            not normalized_answer
            or f" {normalized_answer} " in f" {normalized_cue} "
        ):
            raise ValueError("Learning card cue reveals its answer")

        accepted: list[str] = []
        raw_accepted = raw.get("accepted_answers", [])
        if not isinstance(raw_accepted, (list, tuple)):
            raise ValueError("Learning card accepted answers must be a list")
        for value in (answer, *raw_accepted):
            candidate = " ".join(str(value).split())
            if candidate and _normalized(candidate) not in {
                _normalized(existing) for existing in accepted
            }:
                accepted.append(candidate)
        if any(
            f" {_normalized(candidate)} " in f" {normalized_cue} "
            for candidate in accepted
        ):
            raise ValueError("Learning card cue reveals an accepted answer")
        explanation = " ".join(str(raw.get("explanation", "")).split())
        return cls(
            kind=kind,
            cue=cue,
            answer=answer,
            accepted_answers=tuple(accepted),
            explanation=explanation or answer,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "cue": self.cue,
            "answer": self.answer,
            "accepted_answers": list(self.accepted_answers),
            "explanation": self.explanation,
        }


def learning_card_seeds(value: Any) -> tuple[LearningCardSeed, ...]:
    """Best-effort boundary parser for runtime candidates.

    Curated JSON is parsed strictly by its content loader. Runtime candidates
    may also come from an AI adapter later, so one malformed seed is ignored
    instead of breaking the foreground activity.
    """

    if not isinstance(value, (list, tuple)):
        return ()
    parsed: list[LearningCardSeed] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        try:
            parsed.append(LearningCardSeed.from_dict(raw))
        except (TypeError, ValueError):
            continue
    return tuple(parsed)
