from __future__ import annotations

from typing import Any

from .ai import ADAPTIVE_DRILL_ITEMS, AIError, DrillItem, DrillPack
from .difficulty import level_policy


COPY = {
    "ru": {
        "title": "Закрепление из истории",
        "focus": "Ваши последние фразы и проблемные места",
        "recall": "Ответьте естественной полной фразой на изучаемом языке.",
        "repair": "Исправьте фразу, сохранив её смысл.",
        "order": "Соберите естественное предложение из этих слов.",
        "ending": "Восстановите последнее слово и запишите всю фразу.",
        "transform": "Скажите ту же мысль естественнее.",
        "complete": "Закончите ситуацию подходящей полной фразой.",
        "reconstruct": "Восстановите фразу по смыслу, не копируя подсказку дословно.",
        "mediate": "Передайте эту мысль собеседнику естественной фразой.",
        "explanation": "Сравните ответ с естественной фразой из вашей истории обучения.",
        "hint": "Вспомните исправленный или опорный вариант этой фразы.",
    },
    "uk": {
        "title": "Закріплення з історії",
        "focus": "Ваші останні фрази та проблемні місця",
        "recall": "Дайте природну повну відповідь мовою, яку вивчаєте.",
        "repair": "Виправте фразу, зберігши її зміст.",
        "order": "Складіть природне речення з цих слів.",
        "ending": "Відновіть останнє слово й запишіть усю фразу.",
        "transform": "Висловіть ту саму думку природніше.",
        "complete": "Завершіть ситуацію відповідною повною фразою.",
        "reconstruct": "Відновіть фразу за змістом, не копіюючи підказку дослівно.",
        "mediate": "Передайте цю думку співрозмовнику природною фразою.",
        "explanation": "Порівняйте відповідь із природною фразою з вашої історії.",
        "hint": "Згадайте виправлений або опорний варіант цієї фрази.",
    },
    "en": {
        "title": "Practice from your history",
        "focus": "Your recent phrases and recurring difficulties",
        "recall": "Reply with one natural complete sentence in the target language.",
        "repair": "Correct the sentence without changing its meaning.",
        "order": "Build a natural sentence from these words.",
        "ending": "Restore the final word and write the complete sentence.",
        "transform": "Express the same meaning more naturally.",
        "complete": "Complete the situation with a suitable full sentence.",
        "reconstruct": "Reconstruct the phrase from its meaning without copying the hint word for word.",
        "mediate": "Relay this meaning to the other person in one natural sentence.",
        "explanation": "Compare your answer with the natural phrase from your learning history.",
        "hint": "Recall the corrected or reference version of this phrase.",
    },
    "pl": {
        "title": "Powtórka z historii",
        "focus": "Ostatnie zwroty i powtarzające się trudności",
        "recall": "Odpowiedz jednym naturalnym, pełnym zdaniem w języku docelowym.",
        "repair": "Popraw zdanie, zachowując jego znaczenie.",
        "order": "Ułóż naturalne zdanie z podanych słów.",
        "ending": "Odtwórz ostatnie słowo i zapisz całe zdanie.",
        "transform": "Wyraź tę samą myśl bardziej naturalnie.",
        "complete": "Uzupełnij sytuację odpowiednim pełnym zdaniem.",
        "reconstruct": "Odtwórz zwrot na podstawie znaczenia, nie kopiując podpowiedzi słowo w słowo.",
        "mediate": "Przekaż tę myśl rozmówcy jednym naturalnym zdaniem.",
        "explanation": "Porównaj odpowiedź z naturalnym zwrotem z historii nauki.",
        "hint": "Przypomnij sobie poprawioną lub wzorcową wersję zwrotu.",
    },
}


def build_adaptive_fallback(
    material: dict[str, Any], instruction_language: str
) -> DrillPack:
    """Build a private recovery pack from already known learner material."""
    seeds = _seeds(material)
    if not seeds:
        raise AIError("No learner material for adaptive fallback")
    copy = COPY.get(instruction_language, COPY["en"])
    difficulty = level_policy(str(material.get("learner_level") or "A1")).item_difficulty
    kinds = (
        "dialogue_repair",
        "fill_ending",
        "word_order",
        "reconstruction",
        "free_recall",
        "constrained_paraphrase",
        "mediation",
        "complete_sentence",
    )
    items: list[DrillItem] = []
    for index, kind in enumerate(kinds):
        seed = seeds[index % len(seeds)]
        answer = seed["answer"]
        source = seed["source"]
        context = seed["context"]
        actual_kind = kind
        if kind == "dialogue_repair" and source == answer:
            actual_kind = "constrained_paraphrase"
        if actual_kind == "dialogue_repair":
            prompt, shown = copy["repair"], source
        elif actual_kind == "fill_ending":
            prompt, shown = copy["ending"], _hide_ending(answer)
        elif actual_kind == "word_order":
            prompt, shown = copy["order"], _rotate_words(answer)
        elif actual_kind == "complete_sentence":
            prompt, shown = copy["complete"], context or source
        elif actual_kind == "reconstruction":
            prompt, shown = copy["reconstruct"], context or _hide_words(answer)
        elif actual_kind == "mediation":
            prompt, shown = copy["mediate"], context or source
        elif actual_kind == "constrained_paraphrase":
            prompt, shown = copy["transform"], source or context
        else:
            prompt, shown = copy["recall"], context or source
        items.append(
            DrillItem(
                type=actual_kind,
                skill=seed["skill"],
                prompt=prompt,
                context=shown,
                options=(),
                correct_answer=answer,
                accepted_answers=(answer,),
                explanation=copy["explanation"],
                hint=copy["hint"],
                difficulty=difficulty,
            )
        )
    if len(items) != ADAPTIVE_DRILL_ITEMS or len({item.type for item in items}) < 4:
        raise AIError("Adaptive fallback invariants failed")
    return DrillPack(copy["title"], copy["focus"], tuple(items))


def _seeds(material: dict[str, Any]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in material.get("recurring_problem_material", []):
        answer = str(raw.get("target_chunk") or raw.get("correct_answer") or "").strip()
        if answer and answer not in seen:
            seen.add(answer)
            values.append(
                {
                    "answer": answer,
                    "source": str(raw.get("user_answer") or answer).strip(),
                    "context": str(raw.get("context") or raw.get("prompt") or "").strip(),
                    "skill": str(raw.get("skill") or "recurring_problem").strip(),
                }
            )
    for raw in material.get("recent_learner_material", []):
        answer = str(raw.get("natural_response") or "").strip()
        if answer and answer not in seen:
            seen.add(answer)
            corrections = raw.get("corrections") or raw.get("grammar_chunks") or []
            values.append(
                {
                    "answer": answer,
                    "source": str(raw.get("learner_response") or answer).strip(),
                    "context": str(raw.get("learner_response") or "").strip(),
                    "skill": str(corrections[0] if corrections else "recent_phrase")[:200],
                }
            )
    return values


def _rotate_words(sentence: str) -> str:
    words = sentence.split()
    if len(words) < 2:
        return sentence
    shift = max(1, len(words) // 2)
    return " / ".join(words[shift:] + words[:shift])


def _hide_ending(sentence: str) -> str:
    words = sentence.split()
    if not words:
        return sentence
    last = words[-1]
    punctuation = last[-1] if last[-1:] in ".,!?" else ""
    core = last[:-1] if punctuation else last
    keep = max(1, len(core) - 2)
    words[-1] = core[:keep] + "__" + punctuation
    return " ".join(words)


def _hide_words(sentence: str) -> str:
    words = sentence.split()
    return " ".join("___" if index % 2 else word for index, word in enumerate(words))
