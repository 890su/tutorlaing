from __future__ import annotations

from random import SystemRandom
from typing import Any

from .ai import AIError, DrillItem, DrillPack, FLASHCARD_ITEMS
from .difficulty import level_policy


TEXT = {
    "ru": {
        "cards_title": "Карточки из курса",
        "cards_focus": "Полезные фразы для бытовых ситуаций",
        "cards_prompt": "Что означает эта фраза?",
        "cards_explanation": "Это практическое значение фразы в ситуации курса.",
        "cards_hint": "Вспомните ситуацию, в которой используется фраза.",
        "topic_prompt": "Выберите подходящую фразу для этой ситуации.",
        "recall_prompt": "Ответьте естественной фразой на изучаемом языке.",
        "transform_prompt": "Сформулируйте ту же цель другими словами.",
        "repair_prompt": "Исправьте реплику, чтобы она естественно подошла к ситуации.",
        "mediate_prompt": "Передайте эту просьбу собеседнику одной естественной фразой.",
        "explanation": "Сверьте ответ с опорной фразой курса.",
        "hint": "Используйте ключевую фразу из этой темы.",
    },
    "uk": {
        "cards_title": "Картки з курсу",
        "cards_focus": "Корисні фрази для побутових ситуацій",
        "cards_prompt": "Що означає ця фраза?",
        "cards_explanation": "Це практичне значення фрази в ситуації курсу.",
        "cards_hint": "Згадайте ситуацію, у якій використовується фраза.",
        "topic_prompt": "Оберіть відповідну фразу для цієї ситуації.",
        "recall_prompt": "Дайте природну відповідь мовою, яку вивчаєте.",
        "transform_prompt": "Сформулюйте ту саму мету іншими словами.",
        "repair_prompt": "Виправте репліку, щоб вона природно пасувала до ситуації.",
        "mediate_prompt": "Передайте це прохання співрозмовнику однією природною фразою.",
        "explanation": "Порівняйте відповідь з опорною фразою курсу.",
        "hint": "Використайте ключову фразу з цієї теми.",
    },
    "en": {
        "cards_title": "Course flashcards",
        "cards_focus": "Useful phrases for everyday situations",
        "cards_prompt": "What does this phrase mean?",
        "cards_explanation": "This is the phrase's practical meaning in the course scenario.",
        "cards_hint": "Recall the situation where this phrase is used.",
        "topic_prompt": "Choose the phrase that fits this situation.",
        "recall_prompt": "Reply naturally in the language you are learning.",
        "transform_prompt": "Express the same goal in another natural way.",
        "repair_prompt": "Repair the turn so it sounds natural in this situation.",
        "mediate_prompt": "Relay this request to the other person in one natural sentence.",
        "explanation": "Compare your answer with the course reference phrase.",
        "hint": "Use the key phrase from this topic.",
    },
    "pl": {
        "cards_title": "Fiszki z kursu",
        "cards_focus": "Przydatne zwroty w codziennych sytuacjach",
        "cards_prompt": "Co oznacza to wyrażenie?",
        "cards_explanation": "To praktyczne znaczenie zwrotu w sytuacji z kursu.",
        "cards_hint": "Przypomnij sobie sytuację, w której używa się tego zwrotu.",
        "topic_prompt": "Wybierz zwrot pasujący do tej sytuacji.",
        "recall_prompt": "Odpowiedz naturalnie w języku, którego się uczysz.",
        "transform_prompt": "Wyraź ten sam cel innymi naturalnymi słowami.",
        "repair_prompt": "Popraw wypowiedź, aby naturalnie pasowała do sytuacji.",
        "mediate_prompt": "Przekaż tę prośbę rozmówcy jednym naturalnym zdaniem.",
        "explanation": "Porównaj odpowiedź ze zwrotem wzorcowym z kursu.",
        "hint": "Użyj kluczowego zwrotu z tego tematu.",
    },
}


def _copy(language: str) -> dict[str, str]:
    return TEXT.get(language, TEXT["en"])


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def build_toolkit_fallback(
    mode: str, material: dict[str, Any], instruction_language: str
) -> DrillPack:
    """Build a usable curated pack without an external AI provider."""

    if mode == "cards":
        return _build_cards(material, instruction_language)
    if mode == "topic":
        return _build_topic(material, instruction_language)
    raise ValueError(f"Unsupported toolkit fallback mode: {mode}")


