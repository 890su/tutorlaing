from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .ai import (
    AIClient,
    AIError,
    DrillEvaluation,
    DrillItem,
    GeminiClient,
    ResponseAnalysis,
)
from .config import Settings
from .content import Scenario, load_scenarios
from .engine import (
    evaluate_response,
    normalize,
    review_due_at,
    review_interval_days,
    select_bottleneck,
)
from .i18n import tr
from .reminders import next_reminder_at, pause_until_tomorrow
from .storage import Storage
from .telegram_api import TelegramAPI, TelegramError
from .ui import card, progress, route


LOGGER = logging.getLogger(__name__)
CONSENT_VERSION = 2
LANGUAGE_LABELS = {"ru": "Русский", "uk": "Українська", "en": "English", "pl": "Polski"}
TARGET_COURSE_LABELS = {"pl": "Polski w praktyce", "en": "English in practice"}
TARGET_ADVERBS_RU = {"pl": "по-польски", "en": "по-английски"}
TARGET_NOUNS_RU = {"pl": "польский", "en": "английский"}
REMINDER_LABELS = {
    "off": "выключены",
    "gentle": "мягкие",
    "normal": "обычные",
    "intensive": "интенсивные",
    "aggressive": "агрессивные",
}


class TutorlaingBot:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        telegram: TelegramAPI | None = None,
        ai: AIClient | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.telegram = telegram or TelegramAPI(settings.telegram_bot_token)
        self.ai = ai
        if self.ai is None and settings.ai_enabled:
            self.ai = GeminiClient(
                settings.gemini_api_key,
                settings.gemini_model,
                settings.ai_timeout,
            )
        self.scenarios_by_language = {
            "pl": load_scenarios("pl"),
            "en": load_scenarios("en"),
        }
        self.offset = 0
        self.running = True

    def is_allowed(self, chat_id: int) -> bool:
        allowed = self.settings.allowed_chat_ids
        return allowed is None or chat_id in allowed

    def _scenarios_for_user(self, user: Any) -> dict[str, Scenario]:
        return self.scenarios_by_language.get(
            str(user["target_language"]), self.scenarios_by_language["pl"]
        )

    def _scenarios_for_chat(self, chat_id: int) -> dict[str, Scenario]:
        return self._scenarios_for_user(self.storage.get_user(chat_id))

    def _language(self, chat_id: int) -> str:
        return str(self.storage.get_user(chat_id)["instruction_language"])

    def _t(self, chat_id: int, key: str, **values: Any) -> str:
        return tr(self._language(chat_id), key, **values)

    def _workspace(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
        *,
        force_new: bool = False,
        surface: str = "learning",
    ) -> int | None:
        user = self.storage.get_user(chat_id)
        message_id = user["workspace_message_id"]
        if message_id and not force_new:
            try:
                self.telegram.edit_message(
                    chat_id, int(message_id), text, keyboard
                )
                self.storage.event(
                    chat_id,
                    "ui_message_edited",
                    {"surface": surface},
                )
                return int(message_id)
            except TelegramError as exc:
                if "message is not modified" in str(exc).lower():
                    return int(message_id)
                LOGGER.info("Workspace edit failed; sending a new card", exc_info=True)
        result = self.telegram.send_message(chat_id, text, keyboard)
        new_message_id = (
            int(result["message_id"])
            if isinstance(result, dict) and result.get("message_id")
            else None
        )
        if new_message_id is not None:
            self.storage.set_user_state(
                chat_id, workspace_message_id=new_message_id
            )
        self.storage.event(
            chat_id,
            "ui_message_sent",
            {"surface": surface, "scheduled": force_new},
        )
        return new_message_id

    def home(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        language = str(user["instruction_language"])
        due_count = len(self.storage.pending_reviews(chat_id))
        reminder_label = tr(language, f"reminder.{user['reminder_mode']}")
        if due_count:
            primary = [{"text": f"{tr(language, 'action.reviews')} · {due_count}", "callback_data": "reviews:list"}]
        else:
            primary = [{"text": tr(language, "action.start_situation"), "callback_data": "scenarios:list"}]
        self._workspace(
            chat_id,
            card(
                tr(language, f"home.title.{user['target_language']}"),
                tr(
                    language,
                    "home.summary",
                    due=due_count,
                    reminders=reminder_label,
                ),
            ),
            [
                primary,
                [
                    {"text": tr(language, "action.practice"), "callback_data": "drill:start"},
                    {"text": tr(language, "action.reviews"), "callback_data": "reviews:list"},
                ],
                [{"text": tr(language, "action.progress"), "callback_data": "progress"}],
                [{"text": tr(language, "action.reminders"), "callback_data": "reminders"}],
                [{"text": tr(language, "action.settings"), "callback_data": "settings"}],
            ],
            surface="home",
        )

    def start(self, chat_id: int, first_name: str = "") -> None:
        user = self.storage.ensure_user(chat_id, first_name)
        if not self._has_current_consent(user):
            self._workspace(
                chat_id,
                "Cześć! Я помогу подготовиться к реальным разговорам на новом языке.\n\n"
                "Alpha сохраняет ваши текстовые ответы, результаты и Telegram ID. "
                "Для персональной проверки учебная реплика и минимальный контекст "
                "отправляются Google Gemini. Имя и Telegram ID в AI не передаются. "
                "Голос не записывается. Все данные можно удалить командой /delete_me.\n\n"
                "Продолжить?",
                [
                    [{"text": "✅ Согласен и начать", "callback_data": "consent:accept"}],
                    [{"text": "ℹ️ Подробнее", "callback_data": "privacy"}],
                ],
            )
            return
        self.home(chat_id)

    @staticmethod
    def _has_current_consent(user: Any) -> bool:
        return bool(user["consent_at"]) and int(user["consent_version"]) >= CONSENT_VERSION

    def show_settings(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        language = str(user["instruction_language"])
        instruction = LANGUAGE_LABELS.get(user["instruction_language"], user["instruction_language"])
        translation = LANGUAGE_LABELS.get(user["translation_language"], user["translation_language"])
        target = LANGUAGE_LABELS.get(user["target_language"], user["target_language"])
        self._workspace(
            chat_id,
            card(
                tr(language, "settings.title"),
                tr(
                    language,
                    "settings.summary",
                    instruction=instruction,
                    translation=translation,
                    target=target,
                    level=user["learner_level"],
                ),
            ),
            [
                [{"text": tr(language, "settings.instruction"), "callback_data": "settings:instruction"}],
                [{"text": tr(language, "settings.translation"), "callback_data": "settings:translation"}],
                [{"text": tr(language, "settings.target"), "callback_data": "settings:target"}],
                [{"text": tr(language, "settings.level"), "callback_data": "settings:level"}],
                [{"text": tr(language, "action.reminders"), "callback_data": "reminders"}],
                [{"text": tr(language, "action.progress"), "callback_data": "progress"}],
                [{"text": tr(language, "settings.privacy"), "callback_data": "privacy"}],
                [{"text": tr(language, "action.back"), "callback_data": "home"}],
            ],
            surface="settings",
        )

    def show_language_choices(self, chat_id: int, kind: str) -> None:
        if kind == "target":
            choices = [("pl", "🇵🇱 Polski"), ("en", "🇬🇧 English")]
            heading = self._t(chat_id, "settings.choose_target")
        else:
            choices = [
                ("ru", "🇷🇺 Русский"),
                ("uk", "🇺🇦 Українська"),
                ("en", "🇬🇧 English"),
                ("pl", "🇵🇱 Polski"),
            ]
            heading = self._t(chat_id, "settings.choose")
        keyboard = [
            [{"text": label, "callback_data": f"settings:set:{kind}:{code}"}]
            for code, label in choices
        ]
        keyboard.append([{"text": self._t(chat_id, "action.back"), "callback_data": "settings"}])
        self._workspace(chat_id, heading, keyboard, surface="language_settings")

    def show_level_choices(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        current = str(user["learner_level"])
        keyboard = [
            [
                {
                    "text": ("✓ " if level == current else "") + level,
                    "callback_data": f"settings:set:level:{level}",
                }
                for level in ("A0", "A1", "A2")
            ],
            [
                {
                    "text": ("✓ " if level == current else "") + level,
                    "callback_data": f"settings:set:level:{level}",
                }
                for level in ("B1", "B2", "C1")
            ],
            [{"text": self._t(chat_id, "action.back"), "callback_data": "settings"}],
        ]
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "settings.level"),
                self._t(chat_id, "progress.disclaimer"),
            ),
            keyboard,
            surface="level",
        )

    def show_progress(self, chat_id: int) -> None:
        evidence = self.storage.progress_evidence(chat_id)
        scenarios = self._scenarios_for_chat(chat_id)
        session_by_id = {row["scenario_id"]: row for row in evidence["sessions"]}
        review_by_id = {row["scenario_id"]: row for row in evidence["reviews"]}
        mastered: list[str] = []
        focus: list[str] = []
        untouched: list[str] = []
        for scenario_id, scenario in scenarios.items():
            session = session_by_id.get(scenario_id)
            review = review_by_id.get(scenario_id)
            title = scenario.title_pl
            if review and int(review["completed"] or 0) > 0 and float(review["best_score"] or 0) >= 0.6:
                mastered.append(title)
            elif session and (
                float(session["best_score"] or 0) < 0.8
                or (review and int(review["pending"] or 0) > 0)
            ):
                focus.append(title)
            elif session:
                focus.append(title)
            else:
                untouched.append(title)
        plan = (focus + untouched)[:3]
        empty = self._t(chat_id, "progress.empty")

        def lines(items: list[str]) -> str:
            return "\n".join(f"• {item}" for item in items) if items else empty

        body = (
            f"{self._t(chat_id, 'progress.level', level=evidence['level'])}\n"
            f"{progress('', len(mastered), len(scenarios)).splitlines()[-1]}\n\n"
            f"{self._t(chat_id, 'progress.mastered')}\n{lines(mastered)}\n\n"
            f"{self._t(chat_id, 'progress.focus')}\n{lines(focus[:4])}\n\n"
            f"{self._t(chat_id, 'progress.plan')}\n{lines(plan)}\n\n"
            f"{self._t(chat_id, 'progress.disclaimer')}"
        )
        keyboard: list[list[dict[str, str]]] = []
        if plan:
            planned_id = next(
                scenario_id
                for scenario_id, scenario in scenarios.items()
                if scenario.title_pl == plan[0]
            )
            keyboard.append(
                [
                    {
                        "text": f"▶ {plan[0]}"[:60],
                        "callback_data": f"scenario:{planned_id}",
                    }
                ]
            )
        keyboard.extend(
            [
                [{"text": self._t(chat_id, "settings.level"), "callback_data": "settings:level"}],
                [{"text": self._t(chat_id, "action.back"), "callback_data": "home"}],
            ]
        )
        self._workspace(
            chat_id,
            card(self._t(chat_id, "progress.title"), body),
            keyboard,
            surface="progress",
        )

    def show_reminders(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        mode = str(user["reminder_mode"])
        language = self._language(chat_id)
        paused = user["reminder_paused_until"]
        pause_text = f"\n{tr(language, 'reminders.paused', until=paused)}" if paused else ""
        next_text = ""
        if user["reminder_next_at"] and mode != "off":
            scheduled = datetime.fromisoformat(str(user["reminder_next_at"]))
            local = scheduled.astimezone(ZoneInfo(str(user["timezone"])))
            next_text = "\n" + tr(language, "reminders.next", date=local.strftime('%d.%m'), time=local.strftime('%H:%M'))
        keyboard = []
        for value in REMINDER_LABELS:
            label = tr(language, f"reminder.{value}")
            marker = "✓ " if value == mode else ""
            keyboard.append(
                [{"text": marker + label.capitalize(), "callback_data": f"reminder:set:{value}"}]
            )
        if mode != "off":
            keyboard.append([{"text": tr(language, "action.pause"), "callback_data": "reminder:pause"}])
        keyboard.append([{"text": tr(language, "action.back"), "callback_data": "home"}])
        self._workspace(
            chat_id,
            card(
                tr(language, "reminders.title"),
                f"{tr(language, 'reminders.current', mode=tr(language, f'reminder.{mode}'))}\n{tr(language, f'reminder.desc.{mode}')}"
                f"{next_text}{pause_text}\n\n"
                f"{tr(language, 'reminders.quiet')}",
            ),
            keyboard,
            surface="reminders",
        )

    def set_reminder_mode(self, chat_id: int, mode: str) -> None:
        user = self.storage.get_user(chat_id)
        next_at = next_reminder_at(mode, timezone_name=str(user["timezone"]))
        self.storage.set_reminder_mode(chat_id, mode, next_at)
        self.show_reminders(chat_id)

    def _schedule_next_assignment(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        mode = str(user["reminder_mode"])
        next_at = next_reminder_at(
            mode, timezone_name=str(user["timezone"])
        )
        self.storage.schedule_next_reminder(chat_id, next_at)

    def _drill_continuation_text(self, chat_id: int) -> str:
        user = self.storage.get_user(chat_id)
        if str(user["reminder_mode"]) == "off":
            return self._t(chat_id, "task.next_manual")
        return self._t(chat_id, "task.next_or_wait")

    def show_scenarios(self, chat_id: int) -> None:
        scenarios = self._scenarios_for_chat(chat_id)
        language = self._language(chat_id)
        keyboard = [
            [
                {
                    "text": (
                        f"{scenario.title_ru} · {scenario.title_pl}"
                        if language == "ru"
                        else scenario.title_pl
                    ),
                    "callback_data": f"scenario:{scenario.id}",
                }
            ]
            for scenario in scenarios.values()
        ]
        keyboard.append([{"text": self._t(chat_id, "action.back"), "callback_data": "home"}])
        self._workspace(
            chat_id,
            self._t(chat_id, "task.choose_scenario"),
            keyboard,
            surface="scenario_list",
        )

    def _translate_text(
        self, chat_id: int, text: str, language: str, context: str
    ) -> str | None:
        if language == "ru" and context.startswith("instruction"):
            return text
        if self.ai is None:
            return None
        self.storage.event(chat_id, "translation_requested", {"language": language, "context": context})
        try:
            result = self.ai.translate(text, language, context)
        except AIError:
            LOGGER.exception("AI translation failed")
            self.storage.event(chat_id, "ai_analysis_failed", {"operation": "translation"})
            return None
        translation = str(result.get("translation", "")).strip()
        note = str(result.get("note", "")).strip()
        stored = {"translation": translation, "note": note}
        self.storage.add_ai_analysis(
            chat_id=chat_id,
            operation="translation",
            source_text=text,
            result=stored,
            provider=self.ai.provider,
            model=self.ai.model,
            prompt_version="translation-v1",
            latency_ms=0,
        )
        if note:
            return f"{translation}\n\n💬 {note}"
        return translation

    def _instruction_text(self, chat_id: int, text: str, context: str) -> str:
        user = self.storage.get_user(chat_id)
        language = str(user["instruction_language"])
        translated = self._translate_text(chat_id, text, language, f"instruction:{context}")
        return translated or text

    def _glossary_footnotes(self, chat_id: int, target_text: str) -> str:
        user = self.storage.get_user(chat_id)
        if self.ai is None or str(user["learner_level"]) == "C1" or len(target_text) < 10:
            return ""
        try:
            notes = self.ai.glossary_notes(
                target_text,
                str(user["learner_level"]),
                str(user["target_language"]),
                str(user["translation_language"]),
            )
        except (AIError, AttributeError):
            LOGGER.exception("AI glossary generation failed")
            self.storage.event(chat_id, "ai_fallback_used", {"operation": "glossary"})
            return ""
        if not notes:
            return ""
        lines = [f"{self._t(chat_id, 'glossary.label')}:"]
        lines.extend(
            f"• {note['term']} — {note['translation']} ({note['cefr']})"
            for note in notes
        )
        return "\n\n" + "\n".join(lines)

    def begin_scenario(self, chat_id: int, scenario_id: str) -> None:
        user = self.storage.get_user(chat_id)
        scenario = self._scenarios_for_user(user).get(scenario_id)
        if not scenario:
            self.telegram.send_message(chat_id, "Этот сценарий не найден.")
            return
        current_user = user
        if current_user["current_drill"]:
            self.storage.abandon_drill(str(current_user["current_drill"]), chat_id)
        self.storage.start_session(chat_id, scenario_id)
        description = self._instruction_text(
            chat_id,
            f"Цель: {scenario.objective_ru}\nСитуация: {scenario.opening_ru}",
            "scenario-opening",
        )
        self.send_scenario_step(chat_id, scenario, 0, intro=description)

    def send_scenario_step(
        self,
        chat_id: int,
        scenario: Scenario,
        step_index: int,
        *,
        intro: str = "",
        scheduled: bool = False,
    ) -> None:
        step = scenario.steps[step_index]
        instruction = self._instruction_text(chat_id, step.context_ru, "scenario-step")
        language = self._language(chat_id)
        opening = f"{intro}\n\n" if intro else ""
        footnotes = self._glossary_footnotes(chat_id, step.interlocutor_pl)
        self._workspace(
            chat_id,
            f"{progress(scenario.title_pl, step_index + 1, len(scenario.steps))}\n\n"
            f"{opening}{tr(language, 'task.speaker')}\n{step.interlocutor_pl}\n\n"
            f"{tr(language, 'task.your_task')}\n{instruction}{footnotes}",
            [
                [{"text": f"💡 {tr(language, 'action.hint')}", "callback_data": "hint"}],
                [
                    {
                        "text": tr(language, "action.translate_task"),
                        "callback_data": f"task:translate:{scenario.id}:{step_index}",
                    }
                ],
                [{"text": tr(language, "action.stop"), "callback_data": "cancel"}],
            ],
            force_new=scheduled,
            surface="scenario_task",
        )

    def _evaluate_with_ai(
        self,
        chat_id: int,
        user: Any,
        scenario: Scenario,
        step_index: int,
        response: str,
    ) -> tuple[Any, ResponseAnalysis | None, int | None]:
        step = scenario.steps[step_index]
        rule_evaluation = evaluate_response(step, response)
        if self.ai is None:
            return rule_evaluation, None, None
        self.storage.event(
            chat_id,
            "ai_analysis_requested",
            {"operation": "response_analysis", "scenario_id": scenario.id},
        )
        try:
            analysis = self.ai.analyze_response(
                step,
                response,
                str(user["instruction_language"]),
                str(user["target_language"]),
                rule_evaluation.score,
            )
        except AIError:
            LOGGER.exception("AI response analysis failed")
            self.storage.event(
                chat_id,
                "ai_fallback_used",
                {"operation": "response_analysis", "scenario_id": scenario.id},
            )
            return rule_evaluation, None, None

        score = analysis.score
        if analysis.task_achieved and score < 0.6:
            score = 0.6
        elif not analysis.task_achieved and score >= 0.6:
            score = 0.59
        evaluation = type(rule_evaluation)(
            score,
            rule_evaluation.matched_groups,
            rule_evaluation.missing_groups,
        )
        analysis_id = self.storage.add_ai_analysis(
            chat_id=chat_id,
            session_id=str(user["current_session"]) if user["current_session"] else None,
            scenario_id=scenario.id,
            step_index=step_index,
            operation="response_analysis",
            target_language=str(user["target_language"]),
            source_text=response,
            result=analysis.to_dict(),
            provider=analysis.provider,
            model=analysis.model,
            prompt_version=analysis.prompt_version,
            latency_ms=analysis.latency_ms,
            usage=analysis.usage,
        )
        self.storage.event(
            chat_id,
            "ai_analysis_completed",
            {
                "operation": "response_analysis",
                "scenario_id": scenario.id,
                "model": analysis.model,
                "latency_ms": analysis.latency_ms,
            },
        )
        return evaluation, analysis, analysis_id

    def _send_ai_feedback(
        self,
        chat_id: int,
        analysis: ResponseAnalysis,
        analysis_id: int,
        continuation: bool = False,
    ) -> None:
        language = self._language(chat_id)
        lines = [
            tr(language, "feedback.achieved")
            if analysis.task_achieved
            else tr(language, "feedback.partial"),
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
            lines.append(f"\n{tr(language, 'feedback.natural')}:\n{analysis.natural_response}")
        if analysis.pragmatic_note:
            lines.append(f"\n{tr(language, 'feedback.pragmatic')}: {analysis.pragmatic_note}")
        keyboard: list[list[dict[str, str]]] = [
            [
                {"text": f"• {tr(language, 'tab.result')}", "callback_data": f"ai:result:{analysis_id}"},
                {"text": tr(language, "tab.variants"), "callback_data": f"ai:variants:{analysis_id}"},
                {"text": tr(language, "tab.grammar"), "callback_data": f"ai:grammar:{analysis_id}"},
            ],
            [
                {"text": tr(language, "tab.translation"), "callback_data": f"ai:translate:{analysis_id}"},
            ],
        ]
        if continuation:
            lines.append(f"\n{self._drill_continuation_text(chat_id)}")
            keyboard.insert(
                0,
                [{"text": tr(language, "action.next"), "callback_data": "assignment:next"}],
            )
        keyboard.append(
            [
                {"text": "👍", "callback_data": f"ai:rate:{analysis_id}:up"},
                {"text": "👎", "callback_data": f"ai:rate:{analysis_id}:down"},
            ]
        )
        self._workspace(
            chat_id,
            "".join(lines),
            keyboard,
            surface="scenario_feedback",
        )

    def handle_scenario_response(self, chat_id: int, text: str, user: Any) -> None:
        scenario = self._scenarios_for_user(user)[user["current_scenario"]]
        step_index = int(user["current_step"])
        step = scenario.steps[step_index]
        evaluation, analysis, analysis_id = self._evaluate_with_ai(
            chat_id, user, scenario, step_index, text
        )
        session_id = str(user["current_session"])
        self.storage.add_response(
            session_id,
            step_index,
            "scenario",
            text,
            evaluation.score,
            evaluation.missing_groups,
        )

        next_step = step_index + 1
        if next_step < len(scenario.steps):
            self.storage.set_user_state(chat_id, current_step=next_step)
            assignment = f"scenario:{scenario.id}:{next_step}"
        else:
            assignment = f"practice:{scenario.id}:{session_id}"
        self.storage.queue_assignment(chat_id, assignment)
        self._schedule_next_assignment(chat_id)

        if analysis is not None and analysis_id is not None:
            self._send_ai_feedback(
                chat_id, analysis, analysis_id, continuation=True
            )
        elif evaluation.successful:
            self._workspace(
                chat_id,
                "✅ Коммуникативная задача выполнена.\n\n"
                + self._drill_continuation_text(chat_id),
                [[{"text": "Следующее задание →", "callback_data": "assignment:next"}]],
            )
        else:
            self._workspace(
                chat_id,
                "Ответ принят. В конце выберу одно главное затруднение для короткой тренировки.\n\n"
                + self._drill_continuation_text(chat_id),
                [[{"text": self._t(chat_id, "action.next"), "callback_data": "assignment:next"}]],
                surface="scenario_feedback",
            )

    def send_pending_assignment(self, chat_id: int, scheduled: bool = False) -> bool:
        assignment = self.storage.claim_pending_assignment(chat_id)
        if assignment is None:
            return False
        parts = assignment.split(":")
        user = self.storage.get_user(chat_id)
        scenarios = self._scenarios_for_user(user)
        if len(parts) == 3 and parts[0] == "scenario":
            scenario = scenarios.get(parts[1])
            if scenario is not None:
                step_index = int(parts[2])
                self.storage.set_user_state(
                    chat_id,
                    stage="scenario",
                    current_scenario=scenario.id,
                    current_step=step_index,
                )
                self.send_scenario_step(
                    chat_id, scenario, step_index, scheduled=scheduled
                )
                self._schedule_next_assignment(chat_id)
                return True
        if len(parts) == 3 and parts[0] == "practice":
            scenario = scenarios.get(parts[1])
            if scenario is not None:
                self.begin_practice(
                    chat_id, scenario, parts[2], scheduled=scheduled
                )
                self._schedule_next_assignment(chat_id)
                return True
        self.storage.set_user_state(chat_id, stage="idle")
        return False

    def begin_practice(
        self,
        chat_id: int,
        scenario: Scenario,
        session_id: str,
        *,
        scheduled: bool = False,
    ) -> None:
        scores = self.storage.scenario_scores(session_id)
        bottleneck_index = select_bottleneck(scenario, scores)
        step = scenario.steps[bottleneck_index]
        self.storage.set_user_state(
            chat_id, stage="practice", current_step=bottleneck_index
        )
        self.storage.event(
            chat_id,
            "bottleneck_selected",
            {
                "scenario_id": scenario.id,
                "step_index": bottleneck_index,
                "reason": step.bottleneck_ru,
            },
        )
        explanation = self._instruction_text(
            chat_id,
            f"Главное узкое место этой попытки: {step.bottleneck_ru}.",
            "practice-bottleneck",
        )
        instruction = self._instruction_text(
            chat_id,
            f"Напишите эту мысль {TARGET_ADVERBS_RU.get(str(self.storage.get_user(chat_id)['target_language']), 'на изучаемом языке')} своими словами. Можно использовать образец, "
            "но не копируйте его механически.",
            "practice-instruction",
        )
        block_label = self._instruction_text(chat_id, "Полезный блок:", "practice-label")
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "practice.title"),
                f"{explanation}\n\n{block_label}\n{step.target_chunk}\n\n{instruction}",
                "PHRASE",
                self._language(chat_id),
            ),
            [[{"text": self._t(chat_id, "practice.skip"), "callback_data": "practice:skip"}]],
            force_new=scheduled,
            surface="practice_task",
        )

    def handle_practice_response(self, chat_id: int, text: str, user: Any) -> None:
        scenario = self._scenarios_for_user(user)[user["current_scenario"]]
        step_index = int(user["current_step"])
        step = scenario.steps[step_index]
        session_id = str(user["current_session"])
        evaluation, analysis, analysis_id = self._evaluate_with_ai(
            chat_id, user, scenario, step_index, text
        )
        self.storage.add_response(
            session_id,
            step_index,
            "practice",
            text,
            evaluation.score,
            evaluation.missing_groups,
        )
        attempts = self.storage.response_count(session_id, "practice")
        if analysis is not None and analysis_id is not None:
            self._send_ai_feedback(chat_id, analysis, analysis_id)
        if evaluation.successful:
            self.finish_session(chat_id, scenario, session_id, step_index, evaluation.score)
        elif attempts < 2:
            retry = self._instruction_text(
                chat_id, "Пока не хватает части смысла. Попробуйте ещё раз:", "practice-retry"
            )
            self._workspace(
                chat_id,
                f"{retry}\n{step.target_chunk}",
                surface="practice_retry",
            )
        else:
            fallback = self._instruction_text(
                chat_id, "Зафиксируем образец и вернёмся к нему позже:", "practice-fallback"
            )
            self._workspace(
                chat_id,
                f"{fallback}\n{step.target_chunk}",
                surface="practice_feedback",
            )
            self.finish_session(chat_id, scenario, session_id, step_index, evaluation.score)

    def finish_session(
        self,
        chat_id: int,
        scenario: Scenario,
        session_id: str,
        bottleneck_index: int,
        practice_score: float,
    ) -> None:
        scores = self.storage.scenario_scores(session_id)
        average = sum(score for _, score in scores) / len(scores) if scores else 0.0
        self.storage.complete_session(session_id, bottleneck_index, average)
        interval = review_interval_days(practice_score)
        due_at = review_due_at(practice_score)
        self.storage.schedule_review(
            chat_id, scenario.id, bottleneck_index, due_at, interval
        )
        self.storage.set_user_state(
            chat_id,
            stage="idle",
            current_scenario=None,
            current_step=0,
            current_session=None,
            current_review=None,
        )
        self._workspace(
            chat_id,
            card(
                "Маршрут пройден",
                f"Проверка назначена через {interval} дн.\n\n"
                f"Когда примените {TARGET_NOUNS_RU.get(str(self.storage.get_user(chat_id)['target_language']), 'язык')} в реальной ситуации, отметьте результат:",
                "ПОВТОР",
            ),
            [
                [
                    {
                        "text": "✅ Получилось",
                        "callback_data": f"outcome:{session_id}:success",
                    },
                    {
                        "text": "🟡 Частично",
                        "callback_data": f"outcome:{session_id}:partial",
                    },
                ],
                [
                    {
                        "text": "🔴 Не получилось",
                        "callback_data": f"outcome:{session_id}:failed",
                    }
                ],
                [{"text": "🧩 Закрепить эту фразу", "callback_data": "drill:start"}],
                [{"text": "Выбрать ещё ситуацию", "callback_data": "scenarios:list"}],
            ],
            surface="session_complete",
        )

    def show_reviews(self, chat_id: int, include_future: bool = False) -> None:
        reviews = self.storage.pending_reviews(chat_id, include_future=include_future)
        if not reviews:
            future = self.storage.pending_reviews(chat_id, include_future=True)
            text = (
                f"На сегодня повторений нет. Запланировано: {len(future)}."
                if future
                else "Повторений пока нет. Сначала пройдите один сценарий."
            )
            keyboard = (
                [[{"text": "🧪 Проверить сейчас", "callback_data": "reviews:all"}]]
                if future and not include_future
                else [[{"text": "Выбрать ситуацию", "callback_data": "scenarios:list"}]]
            )
            self._workspace(chat_id, text, keyboard, surface="reviews")
            return
        review = reviews[0]
        scenario = self._scenarios_for_chat(chat_id)[review["scenario_id"]]
        self._workspace(
            chat_id,
            f"Готова проверка: {scenario.title_ru}. В ней не будет исходной подсказки.",
            [
                [
                    {
                        "text": "Начать проверку",
                        "callback_data": f"review:{review['id']}",
                    }
                ],
                [{"text": "← Назад", "callback_data": "home"}],
            ],
            surface="reviews",
        )

    def begin_review(self, chat_id: int, review_id: int, scheduled: bool = False) -> None:
        review = self.storage.get_review(review_id, chat_id)
        if review["status"] != "pending":
            self.telegram.send_message(chat_id, "Эта проверка уже завершена.")
            return
        user = self.storage.get_user(chat_id)
        if str(review["target_language"]) != str(user["target_language"]):
            self.telegram.send_message(
                chat_id,
                "Эта проверка относится к другому изучаемому языку. Переключите язык в настройках.",
            )
            return
        scenario = self._scenarios_for_chat(chat_id)[review["scenario_id"]]
        step = scenario.steps[int(review["step_index"])]
        step_index = int(review["step_index"])
        self.storage.set_user_state(
            chat_id,
            stage="review",
            current_scenario=scenario.id,
            current_step=int(review["step_index"]),
            current_review=review_id,
            current_session=None,
        )
        language = self._language(chat_id)
        footnotes = self._glossary_footnotes(chat_id, step.interlocutor_pl)
        self._workspace(
            chat_id,
            f"Похожая ситуация, без образца.\n\n"
            f"{tr(language, 'task.speaker')}: {step.interlocutor_pl}\n\n"
            f"{tr(language, 'task.your_task')}: {self._instruction_text(chat_id, step.context_ru, 'review-step')}{footnotes}",
            [
                [
                    {
                        "text": tr(language, "action.translate_task"),
                        "callback_data": f"task:translate:{scenario.id}:{step_index}",
                    }
                ],
                [{"text": "Отменить", "callback_data": "cancel"}],
            ],
            force_new=scheduled,
            surface="review_task",
        )

    def handle_review_response(self, chat_id: int, text: str, user: Any) -> None:
        review_id = int(user["current_review"])
        review = self.storage.get_review(review_id, chat_id)
        scenario = self._scenarios_for_user(user)[review["scenario_id"]]
        step_index = int(review["step_index"])
        evaluation, analysis, analysis_id = self._evaluate_with_ai(
            chat_id, user, scenario, step_index, text
        )
        if analysis is not None and analysis_id is not None:
            self._send_ai_feedback(chat_id, analysis, analysis_id)
        self.storage.complete_review(review_id, evaluation.score)
        self.storage.event(
            chat_id,
            "review_completed",
            {"review_id": review_id, "score": evaluation.score},
        )
        next_interval = review_interval_days(
            evaluation.score, int(review["interval_days"])
        )
        self.storage.schedule_review(
            chat_id,
            scenario.id,
            step_index,
            review_due_at(evaluation.score, int(review["interval_days"])),
            next_interval,
        )
        self.storage.set_user_state(
            chat_id,
            stage="idle",
            current_scenario=None,
            current_step=0,
            current_review=None,
        )
        if evaluation.successful:
            message = (
                f"✅ Получилось без исходной подсказки. Следующая проверка через "
                f"{next_interval} дн."
            )
        else:
            message = (
                "Пока перенос не подтвердился. Нужный блок:\n"
                f"{scenario.steps[step_index].target_chunk}\n\n"
                f"Повторим через {next_interval} дн."
            )
        self._workspace(
            chat_id, message, [[{"text": "В меню", "callback_data": "home"}]]
            , surface="review_complete"
        )

    def _stored_analysis(self, chat_id: int, analysis_id: int) -> tuple[Any, ResponseAnalysis]:
        row = self.storage.get_ai_analysis(analysis_id, chat_id)
        return row, ResponseAnalysis.from_dict(json.loads(row["result_json"]))

    def _analysis_tabs(
        self, chat_id: int, analysis_id: int, active: str
    ) -> list[list[dict[str, str]]]:
        language = self._language(chat_id)
        tabs = [
            ("result", "tab.result"),
            ("variants", "tab.variants"),
            ("grammar", "tab.grammar"),
            ("translate", "tab.translation"),
        ]
        keyboard = [
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
        if self.storage.get_user(chat_id)["pending_assignment"]:
            keyboard.insert(
                0,
                [{"text": tr(language, "action.next"), "callback_data": "assignment:next"}],
            )
        return keyboard

    def show_variants(self, chat_id: int, analysis_id: int) -> None:
        _, analysis = self._stored_analysis(chat_id, analysis_id)
        if not analysis.alternatives:
            self.telegram.send_message(chat_id, "Для этого ответа дополнительных вариантов нет.")
            return
        labels = {
            key: self._t(chat_id, f"variant.{key}")
            for key in ("neutral", "formal", "informal")
        }
        blocks = []
        for item in analysis.alternatives:
            heading = labels.get(item.register, item.register)
            nuance = f"\n{item.nuance}" if item.nuance else ""
            blocks.append(f"{heading}:\n{item.text}{nuance}")
        self._workspace(
            chat_id,
            "\n\n".join(blocks),
            self._analysis_tabs(chat_id, analysis_id, "variants"),
            surface="feedback_variants",
        )
        self.storage.event(chat_id, "natural_variants_requested", {"analysis_id": analysis_id})

    def show_grammar_choices(self, chat_id: int, analysis_id: int) -> None:
        _, analysis = self._stored_analysis(chat_id, analysis_id)
        keyboard = [
            [{"text": self._t(chat_id, "grammar.whole"), "callback_data": f"ai:g:{analysis_id}:all"}]
        ]
        for index, chunk in enumerate(analysis.grammar_chunks):
            label = f"{chunk.text} — {chunk.label}"[:55]
            keyboard.append(
                [{"text": label, "callback_data": f"ai:g:{analysis_id}:{index}"}]
            )
        keyboard.append(
            [{"text": self._t(chat_id, "grammar.custom"), "callback_data": f"ai:g:{analysis_id}:custom"}]
        )
        keyboard.extend(self._analysis_tabs(chat_id, analysis_id, "grammar"))
        self._workspace(
            chat_id,
            self._t(chat_id, "grammar.choose"),
            keyboard,
            surface="feedback_grammar",
        )

    def explain_grammar(self, chat_id: int, analysis_id: int, selection: str) -> None:
        if self.ai is None:
            self.telegram.send_message(chat_id, "AI-разбор сейчас недоступен.")
            return
        row, analysis = self._stored_analysis(chat_id, analysis_id)
        sentence = analysis.natural_response or str(row["source_text"])
        if selection == "custom":
            self.telegram.send_message(
                chat_id,
                "Ответьте командой /grammar и укажите фрагмент, например:\n"
                "/grammar od dwóch dni",
            )
            return
        if selection == "all":
            fragment = sentence
        else:
            try:
                fragment = analysis.grammar_chunks[int(selection)].text
            except (ValueError, IndexError):
                self.telegram.send_message(chat_id, "Этот фрагмент больше недоступен.")
                return
        user = self.storage.get_user(chat_id)
        try:
            result = self.ai.explain_grammar(
                sentence,
                fragment,
                str(user["instruction_language"]),
                str(user["target_language"]),
            )
        except AIError:
            LOGGER.exception("AI grammar explanation failed")
            self.telegram.send_message(chat_id, "Не удалось получить разбор. Попробуйте позже.")
            self.storage.event(chat_id, "ai_analysis_failed", {"operation": "grammar"})
            return
        self.storage.add_ai_analysis(
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
        parts = [f"📚 {fragment}", f"\nЗначение: {result['meaning']}", f"\n{result['explanation']}"]
        if result["contrast_example"]:
            parts.append(f"\nСравните: {result['contrast_example']}")
        if result["common_error"]:
            parts.append(f"\nЧастая ошибка: {result['common_error']}")
        self._workspace(
            chat_id,
            "".join(parts),
            self._analysis_tabs(chat_id, analysis_id, "grammar"),
            surface="feedback_grammar",
        )
        self.storage.event(
            chat_id,
            "grammar_explanation_requested",
            {"analysis_id": analysis_id, "selection": selection},
        )

    def explain_custom_grammar(self, chat_id: int, fragment: str) -> None:
        user = self.storage.get_user(chat_id)
        row = self.storage.latest_ai_analysis(
            chat_id, str(user["target_language"])
        )
        if row is None:
            self.telegram.send_message(chat_id, "Сначала напишите ответ в учебном сценарии.")
            return
        analysis = ResponseAnalysis.from_dict(json.loads(row["result_json"]))
        sentence = analysis.natural_response or str(row["source_text"])
        if self.ai is None:
            self.telegram.send_message(chat_id, "AI-разбор сейчас недоступен.")
            return
        try:
            result = self.ai.explain_grammar(
                sentence,
                fragment,
                str(user["instruction_language"]),
                str(user["target_language"]),
            )
        except AIError:
            LOGGER.exception("Custom grammar explanation failed")
            self.telegram.send_message(chat_id, "Не удалось получить разбор. Попробуйте позже.")
            return
        self.storage.add_ai_analysis(
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
        self.storage.event(
            chat_id,
            "grammar_explanation_requested",
            {"analysis_id": int(row["id"]), "selection": "custom"},
        )
        self.telegram.send_message(
            chat_id,
            f"📚 {fragment}\n\nЗначение: {result['meaning']}\n\n"
            f"{result['explanation']}\n\nСравните: {result['contrast_example']}"
            + (f"\n\nЧастая ошибка: {result['common_error']}" if result["common_error"] else ""),
        )

    def translate_analysis(self, chat_id: int, analysis_id: int) -> None:
        _, analysis = self._stored_analysis(chat_id, analysis_id)
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
        user = self.storage.get_user(chat_id)
        translated = self._translate_text(
            chat_id, source, str(user["translation_language"]), "AI feedback"
        )
        self._workspace(
            chat_id,
            f"🌐 Перевод:\n{translated}" if translated else "Перевод сейчас недоступен.",
            self._analysis_tabs(chat_id, analysis_id, "translate"),
            surface="feedback_translation",
        )

    def translate_task(self, chat_id: int, scenario_id: str, step_index: int) -> None:
        scenario = self._scenarios_for_chat(chat_id).get(scenario_id)
        if scenario is None or not 0 <= step_index < len(scenario.steps):
            self.telegram.send_message(chat_id, "Задание не найдено.")
            return
        step = scenario.steps[step_index]
        source = f"{step.interlocutor_pl}\n{step.context_ru}"
        user = self.storage.get_user(chat_id)
        translated = self._translate_text(
            chat_id, source, str(user["translation_language"]), "scenario task"
        )
        self._workspace(
            chat_id,
            f"🌐 Перевод задания:\n{translated}" if translated else "Перевод сейчас недоступен.",
            [[{"text": self._t(chat_id, "action.resume_task"), "callback_data": "task:resume"}]],
            surface="task_translation",
        )

    @staticmethod
    def _drill_item_from_row(row: Any) -> DrillItem:
        return DrillItem.from_dict(
            {
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
        )

    def start_drill(
        self, chat_id: int, announce: bool = True, scheduled: bool = False
    ) -> None:
        active = self.storage.active_drill(chat_id)
        if active is not None:
            self.send_drill_item(chat_id, str(active["id"]), scheduled=scheduled)
            self._schedule_next_assignment(chat_id)
            return
        current = self.storage.get_user(chat_id)
        if current["stage"] in {"scenario", "practice", "review", "waiting"}:
            self.telegram.send_message(
                chat_id,
                card(
                    "Сначала закончите текущий шаг",
                    "Ответьте на последнее задание. Закрепление появится сразу после сценария.",
                ),
            )
            return
        source = self.storage.latest_ai_analysis(
            chat_id, str(current["target_language"])
        )
        if source is None:
            self.telegram.send_message(
                chat_id,
                card(
                    "Сначала нужна фраза",
                    "Пройдите одну ситуацию. После ответа я соберу закрепление именно по вашему материалу.",
                ),
                [[{"text": "▶ Выбрать ситуацию", "callback_data": "scenarios:list"}], [{"text": "← Назад", "callback_data": "home"}]],
            )
            return
        if self.ai is None:
            self.telegram.send_message(chat_id, "Закрепление временно недоступно: AI выключен.")
            return
        analysis = ResponseAnalysis.from_dict(json.loads(source["result_json"]))
        material = {
            "learner_response": str(source["source_text"]),
            "natural_response": analysis.natural_response,
            "meaning_gaps": analysis.meaning_gaps,
            "corrections": analysis.critical_corrections,
            "optional_improvements": analysis.optional_improvements,
            "grammar_chunks": [chunk.text for chunk in analysis.grammar_chunks],
            "scenario_id": source["scenario_id"],
        }
        user = current
        if announce:
            try:
                self.telegram.send_chat_action(chat_id, "typing")
            except TelegramError:
                LOGGER.debug("Could not send typing action", exc_info=True)
        try:
            pack = self.ai.generate_drill_pack(
                material,
                str(user["instruction_language"]),
                str(user["target_language"]),
            )
        except AIError:
            LOGGER.exception("AI drill generation failed")
            self.storage.event(chat_id, "ai_fallback_used", {"operation": "drill_generation"})
            self.telegram.send_message(
                chat_id,
                "Не удалось собрать задания. Основные сценарии и повторы продолжают работать.",
                [[{"text": "Попробовать ещё раз", "callback_data": "drill:start"}], [{"text": "← В меню", "callback_data": "home"}]],
            )
            return
        drill_id = self.storage.start_drill(
            chat_id,
            int(source["id"]),
            pack.title,
            pack.focus,
            [item.to_dict() for item in pack.items],
        )
        self.send_drill_item(chat_id, drill_id, scheduled=scheduled)
        self._schedule_next_assignment(chat_id)

    def send_drill_item(
        self, chat_id: int, drill_id: str, scheduled: bool = False
    ) -> None:
        session = self.storage.drill_session(drill_id, chat_id)
        index = int(session["current_index"])
        row = self.storage.drill_item(drill_id, index)
        item = self._drill_item_from_row(row)
        if row["status"] == "answered":
            self._workspace(
                chat_id,
                card(
                    "Ответ уже сохранён",
                    self._drill_continuation_text(chat_id),
                ),
                [[{"text": self._t(chat_id, "action.next"), "callback_data": f"drill:next:{drill_id}"}]],
            )
            return
        body = []
        if item.context:
            body.append(item.context)
        body.append(item.prompt)
        footnotes = self._glossary_footnotes(chat_id, item.context)
        keyboard: list[list[dict[str, str]]] = []
        if item.options:
            labels = ("A", "B", "C", "D")
            for option_index, option in enumerate(item.options):
                keyboard.append(
                    [
                        {
                            "text": f"{labels[option_index]} · {option}"[:60],
                            "callback_data": f"drill:answer:{row['id']}:{option_index}",
                        }
                    ]
                )
        else:
            body.append(self._t(chat_id, "drill.write"))
        keyboard.append(
            [
                {"text": self._t(chat_id, "action.hint"), "callback_data": f"drill:hint:{row['id']}"},
                {"text": self._t(chat_id, "action.skip"), "callback_data": f"drill:skip:{row['id']}"},
            ]
        )
        if scheduled:
            keyboard.append(
                [
                    {"text": self._t(chat_id, "action.pause"), "callback_data": "reminder:pause"},
                    {"text": self._t(chat_id, "action.reminders"), "callback_data": "reminders"},
                ]
            )
        keyboard.append([{"text": self._t(chat_id, "action.stop"), "callback_data": "drill:stop"}])
        self._workspace(
            chat_id,
            f"{progress(self._t(chat_id, 'drill.title'), index + 1, int(session['total_items']))}\n\n"
            + "\n\n".join(body)
            + footnotes,
            keyboard,
            force_new=scheduled,
            surface="drill_task",
        )

    def _evaluate_drill_response(
        self, chat_id: int, item: DrillItem, response: str
    ) -> DrillEvaluation:
        if item.options:
            correct = normalize(response) == normalize(item.correct_answer)
            return DrillEvaluation(
                correct=correct,
                score=1.0 if correct else 0.0,
                feedback="Форма выбрана верно." if correct else "Эта форма не подходит к контексту.",
                corrected_answer=item.correct_answer,
            )
        user = self.storage.get_user(chat_id)
        if self.ai is not None:
            try:
                return self.ai.evaluate_drill_answer(
                    item,
                    response,
                    str(user["instruction_language"]),
                    str(user["target_language"]),
                )
            except AIError:
                LOGGER.exception("AI drill answer evaluation failed")
                self.storage.event(chat_id, "ai_fallback_used", {"operation": "drill_evaluation"})
        accepted = {normalize(value) for value in item.accepted_answers}
        correct = normalize(response) in accepted
        return DrillEvaluation(
            correct=correct,
            score=1.0 if correct else 0.0,
            feedback="Ответ совпал с допустимым вариантом." if correct else "AI-проверка недоступна; показан образец.",
            corrected_answer=item.correct_answer,
        )

    def answer_drill(self, chat_id: int, item_id: int, response: str) -> None:
        user = self.storage.get_user(chat_id)
        drill_id = user["current_drill"]
        if user["stage"] != "drill" or not drill_id:
            self.telegram.send_message(chat_id, "Это задание уже закрыто.")
            return
        session = self.storage.drill_session(str(drill_id), chat_id)
        row = self.storage.drill_item(str(drill_id), int(session["current_index"]))
        if int(row["id"]) != item_id or row["status"] != "pending":
            self.telegram.send_message(chat_id, "Ответ уже учтён. Продолжите текущее задание.")
            return
        item = self._drill_item_from_row(row)
        evaluation = self._evaluate_drill_response(chat_id, item, response)
        self.storage.answer_drill_item(item_id, response, evaluation.score)
        self._schedule_next_assignment(chat_id)
        title = self._t(chat_id, "drill.success") if evaluation.correct else self._t(chat_id, "drill.fix")
        body = [evaluation.feedback]
        if not evaluation.correct and evaluation.corrected_answer:
            body.append(f"{self._t(chat_id, 'drill.correct')}: {evaluation.corrected_answer}")
        if item.explanation:
            body.append(f"{self._t(chat_id, 'drill.why')}: {item.explanation}")
        body.append(self._drill_continuation_text(chat_id))
        self._workspace(
            chat_id,
            card(title, "\n\n".join(body)),
            [[{"text": self._t(chat_id, "action.next"), "callback_data": f"drill:next:{drill_id}"}]],
            surface="drill_feedback",
        )
        self.storage.event(
            chat_id,
            "drill_item_answered",
            {"drill_id": drill_id, "item_type": item.type, "score": evaluation.score},
        )

    def advance_drill(
        self, chat_id: int, drill_id: str, scheduled: bool = False
    ) -> None:
        user = self.storage.get_user(chat_id)
        if user["stage"] != "drill" or str(user["current_drill"] or "") != drill_id:
            self.home(chat_id)
            return
        session = self.storage.drill_session(drill_id, chat_id)
        current_item = self.storage.drill_item(
            drill_id, int(session["current_index"])
        )
        if current_item["status"] != "answered":
            self.telegram.send_message(
                chat_id, "Сначала ответьте на текущее задание или пропустите его."
            )
            return
        if self.storage.advance_drill(drill_id, chat_id):
            self.send_drill_item(chat_id, drill_id, scheduled=scheduled)
            self._schedule_next_assignment(chat_id)
            return
        session = self.storage.drill_session(drill_id, chat_id)
        correct = int(session["correct_count"])
        total = int(session["total_items"])
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "drill.complete"),
                self._t(chat_id, "drill.score", correct=correct, total=total),
                "PRACTICE",
                self._language(chat_id),
            )
            + "\n\n"
            + route("REVIEW", self._language(chat_id)),
            [
                [{"text": "▶ Новая ситуация", "callback_data": "scenarios:list"}],
                [{"text": "↻ Повторы", "callback_data": "reviews:list"}],
                [{"text": "← В меню", "callback_data": "home"}],
            ],
            surface="drill_complete",
        )

    def handle_drill_text(self, chat_id: int, text: str, user: Any) -> None:
        drill_id = str(user["current_drill"])
        session = self.storage.drill_session(drill_id, chat_id)
        row = self.storage.drill_item(drill_id, int(session["current_index"]))
        item = self._drill_item_from_row(row)
        if item.options:
            self._workspace(chat_id, self._t(chat_id, "drill.choose"), surface="drill_task")
            return
        self.answer_drill(chat_id, int(row["id"]), text)

    def answer_drill_choice(self, chat_id: int, item_id: int, option_index: int) -> None:
        user = self.storage.get_user(chat_id)
        drill_id = user["current_drill"]
        if user["stage"] != "drill" or not drill_id:
            self.telegram.send_message(chat_id, "Это задание уже закрыто.")
            return
        session = self.storage.drill_session(str(drill_id), chat_id)
        row = self.storage.drill_item(str(drill_id), int(session["current_index"]))
        item = self._drill_item_from_row(row)
        if int(row["id"]) != item_id or not 0 <= option_index < len(item.options):
            self.telegram.send_message(chat_id, "Вариант больше недоступен.")
            return
        self.answer_drill(chat_id, item_id, item.options[option_index])

    def show_drill_hint(self, chat_id: int, item_id: int) -> None:
        user = self.storage.get_user(chat_id)
        drill_id = user["current_drill"]
        if user["stage"] != "drill" or not drill_id:
            return
        session = self.storage.drill_session(str(drill_id), chat_id)
        row = self.storage.drill_item(str(drill_id), int(session["current_index"]))
        if int(row["id"]) != item_id:
            return
        item = self._drill_item_from_row(row)
        self.storage.event(chat_id, "drill_hint_used", {"item_id": item_id})
        self._workspace(
            chat_id,
            card("Подсказка", item.hint or "Посмотрите на контекст и согласование слов."),
            [[{"text": self._t(chat_id, "action.resume_task"), "callback_data": "drill:resume"}]],
            surface="drill_hint",
        )

    def skip_drill_item(self, chat_id: int, item_id: int) -> None:
        user = self.storage.get_user(chat_id)
        drill_id = user["current_drill"]
        if user["stage"] != "drill" or not drill_id:
            return
        session = self.storage.drill_session(str(drill_id), chat_id)
        row = self.storage.drill_item(str(drill_id), int(session["current_index"]))
        if int(row["id"]) != item_id or row["status"] != "pending":
            return
        item = self._drill_item_from_row(row)
        self.storage.answer_drill_item(item_id, "[skipped]", 0.0)
        self._schedule_next_assignment(chat_id)
        self._workspace(
            chat_id,
            card(
                "Пропущено",
                f"Верный вариант: {item.correct_answer}\n\nПочему: {item.explanation}\n\n"
                f"{self._drill_continuation_text(chat_id)}",
            ),
            [[{"text": self._t(chat_id, "action.next"), "callback_data": f"drill:next:{drill_id}"}]],
            surface="drill_feedback",
        )

    def stop_drill(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        if user["current_drill"]:
            self.storage.abandon_drill(str(user["current_drill"]), chat_id)
        self.telegram.send_message(
            chat_id,
            card("Закрепление остановлено", "Прогресс этой короткой сессии закрыт."),
            [[{"text": "← В меню", "callback_data": "home"}]],
        )

    def send_scheduled_reminder(self, chat_id: int, mode: str) -> None:
        user = self.storage.get_user(chat_id)
        if user["pending_assignment"]:
            self.send_pending_assignment(chat_id, scheduled=True)
            return
        active = self.storage.active_drill(chat_id)
        if active is not None:
            drill_id = str(active["id"])
            current_item = self.storage.drill_item(
                drill_id, int(active["current_index"])
            )
            if current_item["status"] == "answered":
                self.advance_drill(chat_id, drill_id, scheduled=True)
            else:
                self.send_drill_item(chat_id, drill_id, scheduled=True)
            return
        due = self.storage.pending_reviews(chat_id)
        if due:
            self.begin_review(chat_id, int(due[0]["id"]), scheduled=True)
            return
        if self.storage.latest_ai_analysis(chat_id, str(user["target_language"])):
            self.start_drill(chat_id, announce=False, scheduled=True)
            return
        self.telegram.send_message(
            chat_id,
            card(
                "Нужна первая фраза",
                "Выберите короткую ситуацию. После неё бот сможет присылать конкретные задания автоматически.",
            ),
            [
                [{"text": "▶ Выбрать ситуацию", "callback_data": "scenarios:list"}],
                [
                    {"text": "⏸ Пауза до завтра", "callback_data": "reminder:pause"},
                    {"text": "⚙ Режим", "callback_data": "reminders"},
                ],
            ],
        )

    def show_privacy(self, chat_id: int) -> None:
        text = (
            "Alpha хранит Telegram ID, имя, текст ответов, оценки и расписание "
            "повторений. Для проверки фразы её текст, текущая реплика и учебная "
            "цель отправляются Google Gemini. Telegram ID, имя и история других "
            "сценариев в AI не передаются. Голос и контакты не собираются. Полные "
            "тексты не отправляются в продуктовую аналитику.\n\n"
            "Удалить все данные можно командой /delete_me."
        )
        localized = self._instruction_text(chat_id, text, "privacy")
        self._workspace(
            chat_id,
            localized,
            [[{"text": self._t(chat_id, "action.back"), "callback_data": "home"}]],
            surface="privacy",
        )

    def cancel_activity(self, chat_id: int, notify: bool = True) -> None:
        current = self.storage.get_user(chat_id)
        self.storage.abandon_session(current["current_session"])
        if current["current_drill"]:
            self.storage.abandon_drill(str(current["current_drill"]), chat_id)
        self.storage.set_user_state(
            chat_id,
            stage="idle",
            current_scenario=None,
            current_step=0,
            current_session=None,
            current_review=None,
            pending_assignment=None,
        )
        if notify:
            self.telegram.send_message(
                chat_id,
                "Текущая тренировка остановлена.",
                [[{"text": "В меню", "callback_data": "home"}]],
            )

    def handle_text(self, chat_id: int, first_name: str, text: str) -> None:
        if not self.is_allowed(chat_id):
            self.telegram.send_message(chat_id, "Сейчас доступна только закрытая alpha.")
            return
        user = self.storage.ensure_user(chat_id, first_name)
        command = text.strip().split(maxsplit=1)[0].lower()
        if command in {"/start", "/menu"}:
            self.start(chat_id, first_name)
            return
        if command == "/privacy":
            self.show_privacy(chat_id)
            return
        if command == "/delete_me":
            self.telegram.send_message(
                chat_id,
                "Удалить профиль, ответы, AI-разборы и расписание? Это необратимо.",
                [
                    [{"text": "Да, удалить", "callback_data": "delete:confirm"}],
                    [{"text": "Отмена", "callback_data": "home"}],
                ],
            )
            return
        if not self._has_current_consent(user):
            self.start(chat_id, first_name)
            return
        if command == "/settings":
            self.show_settings(chat_id)
            return
        if command == "/progress":
            self.show_progress(chat_id)
            return
        if command == "/reminders":
            self.show_reminders(chat_id)
            return
        if command == "/drill":
            self.start_drill(chat_id)
            return
        if command == "/grammar":
            fragment = text.strip()[len(command) :].strip()
            if fragment:
                self.explain_custom_grammar(chat_id, fragment)
            else:
                self.telegram.send_message(chat_id, "Использование: /grammar <фрагмент>")
            return
        if command == "/scenarios":
            self.show_scenarios(chat_id)
            return
        if command == "/review":
            self.show_reviews(chat_id)
            return
        if command == "/review_now":
            self.show_reviews(chat_id, include_future=True)
            return
        stage = user["stage"]
        if stage == "scenario":
            self.handle_scenario_response(chat_id, text, user)
        elif stage == "practice":
            self.handle_practice_response(chat_id, text, user)
        elif stage == "review":
            self.handle_review_response(chat_id, text, user)
        elif stage == "drill":
            self.handle_drill_text(chat_id, text, user)
        elif stage == "waiting":
            self.telegram.send_message(
                chat_id,
                self._drill_continuation_text(chat_id),
                [[{"text": "Следующее задание →", "callback_data": "assignment:next"}]],
            )
        else:
            self.home(chat_id)

    def handle_callback(
        self, chat_id: int, first_name: str, callback_id: str, data: str
    ) -> None:
        if not self.is_allowed(chat_id):
            self.telegram.answer_callback(callback_id, "Закрытая alpha")
            return
        user = self.storage.ensure_user(chat_id, first_name)
        try:
            self.telegram.answer_callback(callback_id)
        except TelegramError:
            LOGGER.warning("Could not acknowledge callback", exc_info=True)

        if data == "consent:accept":
            self.storage.accept_consent(chat_id, CONSENT_VERSION)
            self.home(chat_id)
        elif data == "privacy":
            self.show_privacy(chat_id)
        elif data == "delete:confirm":
            self.storage.delete_user(chat_id)
            self.telegram.send_message(
                chat_id, "Профиль, ответы и AI-разборы, связанные с Telegram ID, удалены."
            )
        elif not self._has_current_consent(user):
            self.start(chat_id, first_name)
        elif data == "home":
            self.home(chat_id)
        elif data == "settings":
            self.show_settings(chat_id)
        elif data == "progress":
            self.show_progress(chat_id)
        elif data == "reminders":
            self.show_reminders(chat_id)
        elif data.startswith("reminder:set:"):
            self.set_reminder_mode(chat_id, data.rsplit(":", 1)[1])
        elif data == "reminder:pause":
            current = self.storage.get_user(chat_id)
            until = pause_until_tomorrow(timezone_name=str(current["timezone"]))
            self.storage.pause_reminders(chat_id, until)
            self._workspace(
                chat_id,
                card("Пауза включена", "До завтра новых напоминаний не будет."),
                [[{"text": "← В меню", "callback_data": "home"}]],
                surface="reminder_pause",
            )
        elif data == "drill:start":
            self.start_drill(chat_id)
        elif data.startswith("drill:answer:"):
            _, _, item_id, option_index = data.split(":", 3)
            self.answer_drill_choice(chat_id, int(item_id), int(option_index))
        elif data.startswith("drill:hint:"):
            self.show_drill_hint(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("drill:skip:"):
            self.skip_drill_item(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("drill:next:"):
            self.advance_drill(chat_id, data.split(":", 2)[2])
        elif data == "drill:resume":
            current = self.storage.get_user(chat_id)
            if current["stage"] == "drill" and current["current_drill"]:
                self.send_drill_item(chat_id, str(current["current_drill"]))
            else:
                self.home(chat_id)
        elif data == "assignment:next":
            if not self.send_pending_assignment(chat_id):
                self.home(chat_id)
        elif data == "drill:stop":
            self.stop_drill(chat_id)
        elif data.startswith("settings:set:"):
            _, _, kind, language = data.split(":", 3)
            if kind == "level":
                self.storage.set_learner_level(chat_id, language)
                self.show_settings(chat_id)
                return
            field = {
                "instruction": "instruction_language",
                "translation": "translation_language",
                "target": "target_language",
            }.get(kind)
            allowed = {
                "target": {"pl", "en"},
                "instruction": {"ru", "uk", "en", "pl"},
                "translation": {"ru", "uk", "en", "pl"},
            }
            if field and language in allowed.get(kind, set()):
                current_language = str(self.storage.get_user(chat_id)[field])
                if field == "target_language" and language != current_language:
                    self.cancel_activity(chat_id, notify=False)
                self.storage.set_language(chat_id, field, language)
                self.show_settings(chat_id)
        elif data == "settings:level":
            self.show_level_choices(chat_id)
        elif data.startswith("settings:"):
            self.show_language_choices(chat_id, data.split(":", 1)[1])
        elif data == "scenarios:list":
            self.show_scenarios(chat_id)
        elif data.startswith("scenario:"):
            self.begin_scenario(chat_id, data.split(":", 1)[1])
        elif data.startswith("task:translate:"):
            _, _, scenario_id, step_index = data.split(":", 3)
            self.translate_task(chat_id, scenario_id, int(step_index))
        elif data == "task:resume":
            current = self.storage.get_user(chat_id)
            if current["stage"] == "scenario" and current["current_scenario"]:
                scenario = self._scenarios_for_user(current)[current["current_scenario"]]
                self.send_scenario_step(chat_id, scenario, int(current["current_step"]))
            elif current["stage"] == "review" and current["current_review"]:
                self.begin_review(chat_id, int(current["current_review"]))
            else:
                self.home(chat_id)
        elif data.startswith("ai:variants:"):
            self.show_variants(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("ai:result:"):
            analysis_id = int(data.rsplit(":", 1)[1])
            _, analysis = self._stored_analysis(chat_id, analysis_id)
            self._send_ai_feedback(
                chat_id,
                analysis,
                analysis_id,
                continuation=bool(
                    self.storage.get_user(chat_id)["pending_assignment"]
                ),
            )
        elif data.startswith("ai:grammar:"):
            self.show_grammar_choices(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("ai:g:"):
            _, _, analysis_id, selection = data.split(":", 3)
            self.explain_grammar(chat_id, int(analysis_id), selection)
        elif data.startswith("ai:translate:"):
            self.translate_analysis(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("ai:rate:"):
            _, _, analysis_id, rating = data.split(":", 3)
            if rating in {"up", "down"}:
                self.storage.event(
                    chat_id,
                    "ai_feedback_rated",
                    {"analysis_id": int(analysis_id), "rating": rating},
                )
        elif data == "hint":
            current = self.storage.get_user(chat_id)
            if current["stage"] == "scenario":
                scenario = self._scenarios_for_user(current)[current["current_scenario"]]
                step = scenario.steps[int(current["current_step"])]
                self.storage.event(
                    chat_id,
                    "hint_used",
                    {"scenario_id": scenario.id, "step_index": current["current_step"]},
                )
                hint = self._instruction_text(chat_id, step.hint_ru, "hint")
                self._workspace(
                    chat_id,
                    f"{self._t(chat_id, 'action.hint')}: {hint}",
                    [[{"text": self._t(chat_id, "action.resume_task"), "callback_data": "task:resume"}]],
                    surface="task_hint",
                )
        elif data == "practice:skip":
            current = self.storage.get_user(chat_id)
            if current["stage"] == "practice":
                scenario = self._scenarios_for_user(current)[current["current_scenario"]]
                self.finish_session(
                    chat_id,
                    scenario,
                    str(current["current_session"]),
                    int(current["current_step"]),
                    0.0,
                )
        elif data == "reviews:list":
            self.show_reviews(chat_id)
        elif data == "reviews:all":
            self.show_reviews(chat_id, include_future=True)
        elif data.startswith("review:"):
            self.begin_review(chat_id, int(data.split(":", 1)[1]))
        elif data.startswith("outcome:"):
            _, session_id, result = data.split(":", 2)
            session = self.storage.session(session_id)
            if int(session["chat_id"]) == chat_id and result in {
                "success",
                "partial",
                "failed",
            }:
                self.storage.add_outcome(chat_id, session_id, result)
                self.telegram.send_message(
                    chat_id,
                    "Спасибо. Реальный результат важнее количества пройденных уроков.",
                    [[{"text": "В меню", "callback_data": "home"}]],
                )
        elif data == "cancel":
            self.cancel_activity(chat_id)

    def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if update_id is not None and not self.storage.claim_update(int(update_id)):
            return
        try:
            if "message" in update:
                message = update["message"]
                text = message.get("text")
                if not text:
                    self.telegram.send_message(
                        int(message["chat"]["id"]),
                        "В этой версии используйте текстовые ответы. Голос появится после проверки качества.",
                    )
                else:
                    chat_id = int(message["chat"]["id"])
                    first_name = str(message.get("from", {}).get("first_name", ""))
                    self.handle_text(chat_id, first_name, text)
            elif "callback_query" in update:
                callback = update["callback_query"]
                message = callback.get("message")
                if message:
                    self.handle_callback(
                        int(message["chat"]["id"]),
                        str(callback.get("from", {}).get("first_name", "")),
                        str(callback["id"]),
                        str(callback.get("data", "")),
                    )
        except Exception:
            if update_id is not None:
                self.storage.release_update(int(update_id))
            raise
        else:
            if update_id is not None:
                self.storage.complete_update(int(update_id))

    def run_polling(self) -> None:
        LOGGER.info("Tutorlaing Telegram polling started")
        self.configure_commands()
        try:
            self.telegram.call(
                "deleteWebhook", {"drop_pending_updates": False}
            )
        except TelegramError:
            LOGGER.warning("Could not clear Telegram webhook", exc_info=True)
        backoff = 1
        while self.running:
            try:
                updates = self.telegram.get_updates(
                    self.offset, self.settings.poll_timeout
                )
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    try:
                        self.handle_update(update)
                    except Exception:
                        LOGGER.exception("Update handling failed")
                backoff = 1
            except TelegramError:
                LOGGER.exception("Telegram polling failed")
                time.sleep(backoff)
                backoff = min(30, backoff * 2)

    def configure_commands(self) -> None:
        try:
            self.telegram.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "start", "description": "Открыть главное меню"},
                        {"command": "scenarios", "description": "Выбрать ситуацию"},
                        {"command": "review", "description": "Повторения на сегодня"},
                        {"command": "review_now", "description": "Повторить сейчас"},
                        {"command": "settings", "description": "Языки и настройки"},
                        {"command": "progress", "description": "Прогресс и ближайший план"},
                        {"command": "drill", "description": "Закрепить материал"},
                        {"command": "reminders", "description": "Настроить напоминания"},
                        {"command": "grammar", "description": "Объяснить фрагмент"},
                        {"command": "privacy", "description": "Как хранятся данные"},
                        {"command": "delete_me", "description": "Удалить мои данные"},
                    ]
                },
            )
        except TelegramError:
            LOGGER.warning("Could not register bot commands", exc_info=True)

    def configure_webhook(self) -> None:
        self.configure_commands()
        self.telegram.call(
            "setWebhook",
            {
                "url": self.settings.telegram_webhook_url,
                "secret_token": self.settings.telegram_webhook_secret,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            },
        )
        LOGGER.info("Tutorlaing Telegram webhook configured")
