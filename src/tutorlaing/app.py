from __future__ import annotations

import json
import logging
import time
from typing import Any

from .ai import (
    AIClient,
    AIError,
    DrillEvaluation,
    DrillItem,
    FailoverAIClient,
    GeminiClient,
    OpenAIClient,
    PROMPT_VERSION,
    ResponseAnalysis,
)
from .config import Settings
from .catalog import ScenarioCatalog
from .content import Scenario
from .engine import (
    normalize,
    review_due_at,
    review_interval_days,
    select_bottleneck,
)
from .adaptive_difficulty import AdaptiveDifficultyService, DifficultyProposal
from .difficulty import level_policy, practice_level, shifted_level
from .drill_fallback import build_adaptive_fallback
from .exercise_bank import ExerciseBank, material_signature
from .i18n import tr
from .feedback import FeedbackPresenter
from .evaluation_service import ResponseEvaluator
from .language_support import LanguageSupport
from .menu import LearnerMenu
from .navigation import home_row, reply_action
from .privacy import CONSENT_VERSION
from .progress_service import ProgressService
from .reminders import next_reminder_at, pause_until_tomorrow
from .storage import Storage
from .telegram_api import TelegramAPI, TelegramError
from .toolkit import PracticeToolkit
from .ui import card, progress, route
from .workspace import TelegramWorkspace
from .update_dispatcher import TelegramUpdateDispatcher