def _build_cards(material: dict[str, Any], language: str) -> DrillPack:
    copy = _copy(language)
    policy = level_policy(str(material.get("learner_level") or "A1"))
    phrases: list[tuple[str, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    for raw in material.get("phrases", []):
        phrase = str(raw.get("target_phrase", "")).strip()
        meaning = str(raw.get("practical_meaning_ru", "")).strip()
        pair = (phrase, meaning)
        if phrase and meaning and pair not in seen:
            seen.add(pair)
            phrases.append((phrase, meaning, raw.get("priority") == "problem"))
    meanings = _unique([meaning for _, meaning, _ in phrases])
    if len(phrases) < FLASHCARD_ITEMS or len(meanings) < 4:
        raise AIError("Not enough curated material for fallback flashcards")

    randomizer = SystemRandom()
    problem = [item for item in phrases if item[2]]
    regular = [item for item in phrases if not item[2]]
    randomizer.shuffle(problem)
    randomizer.shuffle(regular)
    selected = (problem + regular)[:FLASHCARD_ITEMS]
    randomizer.shuffle(selected)

    items: list[DrillItem] = []
    for index, (phrase, meaning, _) in enumerate(selected):
        distractor_pool = [value for value in meanings if value != meaning]
        distractors = randomizer.sample(distractor_pool, 3)
        options = [meaning, *distractors]
        shift = index % len(options)
        options = options[shift:] + options[:shift]
        items.append(
            DrillItem(
                type="flashcard",
                skill="practical_meaning",
                prompt=copy["cards_prompt"],
                context=phrase,
                options=tuple(options),
                correct_answer=meaning,
                accepted_answers=(meaning,),
                explanation=copy["cards_explanation"],
                hint=copy["cards_hint"],
                difficulty=policy.item_difficulty,
            )
        )
    return DrillPack(
        title=copy["cards_title"],
        focus=copy["cards_focus"],
        items=tuple(items),
    )


def _build_topic(material: dict[str, Any], language: str) -> DrillPack:
    copy = _copy(language)
    policy = level_policy(str(material.get("learner_level") or "A1"))
    steps = [raw for raw in material.get("steps", []) if raw.get("target_chunk")]
    if not steps:
        raise AIError("No curated scenario steps for fallback topic pack")
    chunks = _unique([str(raw["target_chunk"]) for raw in steps])
    kinds = {
        3: (
            "translation_choice",
            "meaning_choice",
            "choose_form",
            "complete_sentence",
            "free_recall",
        ),
        2: (
            "translation_choice",
            "register_choice",
            "complete_sentence",
            "dialogue_repair",
            "free_recall",
        ),
        1: (
            "register_choice",
            "complete_sentence",
            "constrained_paraphrase",
            "dialogue_repair",
            "mediation",
        ),
    }[policy.choice_items]
    items: list[DrillItem] = []
    for index, kind in enumerate(kinds):
        step = steps[index % len(steps)]
        answer = str(step["target_chunk"]).strip()
        context = str(
            step.get("learner_goal_ru") or step.get("interlocutor") or ""
        ).strip()
        if index < policy.choice_items:
            options = [answer, *[chunk for chunk in chunks if chunk != answer][:3]]
            shift = index % len(options)
            options = options[shift:] + options[:shift]
            prompt = copy["topic_prompt"]
        else:
            options = []
            prompt = {
                "constrained_paraphrase": copy["transform_prompt"],
                "dialogue_repair": copy["repair_prompt"],
                "mediation": copy["mediate_prompt"],
            }.get(kind, copy["recall_prompt"])
        items.append(
            DrillItem(
                type=kind,
                skill="scenario_phrase",
                prompt=prompt,
                context=context,
                options=tuple(options),
                correct_answer=answer,
                accepted_answers=(answer,),
                explanation=copy["explanation"],
                hint=copy["hint"],
                difficulty=policy.item_difficulty,
            )
        )
    return DrillPack(
        title=str(material.get("title") or copy["cards_title"]),
        focus=str(material.get("objective_ru") or copy["cards_focus"]),
        items=tuple(items),
    )
