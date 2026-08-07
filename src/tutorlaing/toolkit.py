from __future__ import annotations

import logging
from dataclasses import replace
from random import SystemRandom
from typing import Any

from .ai import (
    AIClient,
    AIError,
    DrillPack,
    FLASHCARD_ITEMS,
    PROMPT_VERSION,
    PhraseTranslation,
)
from .catalog import ScenarioCatalog
from .content import Scenario
from .difficulty import practice_level
from .contracts import (
    Keyboard,
    TelegramGateway,
    ToolkitDelivery,
    ToolkitStore,
    TransportError,
)
from .i18n import tr
from .exercise_bank import ExerciseBank, material_signature
from .navigation import back_row, home_row
from .toolkit_fallback import build_toolkit_fallback
from .ui import card
from .workspace import TelegramWorkspace


LOGGER = logging.getLogger(__name__)
PACK_MODES = {"cards", "topic"}
INPUT_MODES = {"to_target", "from_target"}
LANGUAGE_LABELS = {
    "ru": "Русский",
    "uk": "Українська",
    "en": "English",
    "pl": "Polski",
}


def shuffle_flashcard_options(
    pack: DrillPack, randomizer: SystemRandom | None = None
) -> DrillPack:
    """Randomize answer positions while keeping every flashcard unambiguous.

    The AI is allowed to return options in any order and often puts the correct
    one first.  We therefore enforce varied positions in application code.  A
    Any pack of at least four cards uses every A-D position at least once.
    """

    rng = randomizer or SystemRandom()
    positions = list(range(4))
    positions.extend(rng.randrange(4) for _ in range(max(0, len(pack.items) - 4)))
    rng.shuffle(positions)

    items = []
    for item, correct_position in zip(pack.items, positions, strict=True):
        options = list(item.options)
        if item.type != "flashcard" or len(options) != 4:
            items.append(item)
            continue
        options.remove(item.correct_answer)
        rng.shuffle(options)
        options.insert(correct_position, item.correct_answer)
        items.append(replace(item, options=tuple(options)))

    return replace(pack, items=tuple(items))