LOGGER = logging.getLogger(__name__)
TARGET_COURSE_LABELS = {"pl": "Polski w praktyce", "en": "English in practice"}
TARGET_ADVERBS_RU = {"pl": "по-польски", "en": "по-английски"}
TARGET_NOUNS_RU = {"pl": "польский", "en": "английский"}


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
            primary = self._build_ai_provider(settings.ai_provider)
            if settings.ai_fallback_provider == "none":
                self.ai = primary
            else:
                self.ai = FailoverAIClient(
                    primary,
                    self._build_ai_provider(settings.ai_fallback_provider),
                    settings.ai_failover_cooldown,
                )
        self.catalog = ScenarioCatalog()
        self.exercise_bank = ExerciseBank(storage)
        self.workspace = TelegramWorkspace(storage, self.telegram)
        self.language_support = LanguageSupport(storage, self.ai)
        self.progress_service = ProgressService(storage)
        self.adaptive_difficulty = AdaptiveDifficultyService(storage)
        self.feedback = FeedbackPresenter(
            storage, self.workspace, self.language_support, self.ai
        )
        self.response_evaluator = ResponseEvaluator(storage, self.ai)
        self.menu = LearnerMenu(
            storage,
            self.workspace,
            self.catalog,
            self.language_support,
            self.progress_service,
        )
        self.update_dispatcher = TelegramUpdateDispatcher(
            storage, self.telegram, self
        )
        self.toolkit = PracticeToolkit(
            storage,
            self.workspace,
            self.catalog,
            self.ai,
            self,
            self.telegram,
            self.exercise_bank,
        )
        self.offset = 0
        self.running = True

    def _build_ai_provider(self, provider: str) -> AIClient:
        if provider == "gemini":
            return GeminiClient(
                self.settings.gemini_api_key,
                self.settings.gemini_model,
                self.settings.ai_timeout,
            )
        if provider == "openai":
            return OpenAIClient(
                self.settings.openai_api_key,
                self.settings.openai_model,
                self.settings.ai_timeout,
                self.settings.openai_reasoning_effort,
            )
        raise ValueError(f"Unsupported AI provider: {provider}")

    def is_allowed(self, chat_id: int) -> bool:
        allowed = self.settings.allowed_chat_ids
        return allowed is None or chat_id in allowed

    def _scenarios_for_user(self, user: Any) -> dict[str, Scenario]:
        return self.catalog.for_user(user)

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
        return self.workspace.show(
            chat_id, text, keyboard, force_new=force_new, surface=surface
        )

    def _notice(self, chat_id: int, text: str) -> None:
        self.telegram.send_temporary_message(chat_id, text)

    def home(self, chat_id: int) -> None:
        self.menu.home(chat_id)

    def start(self, chat_id: int, first_name: str = "") -> None:
        self.menu.start(chat_id, first_name)

    @staticmethod
    def _has_current_consent(user: Any) -> bool:
        return LearnerMenu.has_current_consent(user)

    def show_settings(self, chat_id: int) -> None:
        self.menu.show_settings(chat_id)

    def show_learning_settings(self, chat_id: int) -> None:
        self.menu.show_learning_settings(chat_id)

    def show_language_choices(self, chat_id: int, kind: str) -> None:
        self.menu.show_language_choices(chat_id, kind)

    def show_level_choices(self, chat_id: int) -> None:
        self.menu.show_level_choices(chat_id)

    def show_progress(self, chat_id: int) -> None:
        self.menu.show_progress(chat_id)

    def show_reminders(self, chat_id: int) -> None:
        self.menu.show_reminders(chat_id)

    def set_reminder_mode(self, chat_id: int, mode: str) -> None:
        self.menu.set_reminder_mode(chat_id, mode)

    def _schedule_next_assignment(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        mode = str(user["reminder_mode"])
        next_at = next_reminder_at(
            mode, timezone_name=str(user["timezone"])
        )
        self.storage.schedule_next_reminder(chat_id, next_at)

    def schedule_next_assignment(self, chat_id: int) -> None:
        """Public delivery port used by application services."""
        self._schedule_next_assignment(chat_id)

    def _drill_continuation_text(self, chat_id: int) -> str:
        return self.menu.continuation_text(chat_id)

    def show_scenarios(self, chat_id: int) -> None:
        self.menu.show_scenarios(chat_id)

    def resume_activity(self, chat_id: int) -> None:
        """Render whichever learning activity currently owns the profile state."""
        current = self.storage.get_user(chat_id)
        if current["stage"] == "drill" and current["current_drill"]:
            self.send_drill_item(chat_id, str(current["current_drill"]))
        elif current["stage"] == "scenario" and current["current_scenario"]:
            scenario = self._scenarios_for_user(current)[current["current_scenario"]]
            self.send_scenario_step(chat_id, scenario, int(current["current_step"]))
        elif (
            current["stage"] == "practice"
            and current["current_scenario"]
            and current["current_session"]
        ):
            scenario = self._scenarios_for_user(current)[current["current_scenario"]]
            self.begin_practice(chat_id, scenario, str(current["current_session"]))
        elif current["stage"] == "review" and current["current_review"]:
            self.begin_review(chat_id, int(current["current_review"]))
        elif current["stage"] == "waiting":
            if not self.send_pending_assignment(chat_id):
                self.home(chat_id)
        else:
            self.home(chat_id)

    def _translate_text(
        self, chat_id: int, text: str, language: str, context: str
    ) -> str | None:
        return self.language_support.translate_text(chat_id, text, language, context)

    def _instruction_text(self, chat_id: int, text: str, context: str) -> str:
        return self.language_support.instruction_text(chat_id, text, context)

    def _glossary_footnotes(self, chat_id: int, target_text: str) -> str:
        return self.language_support.glossary_footnotes(chat_id, target_text)

    def begin_scenario(self, chat_id: int, scenario_id: str) -> None:
        user = self.storage.get_user(chat_id)
        scenario = self._scenarios_for_user(user).get(scenario_id)
        if not scenario:
            self._notice(chat_id, "Этот сценарий не найден.")
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
        learner_level = practice_level(self.storage.get_user(chat_id))
        level_guidance = tr(
            language,
            f"task.level.{learner_level}",
            chunk=step.target_chunk,
        )
        opening = f"{intro}\n\n" if intro else ""
        footnotes = self._glossary_footnotes(chat_id, step.interlocutor_pl)
        self._workspace(
            chat_id,
            f"{progress(scenario.title_pl, step_index + 1, len(scenario.steps))}\n\n"
            f"{opening}{tr(language, 'task.speaker')}\n{step.interlocutor_pl}\n\n"
            f"{tr(language, 'task.your_task')}\n{instruction}\n\n"
            f"{tr(language, 'task.level_label', level=learner_level)}\n"
            f"{level_guidance}{footnotes}",
            [
                [{"text": f"💡 {tr(language, 'action.hint')}", "callback_data": "hint"}],
                [
                    {
                        "text": tr(language, "action.translate_task"),
                        "callback_data": f"task:translate:{scenario.id}:{step_index}",
                    }
                ],
                [{"text": tr(language, "action.stop"), "callback_data": "cancel:confirm"}],
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
        result = self.response_evaluator.evaluate(
            chat_id, user, scenario, step_index, response
        )
        return result.evaluation, result.analysis, result.analysis_id

    def _send_ai_feedback(
        self,
        chat_id: int,
        analysis: ResponseAnalysis,
        analysis_id: int,
        continuation: bool = False,
        force_new: bool = False,
    ) -> None:
        self.feedback.show_result(
            chat_id,
            analysis,
            analysis_id,
            continuation=continuation,
            force_new=force_new,
        )

    def handle_scenario_response(self, chat_id: int, text: str, user: Any) -> None:
        scenario = self._scenarios_for_user(user)[user["current_scenario"]]
        step_index = int(user["current_step"])
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
                chat_id,
                analysis,
                analysis_id,
                continuation=True,
                force_new=True,
            )
        elif evaluation.successful:
            self._workspace(
                chat_id,
                "✅ Коммуникативная задача выполнена.\n\n"
                + self._drill_continuation_text(chat_id),
                [[{"text": "Следующее задание →", "callback_data": "assignment:next"}]],
                force_new=True,
                surface="scenario_feedback",
            )
        else:
            self._workspace(
                chat_id,
                "Ответ принят. В конце выберу одно главное затруднение для короткой тренировки.\n\n"
                + self._drill_continuation_text(chat_id),
                [[{"text": self._t(chat_id, "action.next"), "callback_data": "assignment:next"}]],
                force_new=True,
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
            self._send_ai_feedback(chat_id, analysis, analysis_id, force_new=True)
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
            keyboard.append(home_row(self._language(chat_id)))
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
                home_row(self._language(chat_id)),
            ],
            surface="reviews",
        )

    def begin_review(self, chat_id: int, review_id: int, scheduled: bool = False) -> None:
        review = self.storage.get_review(review_id, chat_id)
        if review["status"] != "pending":
            self._notice(chat_id, "Эта проверка уже завершена.")
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
                [
                    {
                        "text": tr(language, "action.stop"),
                        "callback_data": "cancel:confirm",
                    }
                ],
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
            self._send_ai_feedback(chat_id, analysis, analysis_id, force_new=True)
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
            chat_id,
            message,
            [home_row(self._language(chat_id))],
            surface="review_complete",
        )

    def _stored_analysis(self, chat_id: int, analysis_id: int) -> tuple[Any, ResponseAnalysis]:
        return self.feedback.stored_analysis(chat_id, analysis_id)

    def _analysis_tabs(
        self, chat_id: int, analysis_id: int, active: str
    ) -> list[list[dict[str, str]]]:
        return self.feedback.tabs(chat_id, analysis_id, active)

    def show_variants(self, chat_id: int, analysis_id: int) -> None:
        self.feedback.show_variants(chat_id, analysis_id)

    def show_grammar_choices(self, chat_id: int, analysis_id: int) -> None:
        self.feedback.show_grammar_choices(chat_id, analysis_id)

    def explain_grammar(self, chat_id: int, analysis_id: int, selection: str) -> None:
        self.feedback.explain_grammar(chat_id, analysis_id, selection)

    def explain_custom_grammar(self, chat_id: int, fragment: str) -> None:
        self.feedback.explain_custom_grammar(chat_id, fragment)

    def translate_analysis(self, chat_id: int, analysis_id: int) -> None:
        self.feedback.translate_analysis(chat_id, analysis_id)

    def translate_task(self, chat_id: int, scenario_id: str, step_index: int) -> None:
        scenario = self._scenarios_for_chat(chat_id).get(scenario_id)
        if scenario is None or not 0 <= step_index < len(scenario.steps):
            self._notice(chat_id, "Задание не найдено.")
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
        target_language = str(current["target_language"])
        sources = self.storage.recent_ai_analyses(
            chat_id, target_language, limit=5
        )
        problem_history = self.storage.problem_history(
            chat_id, target_language, limit=16
        )
        if not sources and not any(problem_history.values()):
            self.telegram.send_message(
                chat_id,
                card(
                    "Сначала нужна фраза",
                    "Пройдите одну ситуацию. После ответа я соберу закрепление именно по вашему материалу.",
                ),
                [
                    [{"text": "▶ Выбрать ситуацию", "callback_data": "scenarios:list"}],
                    home_row(self._language(chat_id)),
                ],
            )
            return
        recent_material = []
        for source in sources:
            analysis = ResponseAnalysis.from_dict(json.loads(source["result_json"]))
            recent_material.append(
                {
                    "learner_response": str(source["source_text"]),
                    "natural_response": analysis.natural_response,
                    "meaning_gaps": analysis.meaning_gaps,
                    "corrections": analysis.critical_corrections,
                    "optional_improvements": analysis.optional_improvements,
                    "grammar_chunks": [chunk.text for chunk in analysis.grammar_chunks],
                    "scenario_id": source["scenario_id"],
                    "step_index": source["step_index"],
                }
            )

        scenarios = self._scenarios_for_user(current)
        recurring_problem_material = []
        for evidence in problem_history["scenario_steps"]:
            scenario = scenarios.get(str(evidence["scenario_id"]))
            step_index = int(evidence["step_index"])
            if scenario is None or not 0 <= step_index < len(scenario.steps):
                continue
            step = scenario.steps[step_index]
            recurring_problem_material.append(
                {
                    "kind": "scenario_step",
                    "context": step.context_ru,
                    "target_chunk": step.target_chunk,
                    "failures": int(evidence["failures"]),
                    "worst_score": float(evidence["worst_score"]),
                }
            )
        recurring_problem_material.extend(problem_history["drill_items"])
        working_level = practice_level(current)
        material = {
            "recent_learner_material": recent_material,
            "recurring_problem_material": recurring_problem_material,
            "learner_level": working_level,
            "profile_level": str(current["learner_level"]),
            "level_policy": level_policy(working_level).ai_instruction,
        }
        user = current
        material_revision = f"history:{material_signature(material)}"
        bank_pack = self.exercise_bank.find_pack(
            chat_id,
            target_language=target_language,
            instruction_language=str(user["instruction_language"]),
            translation_language=str(user["translation_language"]),
            learner_level=working_level,
            mode="adaptive",
            scenario_id=material_revision,
        )
        self.storage.event(
            chat_id,
            "exercise_bank_lookup",
            {"mode": "adaptive", "hit": bank_pack is not None},
        )
        if announce:
            try:
                self.telegram.send_chat_action(chat_id, "typing")
            except TelegramError:
                LOGGER.debug("Could not send typing action", exc_info=True)
        if bank_pack is None:
            pack = None
            source = "ai"
            provider = ""
            model = ""
            prompt_version = PROMPT_VERSION
            if self.ai is not None:
                try:
                    pack = self.ai.generate_drill_pack(
                        material,
                        str(user["instruction_language"]),
                        str(user["target_language"]),
                    )
                    provider = str(self.ai.provider)
                    model = str(self.ai.model)
                except AIError:
                    LOGGER.exception("AI drill generation failed; using history fallback")
                    self.storage.event(
                        chat_id,
                        "ai_fallback_used",
                        {"operation": "drill_generation", "fallback": "history"},
                    )
            if pack is None:
                source = "recovery"
                provider = "local"
                model = "history-fallback"
                prompt_version = "history-fallback-v1"
                try:
                    pack = build_adaptive_fallback(
                        material, str(user["instruction_language"])
                    )
                except AIError:
                    LOGGER.exception("Adaptive history fallback generation failed")
            if pack is None:
                self.telegram.send_message(
                    chat_id,
                    "Не удалось собрать задания. Основные сценарии и повторы продолжают работать.",
                    [
                        [{"text": "Попробовать ещё раз", "callback_data": "drill:start"}],
                        home_row(self._language(chat_id)),
                    ],
                )
                return
            bank_pack = self.exercise_bank.add_pack(
                chat_id,
                pack,
                target_language=target_language,
                instruction_language=str(user["instruction_language"]),
                translation_language=str(user["translation_language"]),
                learner_level=working_level,
                mode="adaptive",
                scenario_id=material_revision,
                source=source,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                private=True,
                tags=["personal:errors"],
            )
        self.storage.suspend_activity(chat_id)
        drill_id = self.storage.start_drill(
            chat_id,
            int(sources[0]["id"]) if sources else None,
            bank_pack.pack.title,
            bank_pack.pack.focus,
            bank_pack.drill_items(),
            replace_active=False,
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
        footnotes = (
            ""
            if item.type == "flashcard"
            else self._glossary_footnotes(chat_id, item.context)
        )
        keyboard: list[list[dict[str, str]]] = []
        if item.options:
            labels = ("A", "B", "C", "D")
            body.append(
                f"{self._t(chat_id, 'drill.options')}:\n"
                + "\n".join(
                    f"{labels[option_index]}. {option}"
                    for option_index, option in enumerate(item.options)
                )
            )
            answer_row: list[dict[str, str]] = []
            for option_index, option in enumerate(item.options):
                answer_row.append(
                    {
                        "text": labels[option_index],
                        "callback_data": f"drill:answer:{row['id']}:{option_index}",
                    }
                )
            keyboard.append(answer_row)
        else:
            body.append(self._t(chat_id, "drill.write"))
        if item.type == "flashcard":
            keyboard.append(
                [
                    {
                        "text": self._t(chat_id, "toolkit.forgot"),
                        "callback_data": f"drill:skip:{row['id']}",
                    }
                ]
            )
        else:
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
        keyboard.append(
            [
                {
                    "text": self._t(chat_id, "action.stop"),
                    "callback_data": "drill:stop:confirm",
                }
            ]
        )
        self._workspace(
            chat_id,
            f"{progress(self._drill_title(chat_id, str(session['mode'])), index + 1, int(session['total_items']))}\n\n"
            + "\n\n".join(body)
            + footnotes,
            keyboard,
            force_new=scheduled,
            surface="drill_task",
        )

    def _drill_title(self, chat_id: int, mode: str) -> str:
        key = {
            "toolkit_cards": "toolkit.cards_title",
            "toolkit_topic": "toolkit.topic_drill_title",
        }.get(mode, "drill.title")
        return self._t(chat_id, key)

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
        accepted = {normalize(value) for value in item.accepted_answers}
        if normalize(response) in accepted:
            return DrillEvaluation(
                correct=True,
                score=1.0,
                feedback="Ответ совпал с допустимым вариантом.",
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
        correct = normalize(response) in accepted
        return DrillEvaluation(
            correct=correct,
            score=1.0 if correct else 0.0,
            feedback="Ответ совпал с допустимым вариантом." if correct else "AI-проверка недоступна; показан образец.",
            corrected_answer=item.correct_answer,
        )

    def answer_drill(
        self, chat_id: int, item_id: int, response: str, *, force_new: bool = False
    ) -> None:
        user = self.storage.get_user(chat_id)
        drill_id = user["current_drill"]
        if user["stage"] != "drill" or not drill_id:
            self._notice(chat_id, "Это задание уже закрыто.")
            return
        session = self.storage.drill_session(str(drill_id), chat_id)
        row = self.storage.drill_item(str(drill_id), int(session["current_index"]))
        if int(row["id"]) != item_id or row["status"] != "pending":
            self._notice(chat_id, "Ответ уже учтён. Продолжите текущее задание.")
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
            force_new=force_new,
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
            self._notice(
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
        proposal = self.adaptive_difficulty.assess(chat_id)
        keyboard = [
            [
                {
                    "text": self._t(chat_id, "action.toolkit"),
                    "callback_data": "toolkit",
                }
            ],
            [{"text": self._t(chat_id, "action.reviews"), "callback_data": "reviews:list"}],
            home_row(self._language(chat_id)),
        ]
        completion_body = self._t(
            chat_id, "drill.score", correct=correct, total=total
        )
        if proposal is not None:
            offer_text, offer_keyboard = self._difficulty_offer(chat_id, proposal)
            completion_body += f"\n\n{offer_text}"
            keyboard[0:0] = offer_keyboard
        if str(session["mode"]) == "toolkit_cards":
            keyboard.insert(
                0,
                [
                    {
                        "text": self._t(chat_id, "toolkit.more_cards"),
                        "callback_data": "toolkit:start:cards",
                    }
                ],
            )
        resume = self.menu.resume_action(self.storage.get_user(chat_id))
        if resume:
            keyboard.insert(
                0,
                [
                    {
                        "text": self._t(chat_id, "action.return_to_activity"),
                        "callback_data": resume,
                    }
                ],
            )
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "drill.complete"),
                completion_body,
                "PRACTICE",
                self._language(chat_id),
            )
            + "\n\n"
            + route("REVIEW", self._language(chat_id)),
            keyboard,
            surface="drill_complete",
        )

    def _difficulty_offer(
        self, chat_id: int, proposal: DifficultyProposal
    ) -> tuple[str, list[list[dict[str, str]]]]:
        user = self.storage.get_user(chat_id)
        profile = str(user["learner_level"])
        target = shifted_level(profile, proposal.direction)
        direction = "up" if proposal.direction > 0 else "down"
        text = self._t(
            chat_id,
            f"difficulty.offer_{direction}",
            level=target,
            score=round(proposal.average_score * 100),
        )
        return text, [
            [
                {
                    "text": self._t(
                        chat_id, f"difficulty.apply_{direction}", level=target
                    ),
                    "callback_data": f"difficulty:accept:{proposal.id}",
                }
            ],
            [
                {
                    "text": self._t(chat_id, "difficulty.keep"),
                    "callback_data": f"difficulty:dismiss:{proposal.id}",
                }
            ],
        ]

    def resolve_difficulty(
        self, chat_id: int, proposal_id: int, accepted: bool
    ) -> None:
        try:
            self.adaptive_difficulty.resolve(chat_id, proposal_id, accepted)
        except KeyError:
            self._notice(chat_id, self._t(chat_id, "difficulty.stale"))
            return
        user = self.storage.get_user(chat_id)
        if accepted:
            summary = self._t(
                chat_id,
                "difficulty.changed",
                level=practice_level(user),
                profile=str(user["learner_level"]),
            )
        else:
            summary = self._t(chat_id, "difficulty.kept")
        self._workspace(
            chat_id,
            card(self._t(chat_id, "progress.title"), summary),
            [
                [{"text": self._t(chat_id, "action.progress"), "callback_data": "progress"}],
                home_row(self._language(chat_id)),
            ],
            surface="difficulty_result",
        )

    def handle_drill_text(self, chat_id: int, text: str, user: Any) -> None:
        drill_id = str(user["current_drill"])
        session = self.storage.drill_session(drill_id, chat_id)
        row = self.storage.drill_item(drill_id, int(session["current_index"]))
        item = self._drill_item_from_row(row)
        if item.options:
            self._workspace(chat_id, self._t(chat_id, "drill.choose"), surface="drill_task")
            return
        self.answer_drill(chat_id, int(row["id"]), text, force_new=True)

    def answer_drill_choice(self, chat_id: int, item_id: int, option_index: int) -> None:
        user = self.storage.get_user(chat_id)
        drill_id = user["current_drill"]
        if user["stage"] != "drill" or not drill_id:
            self._notice(chat_id, "Это задание уже закрыто.")
            return
        session = self.storage.drill_session(str(drill_id), chat_id)
        row = self.storage.drill_item(str(drill_id), int(session["current_index"]))
        item = self._drill_item_from_row(row)
        if int(row["id"]) != item_id or not 0 <= option_index < len(item.options):
            self._notice(chat_id, "Вариант больше недоступен.")
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
                self._t(chat_id, "toolkit.revealed_title"),
                self._t(
                    chat_id,
                    "toolkit.revealed_summary",
                    answer=item.correct_answer,
                    explanation=item.explanation,
                )
                + "\n\n"
                + self._drill_continuation_text(chat_id),
            ),
            [[{"text": self._t(chat_id, "action.next"), "callback_data": f"drill:next:{drill_id}"}]],
            surface="drill_feedback",
        )

    def stop_drill(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        if user["current_drill"]:
            self.storage.abandon_drill(str(user["current_drill"]), chat_id)
        keyboard = [home_row(self._language(chat_id))]
        resume = self.menu.resume_action(self.storage.get_user(chat_id))
        if resume:
            keyboard.insert(
                0,
                [
                    {
                        "text": self._t(chat_id, "action.return_to_activity"),
                        "callback_data": resume,
                    }
                ],
            )
        self.telegram.send_message(
            chat_id,
            card("Закрепление остановлено", "Прогресс этой короткой сессии закрыт."),
            keyboard,
        )

    def confirm_finish(self, chat_id: int, *, drill: bool = False) -> None:
        """Ask before discarding the current learner flow."""
        language = self._language(chat_id)
        self._workspace(
            chat_id,
            card(
                tr(language, "navigation.finish_title"),
                tr(language, "navigation.finish_summary"),
            ),
            [
                [
                    {
                        "text": tr(language, "action.keep_learning"),
                        "callback_data": "drill:resume" if drill else "task:resume",
                    }
                ],
                [
                    {
                        "text": tr(language, "action.confirm_finish"),
                        "callback_data": "drill:stop" if drill else "cancel",
                    }
                ],
            ],
            surface="finish_confirmation",
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
        stage = str(user["stage"])
        if stage == "scenario" and user["current_scenario"]:
            scenario = self._scenarios_for_user(user).get(
                str(user["current_scenario"])
            )
            step_index = int(user["current_step"])
            if scenario is not None and 0 <= step_index < len(scenario.steps):
                self.send_scenario_step(
                    chat_id, scenario, step_index, scheduled=True
                )
                return
        if (
            stage == "practice"
            and user["current_scenario"]
            and user["current_session"]
        ):
            scenario = self._scenarios_for_user(user).get(
                str(user["current_scenario"])
            )
            if scenario is not None:
                self.begin_practice(
                    chat_id,
                    scenario,
                    str(user["current_session"]),
                    scheduled=True,
                )
                return
        if stage == "review" and user["current_review"]:
            self.begin_review(
                chat_id, int(user["current_review"]), scheduled=True
            )
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

    def show_privacy(self, chat_id: int, *, back_to_settings: bool = False) -> None:
        self.menu.show_privacy(chat_id, back_to_settings=back_to_settings)

    def cancel_activity(self, chat_id: int, notify: bool = True) -> None:
        current = self.storage.get_user(chat_id)
        if current["current_drill"]:
            self.storage.abandon_drill(str(current["current_drill"]), chat_id)
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
            toolkit_input_mode=None,
            suspended_activity_json=None,
        )
        if notify:
            self.telegram.send_message(
                chat_id,
                "Текущая тренировка остановлена.",
                [home_row(self._language(chat_id))],
            )

    def handle_text(self, chat_id: int, first_name: str, text: str) -> None:
        if not self.is_allowed(chat_id):
            self.telegram.send_message(chat_id, "Сейчас доступна только закрытая alpha.")
            return
        user = self.storage.ensure_user(chat_id, first_name)
        command = text.strip().split(maxsplit=1)[0].lower()
        if command.startswith("/") and user["toolkit_input_mode"]:
            self.storage.set_user_state(chat_id, toolkit_input_mode=None)
            user = self.storage.get_user(chat_id)
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
        navigation_action = reply_action(text)
        if navigation_action:
            if user["toolkit_input_mode"]:
                self.storage.set_user_state(chat_id, toolkit_input_mode=None)
                user = self.storage.get_user(chat_id)
            if navigation_action == "learn":
                if self.menu.resume_action(user):
                    self.resume_activity(chat_id)
                elif self.storage.pending_reviews(chat_id):
                    self.show_reviews(chat_id)
                else:
                    self.show_scenarios(chat_id)
            elif navigation_action == "tools":
                self.toolkit.show_menu(chat_id)
            elif navigation_action == "progress":
                self.show_progress(chat_id)
            else:
                self.show_settings(chat_id)
            return
        if command == "/settings":
            self.show_settings(chat_id)
            return
        if command == "/progress":
            self.show_progress(chat_id)
            return
        if command in {"/tools", "/toolkit"}:
            self.toolkit.show_menu(chat_id)
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
        if user["toolkit_input_mode"]:
            self.toolkit.handle_phrase(chat_id, text)
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

        if (
            user["toolkit_input_mode"]
            and data not in {"toolkit", "toolkit:resume"}
            and not data.startswith("toolkit:translate:")
        ):
            self.storage.set_user_state(chat_id, toolkit_input_mode=None)
            user = self.storage.get_user(chat_id)

        if data == "consent:accept":
            self.storage.accept_consent(chat_id, CONSENT_VERSION)
            self.home(chat_id)
        elif data == "privacy":
            self.show_privacy(chat_id)
        elif data == "privacy:settings":
            self.show_privacy(chat_id, back_to_settings=True)
        elif data == "delete:confirm":
            self.storage.delete_user(chat_id)
            self.telegram.send_message(
                chat_id, "Профиль, ответы и AI-разборы, связанные с Telegram ID, удалены."
            )
        elif not self._has_current_consent(user):
            self.start(chat_id, first_name)
        elif data == "home":
            self.home(chat_id)
        elif data == "toolkit":
            self.toolkit.show_menu(chat_id)
        elif data == "toolkit:topics":
            self.toolkit.show_topics(chat_id)
        elif data.startswith("toolkit:topic:"):
            self.toolkit.start_pack(
                chat_id, "topic", data.split(":", 2)[2]
            )
        elif data.startswith("toolkit:start:"):
            self.toolkit.start_pack(chat_id, data.split(":", 2)[2])
        elif data.startswith("toolkit:translate:"):
            self.toolkit.ask_for_phrase(chat_id, data.split(":", 2)[2])
        elif data == "toolkit:resume":
            self.storage.set_user_state(chat_id, toolkit_input_mode=None)
            self.resume_activity(chat_id)
        elif data == "settings":
            self.show_settings(chat_id)
        elif data == "progress":
            self.show_progress(chat_id)
        elif data == "reminders":
            self.show_reminders(chat_id)
        elif data.startswith("reminder:set:"):
            self.set_reminder_mode(chat_id, data.rsplit(":", 1)[1])
        elif data == "reminder:test":
            user = self.storage.get_user(chat_id)
            mode = str(user["reminder_mode"])
            self.send_scheduled_reminder(
                chat_id, mode if mode != "off" else "gentle"
            )
            self.storage.record_reminder_delivery(
                chat_id, "manual_test", mode
            )
        elif data == "reminder:pause":
            current = self.storage.get_user(chat_id)
            until = pause_until_tomorrow(timezone_name=str(current["timezone"]))
            self.storage.pause_reminders(chat_id, until)
            self._workspace(
                chat_id,
                card("Пауза включена", "До завтра новых напоминаний не будет."),
                [home_row(self._language(chat_id))],
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
        elif data.startswith("difficulty:accept:"):
            self.resolve_difficulty(chat_id, int(data.rsplit(":", 1)[1]), True)
        elif data.startswith("difficulty:dismiss:"):
            self.resolve_difficulty(chat_id, int(data.rsplit(":", 1)[1]), False)
        elif data == "drill:resume":
            self.resume_activity(chat_id)
        elif data == "drill:stop:confirm":
            self.confirm_finish(chat_id, drill=True)
        elif data == "assignment:next":
            if not self.send_pending_assignment(chat_id):
                self.home(chat_id)
        elif data == "drill:stop":
            self.stop_drill(chat_id)
        elif data == "settings:languages":
            self.show_learning_settings(chat_id)
        elif data.startswith("settings:set:"):
            _, _, kind, language = data.split(":", 3)
            if kind == "level":
                self.storage.set_learner_level(chat_id, language)
                self.show_learning_settings(chat_id)
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
                if field == "instruction_language":
                    self.menu.refresh_navigation(chat_id)
                self.show_learning_settings(chat_id)
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
            self.resume_activity(chat_id)
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
                    [home_row(self._language(chat_id))],
                )
        elif data == "cancel":
            self.cancel_activity(chat_id)
        elif data == "cancel:confirm":
            self.confirm_finish(chat_id)

    def handle_update(self, update: dict[str, Any]) -> None:
        self.update_dispatcher.dispatch(update)

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
                        {"command": "tools", "description": "Карточки, перевод и темы"},
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
