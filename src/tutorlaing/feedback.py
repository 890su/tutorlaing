from __future__ import annotations

import json
import logging
from typing import Any

from .ai import AIClient, AIError, ResponseAnalysis
from .contracts import FeedbackStore, Keyboard
from .i18n import tr
from .language_support import LanguageSupport
from .navigation import home_row
from .workspace import TelegramWorkspace


LOGGER = logging.getLogger(__name__)


class FeedbackPresenter:
    """Presents persisted AI analysis as editable Telegram tabs."""

    def __init__(
        self,
        store: FeedbackStore,
        workspace: TelegramWorkspace,
        language_support: LanguageSupport,
        ai: AIClient | None,
    ):
        self.store = store
        self.workspace = workspace
        self.language_support = language_support
        self.ai = ai

    def stored_analysis(
        self, chat_id: int, analysis_id: int
    ) -> tuple[Any, ResponseAnalysis]:
        row = self.store.get_ai_analysis(analysis_id, chat_id)
        return row, ResponseAnalysis.from_dict(json.loads(row["result_json"]))

    def tabs(self, chat_id: int, analysis_id: int, active: str) -> Keyboard:
        language = self._language(chat_id)
        tabs = [
            ("result", "tab.result"),
            ("variants", "tab.variants"),
            ("grammar", "tab.grammar"),
            ("translate", "tab.translation"),
        ]
        keyboard: Keyboard = [
            [
                {
                    "text": ("• " if key == active else "") + tr(language, label),
                    "callback_data": f"ai:{key}:{analysis_id}",
                }
                for key, label in tabs[:3]
            ],
            [
                {
                    "text": ("• " if key == active else "") + tr(language, label),
                    "callback_data": f"ai:{key}:{analysis_id}",
                }
                for key, label in tabs[3:]
            ],
        ]
        if self.store.get_user(chat_id)["pending_assignment"]:
            keyboard.insert(
                0,
                [{"text": tr(language, "action.next"), "callback_data": "assignment:next"}],
            )
        keyboard.append(home_row(language))
        return keyboard

    def show_result(
        self,
        chat_id: int,
        analysis: ResponseAnalysis,
        analysis_id: int,
        *,
        continuation: bool = False,
        force_new: bool = False,
    ) -> None:
        language = self._language(chat_id)
        lines = [
            tr(language, "feedback.achieved")
            if analysis.task_achieved
            else tr(language, "feedback.partial")
        ]
        if analysis.positive_feedback:
            lines.append(f"\n{tr(language, 'feedback.good')}: {analysis.positive_feedback}")
        if analysis.critical_corrections:
            lines.append(f"\n{tr(language, 'feedback.fix')}:")
            lines.extend(f"• {item}" for item in analysis.critical_corrections)
        elif analysis.optional_improvements:
            lines.append(f"\n{tr(language, 'feedback.improve')}:")
            lines.extend(f"• {item}" for item in analysis.optional_improvements)
        if analysis.natural_response:
            lines.append(
                f"\n{tr(language, 'feedback.natural')}:\n{analysis.natural_response}"
            )
        if analysis.pragmatic_note:
            lines.append(
                f"\n{tr(language, 'feedback.pragmatic')}: {analysis.pragmatic_note}"
            )

        keyboard = self.tabs(chat_id, analysis_id, "result")
        if continuation:
            lines.append(f"\n{self._continuation_text(chat_id)}")
        keyboard.append(
            [
                {"text": "👍", "callback_data": f"ai:rate:{analysis_id}:up"},
                {"text": "👎", "callback_data": f"ai:rate:{analysis_id}:down"},
            ]
        )
        self.workspace.show(
            chat_id,
            "".join(lines),
            keyboard,
            force_new=force_new,
            surface="scenario_feedback",
        )

    def show_variants(self, chat_id: int, analysis_id: int) -> None:
        _, analysis = self.stored_analysis(chat_id, analysis_id)
        if not analysis.alternatives:
            self.workspace.show(
                chat_id,
                self.language_support.instruction_text(
                    chat_id,
                    "Для этого ответа дополнительных вариантов нет.",
                    "feedback-no-variants",
                ),
                self.tabs(chat_id, analysis_id, "variants"),
                surface="feedback_variants",
            )
            return
        labels = {
            key: tr(self._language(chat_id), f"variant.{key}")
            for key in ("neutral", "formal", "informal")
        }
        blocks = []
        for item in analysis.alternatives:
            heading = labels.get(item.register, item.register)
            nuance = f"\n{item.nuance}" if item.nuance else ""
            blocks.append(f"{heading}:\n{item.text}{nuance}")
        self.workspace.show(
            chat_id,
            "\n\n".join(blocks),
            self.tabs(chat_id, analysis_id, "variants"),
            surface="feedback_variants",
        )
        self.store.event(
            chat_id, "natural_variants_requested", {"analysis_id": analysis_id}
        )

    def show_grammar_choices(self, chat_id: int, analysis_id: int) -> None:
        _, analysis = self.stored_analysis(chat_id, analysis_id)
        language = self._language(chat_id)
        keyboard: Keyboard = [
            [
                {
                    "text": tr(language, "grammar.whole"),
                    "callback_data": f"ai:g:{analysis_id}:all",
                }
            ]
        ]
        if analysis.grammar_chunks:
            keyboard.append(
                [
                    {
                        "text": str(index + 1),
                        "callback_data": f"ai:g:{analysis_id}:{index}",
                    }
                    for index, _ in enumerate(analysis.grammar_chunks)
                ]
            )
        keyboard.append(
            [
                {
                    "text": tr(language, "grammar.custom"),
                    "callback_data": f"ai:g:{analysis_id}:custom",
                }
            ]
        )
        keyboard.extend(self.tabs(chat_id, analysis_id, "grammar"))
        self.workspace.show(
            chat_id,
            tr(language, "grammar.choose")
            + (
                "\n\n"
                + tr(language, "grammar.fragments")
                + ":\n"
                + "\n".join(
                    f"{index + 1}. {chunk.text} — {chunk.label}"
                    for index, chunk in enumerate(analysis.grammar_chunks)
                )
                if analysis.grammar_chunks
                else ""
            ),
            keyboard,
            surface="feedback_grammar",
        )

    def explain_grammar(self, chat_id: int, analysis_id: int, selection: str) -> None:
        row, analysis = self.stored_analysis(chat_id, analysis_id)
        sentence = analysis.natural_response or str(row["source_text"])
        if selection == "custom":
            self.workspace.show(
                chat_id,
                self.language_support.instruction_text(
                    chat_id,
                    "Ответьте командой /grammar и укажите фрагмент, например:\n/grammar od dwóch dni",
                    "grammar-custom-help",
                ),
                self.tabs(chat_id, analysis_id, "grammar"),
                surface="feedback_grammar",
            )
            return
        if selection == "all":
            fragment = sentence
        else:
            try:
                fragment = analysis.grammar_chunks[int(selection)].text
            except (ValueError, IndexError):
                self.workspace.show(
                    chat_id,
                    self.language_support.instruction_text(
                        chat_id,
                        "Этот фрагмент больше недоступен.",
                        "grammar-fragment-unavailable",
                    ),
                    surface="feedback_grammar",
                )
                return
        self._explain_and_show(chat_id, row, analysis_id, sentence, fragment)

    def explain_custom_grammar(self, chat_id: int, fragment: str) -> None:
        user = self.store.get_user(chat_id)
        row = self.store.latest_ai_analysis(chat_id, str(user["target_language"]))
        if row is None:
            self.workspace.show(
                chat_id,
                self.language_support.instruction_text(
                    chat_id,
                    "Сначала напишите ответ в учебном сценарии.",
                    "grammar-no-analysis",
                ),
                surface="feedback_grammar",
            )
            return
        analysis = ResponseAnalysis.from_dict(json.loads(row["result_json"]))
        sentence = analysis.natural_response or str(row["source_text"])
        self._explain_and_show(
            chat_id, row, int(row["id"]), sentence, fragment, selection="custom"
        )

    def translate_analysis(self, chat_id: int, analysis_id: int) -> None:
        _, analysis = self.stored_analysis(chat_id, analysis_id)
        source = "\n".join(
            part
            for part in [
                analysis.positive_feedback,
                *analysis.critical_corrections,
                *analysis.optional_improvements,
                analysis.natural_response,
                analysis.pragmatic_note,
                analysis.explanation,
            ]
            if part
        )
        user = self.store.get_user(chat_id)
        translated = self.language_support.translate_text(
            chat_id,
            source,
            str(user["translation_language"]),
            "AI feedback",
        )
        text = (
            f"🌐 {translated}"
            if translated
            else self.language_support.instruction_text(
                chat_id, "Перевод сейчас недоступен.", "feedback-translation-error"
            )
        )
        self.workspace.show(
            chat_id,
            text,
            self.tabs(chat_id, analysis_id, "translate"),
            surface="feedback_translation",
        )

    def _explain_and_show(
        self,
        chat_id: int,
        row: Any,
        analysis_id: int,
        sentence: str,
        fragment: str,
        *,
        selection: str = "selected",
    ) -> None:
        if self.ai is None:
            self.workspace.show(
                chat_id,
                self.language_support.instruction_text(
                    chat_id, "AI-разбор сейчас недоступен.", "grammar-ai-disabled"
                ),
                surface="feedback_grammar",
            )
            return
        user = self.store.get_user(chat_id)
        try:
            result = self.ai.explain_grammar(
                sentence,
                fragment,
                str(user["instruction_language"]),
                str(user["target_language"]),
            )
        except AIError:
            LOGGER.exception("AI grammar explanation failed")
            self.store.event(chat_id, "ai_analysis_failed", {"operation": "grammar"})
            self.workspace.show(
                chat_id,
                self.language_support.instruction_text(
                    chat_id,
                    "Не удалось получить разбор. Попробуйте позже.",
                    "grammar-error",
                ),
                surface="feedback_grammar",
            )
            return

        self.store.add_ai_analysis(
            chat_id=chat_id,
            operation="grammar",
            source_text=fragment,
            result=result,
            provider=self.ai.provider,
            model=self.ai.model,
            prompt_version="grammar-v1",
            latency_ms=0,
            session_id=row["session_id"],
            scenario_id=row["scenario_id"],
            step_index=row["step_index"],
        )
        parts = [
            f"📚 {fragment}",
            f"\n{result['meaning']}",
            f"\n{result['explanation']}",
        ]
        if result["contrast_example"]:
            parts.append(f"\n{result['contrast_example']}")
        if result["common_error"]:
            parts.append(f"\n{result['common_error']}")
        self.workspace.show(
            chat_id,
            "".join(parts),
            self.tabs(chat_id, analysis_id, "grammar"),
            surface="feedback_grammar",
        )
        self.store.event(
            chat_id,
            "grammar_explanation_requested",
            {"analysis_id": analysis_id, "selection": selection},
        )

    def _language(self, chat_id: int) -> str:
        return str(self.store.get_user(chat_id)["instruction_language"])

    def _continuation_text(self, chat_id: int) -> str:
        language = self._language(chat_id)
        mode = str(self.store.get_user(chat_id)["reminder_mode"])
        key = "task.next_manual" if mode == "off" else "task.next_or_wait"
        return tr(language, key)
