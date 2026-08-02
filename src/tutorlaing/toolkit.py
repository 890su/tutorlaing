from __future__ import annotations

import logging
from typing import Any

from .ai import AIClient, AIError, PhraseTranslation
from .catalog import ScenarioCatalog
from .content import Scenario
from .contracts import (
    Keyboard,
    TelegramGateway,
    ToolkitDelivery,
    ToolkitStore,
    TransportError,
)
from .i18n import tr
from .navigation import back_row, home_row
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
    ):
        self.store = store
        self.workspace = workspace
        self.catalog = catalog
        self.ai = ai
        self.delivery = delivery
        self.telegram = telegram

    def show_menu(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        if user["stage"] == "toolkit_input":
            self.store.set_user_state(chat_id, stage="idle", toolkit_input_mode=None)
        language = str(user["instruction_language"])
        target = LANGUAGE_LABELS.get(
            str(user["target_language"]), str(user["target_language"])
        )
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.title"),
                tr(language, "toolkit.summary"),
            ),
            [
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
            ],
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
        if active is not None:
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
        if user["stage"] not in {"idle", "new"}:
            self._show_busy(chat_id, language)
            return
        if self.ai is None:
            self._show_ai_unavailable(chat_id, language)
            return

        try:
            material = self._pack_material(user, mode, scenario_id)
        except KeyError:
            self.show_topics(chat_id)
            return
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.preparing"),
                tr(language, f"toolkit.preparing_{mode}"),
            ),
            surface="toolkit_preparing",
        )
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
            self.store.event(
                chat_id, "ai_fallback_used", {"operation": f"toolkit_{mode}"}
            )
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

        drill_id = self.store.start_drill(
            chat_id,
            None,
            pack.title,
            pack.focus,
            [item.to_dict() for item in pack.items],
            mode=f"toolkit_{mode}",
        )
        self.store.event(
            chat_id,
            "toolkit_started",
            {"mode": mode, "scenario_id": scenario_id},
        )
        self.delivery.send_drill_item(chat_id, drill_id)
        self.delivery.schedule_next_assignment(chat_id)

    def ask_for_phrase(self, chat_id: int, mode: str) -> None:
        if mode not in INPUT_MODES:
            raise ValueError(f"Unsupported phrase mode: {mode}")
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        if self.store.active_drill(chat_id) is not None or user["stage"] not in {
            "idle",
            "new",
            "toolkit_input",
        }:
            self._show_busy(chat_id, language)
            return
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
        self.store.set_user_state(
            chat_id, stage="toolkit_input", toolkit_input_mode=mode
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
            [
                back_row(language, "toolkit", "toolkit")
            ],
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
            self.workspace.show(
                chat_id,
                tr(language, "toolkit.phrase_invalid"),
                [back_row(language, "toolkit", "toolkit")],
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
            self.workspace.show(
                chat_id,
                card(
                    tr(language, "toolkit.error_title"),
                    tr(language, "toolkit.translation_error"),
                ),
                [
                    [
                        {
                            "text": tr(language, "toolkit.retry"),
                            "callback_data": f"toolkit:translate:{mode}",
                        }
                    ],
                    back_row(language, "toolkit", "toolkit"),
                ],
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
        self.store.set_user_state(chat_id, stage="idle", toolkit_input_mode=None)
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
        self.workspace.show(
            chat_id,
            card(tr(language, "toolkit.translation_result"), "\n\n".join(blocks)),
            [
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
            ],
            force_new=True,
            surface="toolkit_translation_result",
        )

    def _pack_material(
        self, user: Any, mode: str, scenario_id: str | None
    ) -> dict[str, Any]:
        scenarios = self.catalog.for_user(user)
        if mode == "topic":
            scenario = scenarios[scenario_id or ""]
            return self._scenario_material(scenario)
        return {
            "phrases": [
                {
                    "topic": scenario.title_pl,
                    "practical_meaning_ru": step.context_ru,
                    "target_phrase": step.target_chunk,
                }
                for scenario in scenarios.values()
                for step in scenario.steps
            ],
            "learner_level": str(user["learner_level"]),
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

    def _show_busy(self, chat_id: int, language: str) -> None:
        user = self.store.get_user(chat_id)
        resume_callback = "drill:resume" if user["current_drill"] else "task:resume"
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.busy_title"),
                tr(language, "toolkit.busy_summary"),
            ),
            [
                [
                    {
                        "text": tr(language, "action.resume_task"),
                        "callback_data": resume_callback,
                    }
                ],
                home_row(language),
            ],
            surface="toolkit_busy",
        )

    def _show_ai_unavailable(self, chat_id: int, language: str) -> None:
        self.workspace.show(
            chat_id,
            card(
                tr(language, "toolkit.error_title"),
                tr(language, "toolkit.ai_unavailable"),
            ),
            [back_row(language, "toolkit", "toolkit")],
            surface="toolkit_error",
        )