class PracticeToolkit:
    """Learner-facing toolbox for flashcards, phrase translation and topics."""

    def __init__(
        self,
        store: ToolkitStore,
        workspace: TelegramWorkspace,
        catalog: ScenarioCatalog,
        ai: AIClient | None,
        delivery: ToolkitDelivery,
        telegram: TelegramGateway,
        exercise_bank: ExerciseBank | None = None,
    ):
        self.store = store
        self.workspace = workspace
        self.catalog = catalog
        self.ai = ai
        self.delivery = delivery
        self.telegram = telegram
        self.exercise_bank = exercise_bank or ExerciseBank(store)

    def show_menu(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        if user["toolkit_input_mode"]:
            values: dict[str, Any] = {"toolkit_input_mode": None}
            if user["stage"] == "toolkit_input":
                values["stage"] = "idle"
            self.store.set_user_state(chat_id, **values)
            user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        target = LANGUAGE_LABELS.get(
            str(user["target_language"]), str(user["target_language"])
        )
        keyboard: Keyboard = [
            [
                {
                    "text": tr(language, "toolkit.my_material"),
                    "callback_data": "drill:start",
                }
            ],
            [
                {
                    "text": tr(language, "toolkit.cards"),
                    "callback_data": "toolkit:start:cards",
                }
            ],
            [
                {
                    "text": tr(
                        language, "toolkit.translate_to_named", target=target
                    ),
                    "callback_data": "toolkit:translate:to_target",
                }
            ],
            [
                {
                    "text": tr(
                        language, "toolkit.translate_from_named", target=target
                    ),
                    "callback_data": "toolkit:translate:from_target",
                },
            ],
            [
                {
                    "text": tr(language, "toolkit.topic"),
                    "callback_data": "toolkit:topics",
                }
            ],
            home_row(language),
        ]
        self._prepend_activity_return(keyboard, user, language)
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.title"),
                tr(language, "toolkit.summary"),
            ),
            keyboard,
            surface="toolkit",
        )

    def show_topics(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        scenarios = self.catalog.for_user(user)
        keyboard: Keyboard = [
            [
                {
                    "text": self._scenario_title(scenario, language),
                    "callback_data": f"toolkit:topic:{scenario.id}",
                }
            ]
            for scenario in scenarios.values()
        ]
        keyboard.append(back_row(language, "toolkit", "toolkit"))
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.topic_title"),
                tr(language, "toolkit.topic_summary"),
            ),
            keyboard,
            surface="toolkit_topics",
        )

    def start_pack(
        self, chat_id: int, mode: str, scenario_id: str | None = None
    ) -> None:
        if mode not in PACK_MODES:
            raise ValueError(f"Unsupported toolkit mode: {mode}")
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        active = self.store.active_drill(chat_id)
        if active is not None and str(active["mode"]).startswith("toolkit_"):
            self.workspace.show(
                chat_id,
                card(
                    tr(language, "toolkit.active_title"),
                    tr(language, "toolkit.active_summary"),
                ),
                [
                    [
                        {
                            "text": tr(language, "toolkit.continue"),
                            "callback_data": "drill:resume",
                        }
                    ],
                    back_row(language, "toolkit", "toolkit"),
                ],
                surface="toolkit_active",
            )
            return
        try:
            material = self._pack_material(user, mode, scenario_id)
        except KeyError:
            self.show_topics(chat_id)
            return
        bank_mode = f"toolkit_{mode}"
        working_level = practice_level(user)
        bank_context = scenario_id or ""
        if mode == "cards":
            problem_phrases = [
                {
                    "target_phrase": item.get("target_phrase", ""),
                    "meaning": item.get("practical_meaning_ru", ""),
                }
                for item in material.get("phrases", [])
                if item.get("priority") == "problem"
            ]
            bank_context = (
                f"history:{material_signature(problem_phrases)}"
                if problem_phrases
                else "catalog"
            )
        bank_pack = self.exercise_bank.find_pack(
            chat_id,
            target_language=str(user["target_language"]),
            instruction_language=language,
            translation_language=str(user["translation_language"]),
            learner_level=working_level,
            mode=bank_mode,
            scenario_id=bank_context,
        )
        self.store.event(
            chat_id,
            "exercise_bank_lookup",
            {"mode": bank_mode, "hit": bank_pack is not None},
        )
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.preparing"),
                tr(language, f"toolkit.preparing_{mode}"),
            ),
            surface="toolkit_preparing",
        )
        used_fallback = False
        try:
            if bank_pack is not None:
                pack = bank_pack.pack
            elif self.ai is None:
                used_fallback = True
                pack = build_toolkit_fallback(mode, material, language)
            else:
                try:
                    pack = self.ai.generate_toolkit_pack(
                        mode,
                        material,
                        language,
                        str(user["target_language"]),
                        str(user["translation_language"]),
                    )
                except AIError:
                    LOGGER.exception("AI toolkit pack generation failed")
                    used_fallback = True
                    pack = build_toolkit_fallback(mode, material, language)
        except AIError:
            LOGGER.exception("Toolkit fallback pack generation failed")
            self.workspace.show(
                chat_id,
                card(
                    tr(language, "toolkit.error_title"),
                    tr(language, "toolkit.error_summary"),
                ),
                [
                    [
                        {
                            "text": tr(language, "toolkit.retry"),
                            "callback_data": (
                                f"toolkit:topic:{scenario_id}"
                                if scenario_id
                                else f"toolkit:start:{mode}"
                            ),
                        }
                    ],
                    back_row(language, "toolkit", "toolkit"),
                ],
                surface="toolkit_error",
            )
            return

        if bank_pack is None:
            bank_pack = self.exercise_bank.add_pack(
                chat_id,
                pack,
                target_language=str(user["target_language"]),
                instruction_language=language,
                translation_language=str(user["translation_language"]),
                learner_level=working_level,
                mode=bank_mode,
                scenario_id=bank_context,
                source="fallback" if used_fallback else "ai",
                provider=("local" if used_fallback else str(self.ai.provider)),
                model=("curated-fallback" if used_fallback else str(self.ai.model)),
                prompt_version=("fallback-v1" if used_fallback else PROMPT_VERSION),
                private=mode == "cards",
            )

        if mode == "cards":
            pack = shuffle_flashcard_options(pack)
            bank_pack = replace(bank_pack, pack=pack)

        if used_fallback:
            self.store.event(
                chat_id, "ai_fallback_used", {"operation": f"toolkit_{mode}"}
            )

        drill_id = self.store.start_drill(
            chat_id,
            None,
            pack.title,
            pack.focus,
            bank_pack.drill_items(),
            mode=bank_mode,
            replace_active=False,
        )
        self.store.event(
            chat_id,
            "toolkit_started",
            {"mode": mode, "scenario_id": scenario_id, "source": bank_pack.source},
        )
        self.delivery.send_drill_item(chat_id, drill_id)
        self.delivery.schedule_next_assignment(chat_id)

    def ask_for_phrase(self, chat_id: int, mode: str) -> None:
        if mode not in INPUT_MODES:
            raise ValueError(f"Unsupported phrase mode: {mode}")
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        source_language, target_language = self._translation_direction(user, mode)
        if source_language == target_language:
            self.workspace.show(
                chat_id,
                card(
                    tr(language, "toolkit.language_conflict_title"),
                    tr(language, "toolkit.language_conflict_summary"),
                ),
                [
                    [
                        {
                            "text": tr(language, "settings.translation"),
                            "callback_data": "settings:translation",
                        }
                    ],
                    back_row(language, "toolkit", "toolkit"),
                ],
                surface="toolkit_translation_conflict",
            )
            return
        self.store.set_user_state(chat_id, toolkit_input_mode=mode)
        keyboard: Keyboard = [back_row(language, "toolkit", "toolkit")]
        self._prepend_activity_return(
            keyboard, user, language, cancel_phrase_input=True
        )
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.phrase_title"),
                tr(
                    language,
                    "toolkit.phrase_prompt",
                    source=LANGUAGE_LABELS.get(source_language, source_language),
                    target=LANGUAGE_LABELS.get(target_language, target_language),
                ),
            ),
            keyboard,
            surface="toolkit_phrase_input",
        )

    def handle_phrase(self, chat_id: int, text: str) -> None:
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        mode = str(user["toolkit_input_mode"] or "")
        if mode not in INPUT_MODES:
            self.show_menu(chat_id)
            return
        phrase = text.strip()
        if not phrase or len(phrase) > 4000:
            keyboard: Keyboard = [back_row(language, "toolkit", "toolkit")]
            self._prepend_activity_return(
                keyboard, user, language, cancel_phrase_input=True
            )
            self.workspace.show(
                chat_id,
                tr(language, "toolkit.phrase_invalid"),
                keyboard,
                force_new=True,
                surface="toolkit_phrase_input",
            )
            return
        if self.ai is None:
            self._show_ai_unavailable(chat_id, language)
            return
        source_language, target_language = self._translation_direction(user, mode)
        try:
            self.telegram.send_chat_action(chat_id, "typing")
        except TransportError:
            LOGGER.debug("Could not send phrase-tool typing action", exc_info=True)
        try:
            result = self.ai.translate_with_variants(
                phrase,
                source_language,
                target_language,
                language,
            )
        except AIError:
            LOGGER.exception("AI phrase translation failed")
            self.store.event(
                chat_id,
                "ai_fallback_used",
                {"operation": "phrase_translation", "direction": mode},
            )
            keyboard: Keyboard = [
                [
                    {
                        "text": tr(language, "toolkit.retry"),
                        "callback_data": f"toolkit:translate:{mode}",
                    }
                ],
                back_row(language, "toolkit", "toolkit"),
            ]
            self._prepend_activity_return(
                keyboard, user, language, cancel_phrase_input=True
            )
            self.workspace.show(
                chat_id,
                card(
                    tr(language, "toolkit.error_title"),
                    tr(language, "toolkit.translation_error"),
                ),
                keyboard,
                force_new=True,
                surface="toolkit_error",
            )
            return

        analysis_id = self.store.add_ai_analysis(
            chat_id=chat_id,
            operation="phrase_translation",
            target_language=str(user["target_language"]),
            source_text=phrase,
            result=self._translation_dict(result, mode),
            provider=self.ai.provider,
            model=self.ai.model,
            prompt_version="phrase-translation-v1",
            latency_ms=0,
        )
        values: dict[str, Any] = {"toolkit_input_mode": None}
        if user["stage"] == "toolkit_input":
            values["stage"] = "idle"
        self.store.set_user_state(chat_id, **values)
        self.store.event(
            chat_id,
            "phrase_translated",
            {"direction": mode, "analysis_id": analysis_id},
        )
        self._show_translation(chat_id, result, mode)

    def _show_translation(
        self, chat_id: int, result: PhraseTranslation, mode: str
    ) -> None:
        language = str(self.store.get_user(chat_id)["instruction_language"])
        blocks = [
            tr(language, "toolkit.translation_source", text=result.source_text),
            tr(language, "toolkit.translation_primary", text=result.primary),
        ]
        for alternative in result.alternatives:
            register = tr(language, f"variant.{alternative.register}")
            nuance = f" — {alternative.nuance}" if alternative.nuance else ""
            blocks.append(f"{register}: {alternative.text}{nuance}")
        if result.usage_note:
            blocks.append(
                tr(language, "toolkit.translation_note", note=result.usage_note)
            )
        swapped = "from_target" if mode == "to_target" else "to_target"
        keyboard: Keyboard = [
            [
                {
                    "text": tr(language, "toolkit.another_phrase"),
                    "callback_data": f"toolkit:translate:{mode}",
                },
                {
                    "text": tr(language, "toolkit.swap"),
                    "callback_data": f"toolkit:translate:{swapped}",
                },
            ],
            back_row(language, "toolkit", "toolkit"),
        ]
        self._prepend_activity_return(
            keyboard, self.store.get_user(chat_id), language
        )
        self.workspace.show(
            chat_id,
            card(tr(language, "toolkit.translation_result"), "\n\n".join(blocks)),
            keyboard,
            force_new=True,
            surface="toolkit_translation_result",
        )

    def _pack_material(
        self, user: Any, mode: str, scenario_id: str | None
    ) -> dict[str, Any]:
        scenarios = self.catalog.for_user(user)
        if mode == "topic":
            scenario = scenarios[scenario_id or ""]
            material = self._scenario_material(scenario)
            material["learner_level"] = practice_level(user)
            return material
        target_language = str(user["target_language"])
        history = self.store.problem_history(
            int(user["chat_id"]), target_language, limit=20
        )
        problem_steps = {
            (str(item["scenario_id"]), int(item["step_index"]))
            for item in history["scenario_steps"]
        }
        phrases = [
            {
                "topic": scenario.title_pl,
                "scenario_id": scenario.id,
                "step_index": step_index,
                "practical_meaning_ru": step.context_ru,
                "target_phrase": step.target_chunk,
                "priority": (
                    "problem"
                    if (scenario.id, step_index) in problem_steps
                    else "regular"
                ),
            }
            for scenario in scenarios.values()
            for step_index, step in enumerate(scenario.steps)
        ]

        failed_cards = []
        known_phrases = {str(item["target_phrase"]) for item in phrases}
        for item in history["drill_items"]:
            if item["item_type"] != "flashcard" or not item["context"]:
                continue
            phrase = str(item["context"]).strip()
            if phrase in known_phrases:
                for candidate in phrases:
                    if candidate["target_phrase"] == phrase:
                        candidate["priority"] = "problem"
                continue
            meaning = str(item["correct_answer"]).strip()
            if phrase and meaning:
                failed_cards.append(
                    {
                        "topic": "learner_history",
                        "practical_meaning_ru": meaning,
                        "target_phrase": phrase,
                        "priority": "problem",
                    }
                )
                known_phrases.add(phrase)

        randomizer = SystemRandom()
        problem = failed_cards + [
            item for item in phrases if item["priority"] == "problem"
        ]
        regular = [item for item in phrases if item["priority"] != "problem"]
        randomizer.shuffle(problem)
        randomizer.shuffle(regular)
        problem_limit = min(len(problem), 7)
        selected = problem[:problem_limit]
        selected.extend(regular[: FLASHCARD_ITEMS - len(selected)])
        if len(selected) < FLASHCARD_ITEMS:
            selected.extend(
                problem[problem_limit : problem_limit + FLASHCARD_ITEMS - len(selected)]
            )
        randomizer.shuffle(selected)
        return {
            "phrases": selected[:FLASHCARD_ITEMS],
            "learner_level": practice_level(user),
            "selection_policy": {
                "problem_items": sum(
                    item["priority"] == "problem" for item in selected
                ),
                "random_items": sum(
                    item["priority"] != "problem" for item in selected
                ),
            },
        }

    @staticmethod
    def _scenario_material(scenario: Scenario) -> dict[str, Any]:
        return {
            "scenario_id": scenario.id,
            "title": scenario.title_pl,
            "objective_ru": scenario.objective_ru,
            "opening_ru": scenario.opening_ru,
            "steps": [
                {
                    "interlocutor": step.interlocutor_pl,
                    "learner_goal_ru": step.context_ru,
                    "target_chunk": step.target_chunk,
                    "expected_meanings": step.expected_groups,
                }
                for step in scenario.steps
            ],
        }

    @staticmethod
    def _translation_direction(user: Any, mode: str) -> tuple[str, str]:
        target = str(user["target_language"])
        support = str(user["translation_language"])
        return (support, target) if mode == "to_target" else (target, support)

    @staticmethod
    def _translation_dict(result: PhraseTranslation, direction: str) -> dict[str, Any]:
        return {
            "direction": direction,
            "primary": result.primary,
            "alternatives": [
                {
                    "text": item.text,
                    "register": item.register,
                    "nuance": item.nuance,
                }
                for item in result.alternatives
            ],
            "usage_note": result.usage_note,
        }

    @staticmethod
    def _scenario_title(scenario: Scenario, language: str) -> str:
        return (
            f"{scenario.title_ru} · {scenario.title_pl}"
            if language == "ru"
            else scenario.title_pl
        )

    def _show_ai_unavailable(self, chat_id: int, language: str) -> None:
        user = self.store.get_user(chat_id)
        keyboard: Keyboard = [back_row(language, "toolkit", "toolkit")]
        self._prepend_activity_return(
            keyboard,
            user,
            language,
            cancel_phrase_input=bool(user["toolkit_input_mode"]),
        )
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.error_title"),
                tr(language, "toolkit.ai_unavailable"),
            ),
            keyboard,
            surface="toolkit_error",
        )

    @staticmethod
    def _resume_callback(user: Any) -> str | None:
        stage = str(user["stage"])
        if stage in {"scenario", "practice", "review"}:
            return "task:resume"
        if stage == "quest" and user["current_quest"]:
            return "quest:resume"
        if stage == "waiting" and user["pending_assignment"]:
            return "assignment:next"
        if stage == "drill" and user["current_drill"]:
            return "drill:resume"
        return None

    @classmethod
    def _prepend_activity_return(
        cls,
        keyboard: Keyboard,
        user: Any,
        language: str,
        *,
        cancel_phrase_input: bool = False,
    ) -> None:
        resume = cls._resume_callback(user)
        if resume:
            keyboard.insert(
                0,
                [
                    {
                        "text": tr(language, "action.return_to_activity"),
                        "callback_data": (
                            "toolkit:resume" if cancel_phrase_input else resume
                        ),
                    }
                ],
            )
