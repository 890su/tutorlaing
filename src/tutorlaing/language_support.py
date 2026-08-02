from __future__ import annotations

import logging

from .ai import AIClient, AIError
from .contracts import LanguageStore
from .i18n import tr


LOGGER = logging.getLogger(__name__)


class LanguageSupport:
    """Translation and level-aware glossary use cases.

    This service knows language preferences and AI persistence, but has no
    Telegram dependency.  Callers decide where and how returned text is shown.
    """

    def __init__(self, store: LanguageStore, ai: AIClient | None):
        self.store = store
        self.ai = ai

    def translate_text(
        self, chat_id: int, text: str, language: str, context: str
    ) -> str | None:
        if language == "ru" and context.startswith("instruction"):
            return text
        if self.ai is None:
            return None
        self.store.event(
            chat_id,
            "translation_requested",
            {"language": language, "context": context},
        )
        try:
            result = self.ai.translate(text, language, context)
        except AIError:
            LOGGER.exception("AI translation failed")
            self.store.event(chat_id, "ai_analysis_failed", {"operation": "translation"})
            return None

        translation = str(result.get("translation", "")).strip()
        note = str(result.get("note", "")).strip()
        stored = {"translation": translation, "note": note}
        self.store.add_ai_analysis(
            chat_id=chat_id,
            operation="translation",
            source_text=text,
            result=stored,
            provider=self.ai.provider,
            model=self.ai.model,
            prompt_version="translation-v1",
            latency_ms=0,
        )
        return f"{translation}\n\n💬 {note}" if note else translation

    def instruction_text(self, chat_id: int, text: str, context: str) -> str:
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        translated = self.translate_text(
            chat_id, text, language, f"instruction:{context}"
        )
        return translated or text

    def glossary_footnotes(self, chat_id: int, target_text: str) -> str:
        user = self.store.get_user(chat_id)
        level = str(user["learner_level"])
        if self.ai is None or level == "C1" or len(target_text) < 10:
            return ""
        try:
            notes = self.ai.glossary_notes(
                target_text,
                level,
                str(user["target_language"]),
                str(user["translation_language"]),
            )
        except AIError:
            LOGGER.exception("AI glossary generation failed")
            self.store.event(chat_id, "ai_fallback_used", {"operation": "glossary"})
            return ""
        if not notes:
            return ""

        language = str(user["instruction_language"])
        lines = [f"{tr(language, 'glossary.label')}:"]
        lines.extend(
            f"• {note['term']} — {note['translation']} ({note['cefr']})"
            for note in notes
        )
        return "\n\n" + "\n".join(lines)
