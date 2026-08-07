from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
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
from .coach import CoachService
from .commands import PUBLIC_COMMANDS, command_payload, parse_command
from .content import Scenario, ScenarioStep
from .engine import (
    normalize,
    review_due_at,
    review_interval_days,
    select_bottleneck,
)
from .adaptive_difficulty import AdaptiveDifficultyService, DifficultyProposal
from .activities import ActivityService
from .background_learning import BackgroundLearningService
from .difficulty import level_policy, practice_level, shifted_level
from .drill_fallback import build_adaptive_fallback
from .exercise_bank import ExerciseBank, material_signature
from .i18n import tr, ui_copy
from .feedback import FeedbackPresenter
from .evaluation_service import ResponseEvaluator
from .language_support import LanguageSupport
from .learner_profile import LearnerProfileService
from .learning_cards import LEARNING_CARD_KINDS
from .menu import LearnerMenu
from .navigation import back_row, home_row, reply_action
from .privacy import CONSENT_VERSION
from .progress_service import ProgressService
from .quest_content import Quest, QuestCatalog, QuestNode
from .quest_engine import QuestTransition, answer_free
from .reminders import next_reminder_at, pause_until_tomorrow
from .storage import Storage
from .telegram_api import TelegramAPI, TelegramError
from .toolkit import LANGUAGE_LABELS, PracticeToolkit
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
        self.quest_catalog = QuestCatalog()
        self.exercise_bank = ExerciseBank(storage)
        self.workspace = TelegramWorkspace(storage, self.telegram)
        self.language_support = LanguageSupport(storage, self.ai)
        self.progress_service = ProgressService(storage)
        self.learner_profiles = LearnerProfileService(storage)
        self.activities = ActivityService(storage)
        self.coach = CoachService(storage, self.ai)
        self.background_learning = BackgroundLearningService(storage, self.ai)
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
            self.learner_profiles,
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

    def _copy(self, chat_id: int, key: str, **values: Any) -> str:
        return ui_copy(self._language(chat_id), key, **values)

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

    def _focus_new_surface(
        self, chat_id: int, incoming_message_id: int | None = None
    ) -> None:
        """Put a navigation result below the learner's latest message."""

        if incoming_message_id is not None:
            try:
                self.telegram.delete_message(chat_id, incoming_message_id)
            except TelegramError:
                LOGGER.debug("Could not delete reply-navigation message", exc_info=True)
        self.workspace.start_new_surface(chat_id)

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

    def show_learner_profile(self, chat_id: int) -> None:
        self.menu.show_learner_profile(chat_id)

    def show_practice_hub(self, chat_id: int) -> None:
        self.menu.show_practice_hub(chat_id)

    def show_help(self, chat_id: int) -> None:
        self.menu.show_help(chat_id)

    def ask_profile_text(self, chat_id: int, field: str) -> None:
        if field not in {"weekly", "goal"}:
            self.show_learner_profile(chat_id)
            return
        self.storage.set_user_state(chat_id, profile_input_mode=field)
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, f"profile.input.{field}.title"),
                self._t(chat_id, f"profile.input.{field}.prompt"),
            ),
            [
                [
                    {
                        "text": self._t(chat_id, "action.cancel"),
                        "callback_data": "settings:profile",
                    }
                ]
            ],
            surface="profile_input",
        )

    def save_profile_text(self, chat_id: int, text: str, field: str) -> None:
        try:
            if field == "weekly":
                self.learner_profiles.set_weekly_context(chat_id, text)
            elif field == "goal":
                self.learner_profiles.set_current_goal(chat_id, text)
            else:
                raise ValueError("Unknown profile field")
        except ValueError:
            self._notice(chat_id, self._t(chat_id, "profile.input.invalid"))
            return
        self.storage.set_user_state(chat_id, profile_input_mode=None)
        self.show_learner_profile(chat_id)

    def open_coach(self, chat_id: int) -> None:
        try:
            activity_kind, activity_id, context = self._coach_context(chat_id)
        except KeyError:
            self._notice(chat_id, self._t(chat_id, "coach.no_activity"))
            return
        session = self.coach.open(chat_id, activity_kind, activity_id, context)
        self._show_coach_menu(chat_id, session.id)

    def _show_coach_menu(self, chat_id: int, session_id: str) -> None:
        session = self.coach.get(chat_id, session_id)
        title = str(session.context.get("title") or session.activity_kind)
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "coach.title", activity=title),
                self._t(chat_id, "coach.summary"),
            ),
            [
                [{"text": self._t(chat_id, "coach.hint"), "callback_data": "coach:ask:hint"}],
                [{"text": self._t(chat_id, "coach.say"), "callback_data": "coach:input:say"}],
                [{"text": self._t(chat_id, "coach.translate"), "callback_data": "coach:input:translate"}],
                [{"text": self._t(chat_id, "coach.explain"), "callback_data": "coach:ask:explain"}],
                [{"text": self._t(chat_id, "coach.question"), "callback_data": "coach:input:question"}],
                [{"text": self._t(chat_id, "coach.return"), "callback_data": "coach:return"}],
            ],
            surface="coach_menu",
        )

    def ask_coach_input(self, chat_id: int, operation: str) -> None:
        user = self.storage.get_user(chat_id)
        if not user["coach_session_id"]:
            self.open_coach(chat_id)
            return
        self.storage.set_user_state(chat_id, coach_input_mode=operation)
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, f"coach.input.{operation}.title"),
                self._t(chat_id, f"coach.input.{operation}.prompt"),
            ),
            [[{"text": self._t(chat_id, "coach.back"), "callback_data": "coach:menu"}]],
            surface="coach_input",
        )

    def answer_coach(
        self, chat_id: int, operation: str, question: str = ""
    ) -> None:
        user = self.storage.get_user(chat_id)
        session_id = str(user["coach_session_id"] or "")
        if not session_id:
            self.open_coach(chat_id)
            return
        self.telegram.send_chat_action(chat_id)
        response = self.coach.answer(
            chat_id, session_id, operation, question or operation
        )
        self.storage.set_user_state(chat_id, coach_input_mode=None)
        body = [response.answer]
        if response.translation:
            body.append(self._t(chat_id, "coach.translation_result", text=response.translation))
        if response.suggested_phrases:
            body.append(
                self._t(chat_id, "coach.variants")
                + "\n"
                + "\n".join(
                    f"• {item.text} — {item.nuance}" if item.nuance else f"• {item.text}"
                    for item in response.suggested_phrases
                )
            )
        if response.disclosed_help_level >= 4:
            body.append(self._t(chat_id, "coach.full_example_note"))
        self._workspace(
            chat_id,
            card(self._t(chat_id, "coach.answer_title"), "\n\n".join(body)),
            [
                [{"text": self._t(chat_id, "coach.ask_more"), "callback_data": "coach:menu"}],
                [{"text": self._t(chat_id, "coach.return"), "callback_data": "coach:return"}],
            ],
            surface="coach_answer",
        )

    def close_coach(self, chat_id: int) -> None:
        self.storage.close_coach_session(chat_id)
        self.resume_activity(chat_id)

    def _coach_context(self, chat_id: int) -> tuple[str, str, dict[str, Any]]:
        user = self.storage.get_user(chat_id)
        base = {
            "instruction_language": str(user["instruction_language"]),
            "translation_language": str(user["translation_language"]),
            "target_language": str(user["target_language"]),
            "learner_level": practice_level(user),
        }
        stage = str(user["stage"])
        if stage in {"scenario", "practice"} and user["current_scenario"]:
            scenario = self._scenarios_for_user(user)[str(user["current_scenario"])]
            step_index = min(int(user["current_step"]), len(scenario.steps) - 1)
            step = scenario.steps[step_index]
            return (
                "scenario",
                str(user["current_session"] or scenario.id),
                {
                    **base,
                    "title": scenario.title_pl,
                    "goal": scenario.objective_ru,
                    "interlocutor": step.interlocutor_pl,
                    "task": step.context_ru,
                    "hint": step.hint_ru,
                    "reference": step.target_chunk,
                    "learning_cards": [
                        item.to_dict() for item in step.learning_cards
                    ],
                    "step": step_index,
                },
            )
        if stage == "quest" and user["current_quest"]:
            session = self.storage.quest_session(str(user["current_quest"]), chat_id)
            quest = self.quest_catalog.for_language(str(session["target_language"]))[
                str(session["quest_id"])
            ]
            node = quest.nodes[str(session["current_node"])]
            return (
                "quest",
                str(session["id"]),
                {
                    **base,
                    "title": quest.title_target,
                    "goal": quest.goal_ru,
                    "interlocutor": node.message,
                    "task": node.task_ru,
                    "hint": node.hint_ru,
                    "reference": node.reference_answer or node.message,
                    "learning_cards": [
                        item.to_dict() for item in node.learning_cards
                    ],
                    "node": node.id,
                    "facts": self._quest_facts(json.loads(str(session["state_json"]))),
                },
            )
        if stage == "drill" and user["current_drill"]:
            session = self.storage.drill_session(str(user["current_drill"]), chat_id)
            item = self.storage.drill_item(str(session["id"]), int(session["current_index"]))
            return (
                "drill",
                str(session["id"]),
                {
                    **base,
                    "title": str(session["title"]),
                    "interlocutor": str(item["context"]),
                    "task": str(item["prompt"]),
                    "hint": str(item["hint"]),
                    "reference": str(item["correct_answer"]),
                },
            )
        if stage == "review" and user["current_review"]:
            review = self.storage.get_review(int(user["current_review"]), chat_id)
            scenario = self._scenarios_for_user(user)[str(review["scenario_id"])]
            step = scenario.steps[int(review["step_index"])]
            return (
                "review",
                str(review["id"]),
                {
                    **base,
                    "title": scenario.title_pl,
                    "interlocutor": step.interlocutor_pl,
                    "task": step.context_ru,
                    "hint": step.hint_ru,
                    "reference": step.target_chunk,
                    "learning_cards": [
                        item.to_dict() for item in step.learning_cards
                    ],
                },
            )
        raise KeyError("No coachable foreground activity")

    def _background_context(
        self,
        chat_id: int,
        activity_kind: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Add related and due material without coupling the selector to SQLite."""

        user = self.storage.get_user(chat_id)
        enriched = dict(context)
        related: list[dict[str, Any]] = []
        due: list[dict[str, Any]] = []
        current_scenario_id = ""
        current_step = context.get("step")

        if activity_kind in {"scenario", "review"}:
            if activity_kind == "scenario":
                current_scenario_id = str(user["current_scenario"] or "")
            elif user["current_review"]:
                current_review = self.storage.get_review(
                    int(user["current_review"]), chat_id
                )
                current_scenario_id = str(current_review["scenario_id"])
                current_step = int(current_review["step_index"])
            scenarios = self._scenarios_for_user(user)
            scenario = scenarios.get(current_scenario_id)
            if scenario is not None:
                evidence = self.storage.problem_history(
                    chat_id, str(user["target_language"]), limit=20
                )
                weak_indices = [
                    int(item["step_index"])
                    for item in evidence["scenario_steps"]
                    if str(item["scenario_id"]) == current_scenario_id
                ]
                ordered_indices = weak_indices + list(range(len(scenario.steps)))
                seen: set[int] = set()
                for index in ordered_indices:
                    if index in seen or index == current_step:
                        continue
                    seen.add(index)
                    step = scenario.steps[index]
                    related.append(
                        {
                            "title": scenario.title_pl,
                            "task": step.context_ru,
                            "interlocutor": step.interlocutor_pl,
                            "reference": step.target_chunk,
                            "learning_cards": [
                                item.to_dict() for item in step.learning_cards
                            ],
                            "step": index,
                        }
                    )

        if activity_kind == "quest" and user["current_quest"]:
            session = self.storage.quest_session(str(user["current_quest"]), chat_id)
            quest = self.quest_catalog.for_language(str(session["target_language"]))[
                str(session["quest_id"])
            ]
            current_node = str(session["current_node"])
            for node in quest.nodes.values():
                if node.id == current_node or not node.reference_answer:
                    continue
                related.append(
                    {
                        "title": quest.title_target,
                        "task": node.task_ru,
                        "interlocutor": node.message,
                        "reference": node.reference_answer,
                        "learning_cards": [
                            item.to_dict() for item in node.learning_cards
                        ],
                        "node": node.id,
                    }
                )

        scenarios = self._scenarios_for_user(user)
        for review in self.storage.pending_reviews(chat_id):
            scenario = scenarios.get(str(review["scenario_id"]))
            step_index = int(review["step_index"])
            if scenario is None or not 0 <= step_index < len(scenario.steps):
                continue
            if (
                str(review["scenario_id"]) == current_scenario_id
                and step_index == current_step
            ):
                continue
            step = scenario.steps[step_index]
            due.append(
                {
                    "title": scenario.title_pl,
                    "task": step.context_ru,
                    "interlocutor": step.interlocutor_pl,
                    "reference": step.target_chunk,
                    "learning_cards": [
                        item.to_dict() for item in step.learning_cards
                    ],
                    "step": step_index,
                }
            )

        enriched["related_candidates"] = related[:8]
        enriched["due_candidates"] = due[:8]
        return enriched

    def send_background_card(self, chat_id: int, *, scheduled: bool = False) -> bool:
        return self._send_background_card(chat_id, scheduled=scheduled)

    def _semantic_practice_context(
        self, chat_id: int
    ) -> tuple[str, str, dict[str, Any]] | None:
        try:
            activity_kind, activity_id, context = self._coach_context(chat_id)
        except KeyError:
            return None
        if activity_kind == "drill":
            return None
        context = self._background_context(chat_id, activity_kind, context)
        return activity_kind, activity_id, context

    def show_semantic_practice(
        self, chat_id: int, *, return_to: str = "toolkit"
    ) -> None:
        language = self._language(chat_id)
        semantic_context = self._semantic_practice_context(chat_id)
        kinds = (
            self.background_learning.semantic_kinds(semantic_context[2])
            if semantic_context
            else ()
        )
        if kinds:
            available = "\n".join(
                f"• {self._t(chat_id, f'background.kind.{kind}')}"
                for kind in kinds
            )
            body = self._t(
                chat_id, "background.menu_summary", available=available
            )
            keyboard: list[list[dict[str, str]]] = [
                [
                    {
                        "text": self._t(chat_id, f"background.kind.{kind}"),
                        "callback_data": f"background:start:{kind}",
                    }
                ]
                for kind in kinds
            ]
        else:
            body = self._t(chat_id, "background.menu_unavailable")
            keyboard = []
            if getattr(
                self.storage, "open_activity_count", lambda _chat_id: 0
            )(chat_id):
                keyboard.append(
                    [
                        {
                            "text": self._t(chat_id, "action.activities"),
                            "callback_data": "activities:list",
                        }
                    ]
                )
            keyboard.append(
                [
                    {
                        "text": self._t(chat_id, "action.choose_situation"),
                        "callback_data": "scenarios:list",
                    }
                ]
            )
        destination = "practice" if return_to == "practice" else "toolkit"
        return_to = destination
        keyboard.append(back_row(language, return_to, destination))
        self._workspace(
            chat_id,
            card(self._t(chat_id, "background.menu_title"), body),
            keyboard,
            surface="background_menu",
        )

    def start_semantic_practice(
        self, chat_id: int, semantic_kind: str | None = None
    ) -> None:
        if not self._send_background_card(
            chat_id, semantic_only=True, semantic_kind=semantic_kind
        ):
            self.show_semantic_practice(chat_id)

    def _send_background_card(
        self,
        chat_id: int,
        *,
        scheduled: bool = False,
        semantic_only: bool = False,
        semantic_kind: str | None = None,
    ) -> bool:
        semantic_context = self._semantic_practice_context(chat_id)
        if semantic_context is None:
            return False
        activity_kind, activity_id, context = semantic_context
        draft = self.background_learning.build(
            chat_id,
            activity_kind,
            activity_id,
            context,
            semantic_only=semantic_only,
            semantic_kind=semantic_kind,
        )
        if draft is None:
            return False
        prompt = self._t(chat_id, f"background.prompt.{draft.card_type}")
        card_id = self.storage.create_background_card(
            chat_id,
            activity_kind=draft.activity_kind,
            activity_id=draft.activity_id,
            topic=draft.topic,
            source_step=draft.source_step,
            reason=draft.reason,
            card_type=draft.card_type,
            prompt=prompt,
            context=draft.context,
            correct_answer=draft.correct_answer,
            accepted_answers=list(draft.accepted_answers),
            explanation=draft.explanation,
        )
        keyboard = [
            [{"text": self._t(chat_id, "toolkit.forgot"), "callback_data": f"background:reveal:{card_id}"}],
            [{"text": self._t(chat_id, "background.return"), "callback_data": "background:return"}],
        ]
        if scheduled:
            keyboard.extend(self._scheduled_reminder_controls(chat_id))
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "background.title", topic=draft.topic),
                f"{self._t(chat_id, f'background.reason.{draft.reason}')}\n\n"
                f"{prompt}\n\n{draft.context}\n\n"
                f"{self._t(chat_id, 'background.write')}",
            ),
            keyboard,
            force_new=scheduled,
            surface="background_card",
        )
        return True

    def answer_background_card(self, chat_id: int, card_id: int, response: str) -> None:
        row = self.storage.background_card(chat_id, card_id)
        if str(row["status"]) != "pending":
            self._notice(chat_id, self._t(chat_id, "difficulty.stale"))
            return
        user = self.storage.get_user(chat_id)
        score, corrected = self.background_learning.evaluate(
            row,
            response,
            str(user["instruction_language"]),
            str(user["target_language"]),
        )
        self.storage.answer_background_card(chat_id, card_id, response, score)
        title = (
            self._t(chat_id, "background.correct")
            if score >= 0.6
            else self._t(chat_id, "background.retry")
        )
        body = self._t(
            chat_id,
            "background.feedback",
            answer=corrected,
            source=str(row["explanation"]),
        )
        keyboard = [
            [{"text": self._t(chat_id, "background.return"), "callback_data": "background:return"}]
        ]
        if str(row["card_type"]) in LEARNING_CARD_KINDS:
            keyboard.insert(
                0,
                [
                    {
                        "text": self._t(chat_id, "background.more"),
                        "callback_data": "background:menu:toolkit",
                    }
                ],
            )
        self._workspace(
            chat_id,
            card(title, body),
            keyboard,
            force_new=True,
            surface="background_feedback",
        )

    def reveal_background_card(self, chat_id: int, card_id: int) -> None:
        row = self.storage.background_card(chat_id, card_id)
        if str(row["status"]) != "pending":
            return
        self.storage.answer_background_card(chat_id, card_id, "[revealed]", 0.0)
        keyboard = [
            [{"text": self._t(chat_id, "background.return"), "callback_data": "background:return"}]
        ]
        if str(row["card_type"]) in LEARNING_CARD_KINDS:
            keyboard.insert(
                0,
                [
                    {
                        "text": self._t(chat_id, "background.more"),
                        "callback_data": "background:menu:toolkit",
                    }
                ],
            )
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "background.revealed"),
                self._t(chat_id, "background.reference", answer=row["correct_answer"]),
            ),
            keyboard,
            surface="background_feedback",
        )

    def close_background_card(self, chat_id: int) -> None:
        self.storage.dismiss_background_card(chat_id)
        self.resume_activity(chat_id)

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

    def show_quests(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        language = self._language(chat_id)
        quests = self.quest_catalog.for_user(user)
        if not quests:
            self._workspace(
                chat_id,
                card(
                    self._t(chat_id, "quest.list_title"),
                    self._t(chat_id, "quest.unavailable"),
                ),
                [home_row(language)],
                surface="quest_list",
            )
            return
        history = {
            str(row["quest_id"]): row
            for row in self.storage.quest_history(
                chat_id, str(user["target_language"])
            )
        }
        keyboard: list[list[dict[str, str]]] = []
        if user["stage"] == "quest" and user["current_quest"]:
            keyboard.append(
                [
                    {
                        "text": self._t(chat_id, "quest.continue"),
                        "callback_data": "quest:resume",
                    }
                ]
            )
        for quest in quests.values():
            completed = (
                int(history[quest.id]["successes"] or 0)
                if quest.id in history
                else 0
            )
            marker = "✓ " if completed else ""
            keyboard.append(
                [
                    {
                        "text": f"{marker}{quest.title_target}",
                        "callback_data": f"quest:start:{quest.id}",
                    }
                ]
            )
        keyboard.append(home_row(language))
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "quest.list_title"),
                self._t(chat_id, "quest.list_summary"),
            ),
            keyboard,
            surface="quest_list",
        )

    def begin_quest(self, chat_id: int, quest_id: str) -> None:
        user = self.storage.get_user(chat_id)
        quests = self.quest_catalog.for_user(user)
        quest = quests.get(quest_id)
        if quest is None:
            self.show_quests(chat_id)
            return
        if str(user["stage"]) == "quest" and user["current_quest"]:
            current = self.storage.quest_session(str(user["current_quest"]), chat_id)
            if str(current["quest_id"]) == quest_id:
                self.resume_quest(chat_id)
                return
        quest_session_id = self.storage.start_quest(
            chat_id, quest.id, quest.start_node, preserve_active=True
        )
        briefing = self._instruction_text(
            chat_id,
            f"Цель: {quest.goal_ru}\n\n{quest.briefing_ru}",
            "quest-briefing",
        )
        self._workspace(
            chat_id,
            card(self._t(chat_id, "quest.briefing"), briefing),
            [
                [
                    {
                        "text": self._t(chat_id, "quest.start"),
                        "callback_data": f"quest:next:{quest_session_id}",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "action.stop"),
                        "callback_data": "quest:stop:confirm",
                    }
                ],
            ],
            surface="quest_briefing",
        )

    def resume_quest(self, chat_id: int, *, scheduled: bool = False) -> None:
        user = self.storage.get_user(chat_id)
        quest_session_id = str(user["current_quest"] or "")
        if str(user["stage"]) != "quest" or not quest_session_id:
            self.show_quests(chat_id)
            return
        self.send_quest_node(chat_id, quest_session_id, scheduled=scheduled)

    def send_quest_node(
        self, chat_id: int, quest_session_id: str, *, scheduled: bool = False
    ) -> None:
        user = self.storage.get_user(chat_id)
        if (
            str(user["stage"]) != "quest"
            or str(user["current_quest"] or "") != quest_session_id
        ):
            self._notice(chat_id, self._t(chat_id, "difficulty.stale"))
            return
        session = self.storage.quest_session(quest_session_id, chat_id)
        if str(session["status"]) != "active":
            self.show_quests(chat_id)
            return
        quest = self.quest_catalog.for_language(str(session["target_language"]))[
            str(session["quest_id"])
        ]
        node = quest.nodes[str(session["current_node"])]
        if node.mode == "ending":
            self._complete_quest_view(chat_id, quest, node, quest_session_id)
            return
        step = int(session["steps_taken"]) + 1
        task = self._instruction_text(chat_id, node.task_ru, "quest-task")
        body = [
            f"{self._t(chat_id, 'quest.mission')}: {quest.title_target}",
            self._t(chat_id, "quest.step", step=step),
        ]
        facts = self._quest_facts(json.loads(str(session["state_json"])))
        if facts:
            body.append(
                f"{self._t(chat_id, 'quest.facts')}\n"
                + "\n".join(f"• {value}" for value in facts)
            )
        body.extend(
            [
                f"{node.speaker}\n{node.message}",
                f"{self._t(chat_id, 'quest.your_move')}\n{task}",
            ]
        )
        keyboard: list[list[dict[str, str]]] = []
        body.append(self._t(chat_id, "quest.write"))
        keyboard.extend(
            [
                [
                    {
                        "text": self._t(chat_id, "action.hint"),
                        "callback_data": f"quest:hint:{quest_session_id}:{node.id}",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "coach.open"),
                        "callback_data": "coach:open",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "action.stop"),
                        "callback_data": "quest:stop:confirm",
                    }
                ],
            ]
        )
        if scheduled:
            keyboard.extend(self._scheduled_reminder_controls(chat_id))
        self._workspace(
            chat_id,
            "\n\n".join(body),
            keyboard,
            force_new=scheduled,
            surface="quest_node",
        )

    @staticmethod
    def _quest_facts(state: dict[str, Any]) -> list[str]:
        ignored = {"yes", "no", "assumed", "none", "unclear"}
        values = []
        for key, value in state.items():
            text = str(value).strip()
            if key.startswith("answer:") or not text or text in ignored:
                continue
            if text not in values:
                values.append(text)
        return values[-3:]

    def handle_quest_text(self, chat_id: int, response: str, user: Any) -> None:
        quest_session_id = str(user["current_quest"] or "")
        session, quest, node = self._quest_context(chat_id, quest_session_id)
        if node.mode != "free":
            self._workspace(
                chat_id,
                self._t(chat_id, "drill.choose"),
                surface="quest_node",
            )
            return
        state = json.loads(str(session["state_json"]))
        transition = answer_free(node, response, state)
        ai_feedback = ""
        if self.ai is not None:
            step = ScenarioStep(
                id=f"quest:{quest.id}:{node.id}",
                interlocutor_pl=node.message,
                context_ru=node.task_ru,
                hint_ru=node.hint_ru,
                expected_groups=node.expected_groups,
                target_chunk=node.reference_answer,
                bottleneck_ru=node.task_ru,
                task_blocking=True,
            )
            try:
                analysis = self.ai.analyze_response(
                    step,
                    response,
                    str(user["instruction_language"]),
                    str(user["target_language"]),
                    transition.points,
                    practice_level(user),
                )
                score = analysis.score
                successful = analysis.task_achieved
                transition = answer_free(
                    node,
                    response,
                    state,
                    score=score,
                    successful=successful,
                )
                self.storage.add_ai_analysis(
                    chat_id=chat_id,
                    operation="quest_response",
                    target_language=str(user["target_language"]),
                    source_text=response,
                    result=analysis.to_dict(),
                    provider=analysis.provider,
                    model=analysis.model,
                    prompt_version=analysis.prompt_version,
                    latency_ms=analysis.latency_ms,
                    usage=analysis.usage,
                    scenario_id=quest.id,
                    step_index=int(session["steps_taken"]),
                )
                self.storage.event(
                    chat_id,
                    "quest_ai_analysis_completed",
                    {"quest_id": quest.id, "node_id": node.id},
                )
                details = [*analysis.critical_corrections[:1]]
                if analysis.natural_response:
                    details.append(analysis.natural_response)
                ai_feedback = "\n\n".join(details)
            except AIError:
                LOGGER.exception("Quest response analysis failed; using local rubric")
                self.storage.event(
                    chat_id,
                    "ai_fallback_used",
                    {"operation": "quest_response", "quest_id": quest.id},
                )
        self._apply_quest_transition(
            chat_id,
            quest_session_id,
            quest,
            node,
            transition,
            input_kind="text",
            user_answer=response,
            choice_id=None,
            force_new=True,
            extra_feedback=ai_feedback,
        )

    def _quest_context(
        self, chat_id: int, quest_session_id: str
    ) -> tuple[Any, Quest, QuestNode]:
        session = self.storage.quest_session(quest_session_id, chat_id)
        quest = self.quest_catalog.for_language(str(session["target_language"]))[
            str(session["quest_id"])
        ]
        return session, quest, quest.nodes[str(session["current_node"])]

    def _apply_quest_transition(
        self,
        chat_id: int,
        quest_session_id: str,
        quest: Quest,
        node: QuestNode,
        transition: QuestTransition,
        *,
        input_kind: str,
        user_answer: str,
        choice_id: str | None,
        force_new: bool = False,
        extra_feedback: str = "",
    ) -> None:
        advanced = self.storage.advance_quest(
            quest_session_id,
            chat_id,
            node.id,
            transition.next_node,
            input_kind=input_kind,
            user_answer=user_answer,
            choice_id=choice_id,
            score=transition.points,
            outcome=transition.outcome,
            state=transition.state,
        )
        if not advanced:
            self._notice(chat_id, self._t(chat_id, "difficulty.stale"))
            return
        feedback = self._instruction_text(
            chat_id, transition.feedback_ru, "quest-consequence"
        )
        if extra_feedback:
            feedback += "\n\n" + extra_feedback
        if transition.reference_answer:
            feedback += "\n\n" + self._t(
                chat_id,
                "quest.reference",
                answer=transition.reference_answer,
            )
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, f"quest.outcome.{transition.outcome}"),
                feedback,
            ),
            [
                [
                    {
                        "text": self._t(chat_id, "action.next"),
                        "callback_data": f"quest:next:{quest_session_id}",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "action.stop"),
                        "callback_data": "quest:stop:confirm",
                    }
                ],
            ],
            force_new=force_new,
            surface="quest_consequence",
        )
        self._schedule_next_assignment(chat_id)

    def show_quest_hint(
        self, chat_id: int, quest_session_id: str, node_id: str
    ) -> None:
        user = self.storage.get_user(chat_id)
        if (
            str(user["stage"]) != "quest"
            or str(user["current_quest"] or "") != quest_session_id
        ):
            self._notice(chat_id, self._t(chat_id, "difficulty.stale"))
            return
        session, _, node = self._quest_context(chat_id, quest_session_id)
        if str(session["current_node"]) != node_id:
            return
        hint = self._instruction_text(chat_id, node.hint_ru, "quest-hint")
        self._workspace(
            chat_id,
            card(self._t(chat_id, "action.hint"), hint),
            [
                [
                    {
                        "text": self._t(chat_id, "action.resume_task"),
                        "callback_data": f"quest:next:{quest_session_id}",
                    }
                ]
            ],
            surface="quest_hint",
        )

    def _complete_quest_view(
        self, chat_id: int, quest: Quest, ending_node: QuestNode, quest_session_id: str
    ) -> None:
        session = self.storage.complete_quest(
            quest_session_id, chat_id, ending_node.ending
        )
        steps = max(1, int(session["steps_taken"]))
        score = round(float(session["score"]) / steps * 100)
        summary = self._instruction_text(
            chat_id, ending_node.summary_ru, "quest-ending"
        )
        body = (
            f"{summary}\n\n"
            + self._t(chat_id, "quest.score", score=score, steps=steps)
        )
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, f"quest.ending.{ending_node.ending}"),
                body,
            ),
            [
                [
                    {
                        "text": self._t(chat_id, "quest.try_again"),
                        "callback_data": f"quest:start:{quest.id}",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "action.quests"),
                        "callback_data": "quests:list",
                    }
                ],
                home_row(self._language(chat_id)),
            ],
            surface="quest_ending",
        )

    def confirm_stop_quest(self, chat_id: int) -> None:
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "quest.stop_title"),
                self._t(chat_id, "quest.stop_summary"),
            ),
            [
                [
                    {
                        "text": self._t(chat_id, "quest.continue"),
                        "callback_data": "quest:resume",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "action.confirm_finish"),
                        "callback_data": "quest:stop",
                    }
                ],
            ],
            surface="quest_stop_confirmation",
        )

    def stop_quest(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        if user["current_quest"]:
            self.storage.abandon_quest(str(user["current_quest"]), chat_id)
        self.show_quests(chat_id)

    def resume_activity(self, chat_id: int) -> None:
        """Render whichever learning activity currently owns the profile state."""
        current = self.storage.get_user(chat_id)
        if current["stage"] == "quest" and current["current_quest"]:
            self.resume_quest(chat_id)
        elif current["stage"] == "drill" and current["current_drill"]:
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

    def _confirm_target_language_change(self, chat_id: int, language: str) -> None:
        """A course change is destructive, so it cannot be hidden in settings."""
        interface = self._language(chat_id)
        copy = {
            "title": {
                "ru": "Сменить изучаемый язык?",
                "uk": "Змінити мову навчання?",
                "en": "Change the target language?",
                "pl": "Zmienić język docelowy?",
            },
            "summary": {
                "ru": "Текущее занятие относится к другому языку и будет завершено. Сохранённые ответы останутся в истории.",
                "uk": "Поточне заняття стосується іншої мови й буде завершене. Збережені відповіді залишаться в історії.",
                "en": "The current practice belongs to another language and will end. Saved answers will remain in your history.",
                "pl": "Bieżące zajęcia dotyczą innego języka i zostaną zakończone. Zapisane odpowiedzi pozostaną w historii.",
            },
            "keep": {
                "ru": "Оставить текущий язык",
                "uk": "Залишити поточну мову",
                "en": "Keep the current language",
                "pl": "Zostaw obecny język",
            },
            "confirm": {
                "ru": "Завершить и сменить",
                "uk": "Завершити й змінити",
                "en": "End and change",
                "pl": "Zakończ i zmień",
            },
        }
        text = lambda key: copy[key].get(interface, copy[key]["ru"])
        self._workspace(
            chat_id,
            card(text("title"), text("summary")),
            [
                [{"text": text("keep"), "callback_data": "settings:target:keep"}],
                [
                    {
                        "text": text("confirm"),
                        "callback_data": f"settings:target:confirm:{language}",
                    }
                ],
            ],
            surface="target_language_confirmation",
        )

    def _cancel_all_activities(self, chat_id: int) -> None:
        """End all open work after an explicit destructive course change."""
        self.storage.abandon_all_activities(chat_id)

    def begin_scenario(
        self,
        chat_id: int,
        scenario_id: str,
        *,
        allow_switch: bool = False,
        preserve_active: bool = False,
    ) -> None:
        user = self.storage.get_user(chat_id)
        scenario = self._scenarios_for_user(user).get(scenario_id)
        if not scenario:
            self._notice(chat_id, "Этот сценарий не найден.")
            return
        current_scenario = str(user["current_scenario"] or "")
        if not allow_switch and self.menu.resume_action(user):
            if str(user["stage"]) in {"scenario", "practice"} and current_scenario == scenario_id:
                self.resume_activity(chat_id)
                return
            preserve_active = True
        self.storage.start_session(
            chat_id, scenario_id, preserve_active=preserve_active
        )
        description = self._instruction_text(
            chat_id,
            f"Цель: {scenario.objective_ru}\nСитуация: {scenario.opening_ru}",
            "scenario-opening",
        )
        self.send_scenario_step(chat_id, scenario, 0, intro=description)

    def show_activities(self, chat_id: int) -> None:
        user = self.storage.get_user(chat_id)
        language = self._language(chat_id)
        activities = self.activities.list_open(chat_id)
        if not activities:
            self._workspace(
                chat_id,
                card(
                    self._t(chat_id, "activities.title"),
                    self._t(chat_id, "activities.empty"),
                ),
                [home_row(language)],
                surface="activities",
            )
            return
        scenarios = self._scenarios_for_user(user)
        quests = self.quest_catalog.for_user(user)
        keyboard: list[list[dict[str, str]]] = []
        lines: list[str] = []
        for activity in activities:
            if activity.kind == "scenario":
                scenario = scenarios.get(activity.content_id)
                title = scenario.title_pl if scenario else activity.title_hint
                total = len(scenario.steps) if scenario else None
                position = f"{min(activity.current + 1, total)}/{total}" if total else activity.position_label
            elif activity.kind == "quest":
                quest = quests.get(activity.content_id)
                title = quest.title_target if quest else activity.title_hint
                position = self._t(chat_id, "activities.quest_steps", count=activity.current)
            else:
                title = activity.title_hint
                position = activity.position_label
            status = self._t(
                chat_id,
                "activities.current" if activity.is_foreground else "activities.paused",
            )
            kind = self._t(chat_id, f"activities.kind.{activity.kind}")
            lines.append(f"{status} · {kind}\n{title} · {position}")
            keyboard.append(
                [
                    {
                        "text": self._t(chat_id, "activities.resume", title=title),
                        "callback_data": f"activity:resume:{activity.kind}:{activity.session_id}",
                    }
                ]
            )
        keyboard.append(home_row(language))
        self._workspace(
            chat_id,
            card(self._t(chat_id, "activities.title"), "\n\n".join(lines)),
            keyboard,
            surface="activities",
        )

    def resume_saved_scenario(self, chat_id: int, session_id: str) -> None:
        try:
            session = self.storage.resume_scenario_session(chat_id, session_id)
        except KeyError:
            self.show_activities(chat_id)
            return
        scenario = self._scenarios_for_chat(chat_id).get(str(session["scenario_id"]))
        if scenario is None:
            self.show_activities(chat_id)
            return
        step = int(self.storage.get_user(chat_id)["current_step"])
        if step >= len(scenario.steps):
            self.begin_practice(chat_id, scenario, session_id)
            return
        self.send_scenario_step(chat_id, scenario, step)

    def resume_saved_quest(self, chat_id: int, quest_session_id: str) -> None:
        try:
            self.storage.resume_quest_session(chat_id, quest_session_id)
        except KeyError:
            self.show_activities(chat_id)
            return
        self.resume_quest(chat_id)

    def resume_saved_drill(self, chat_id: int, drill_id: str) -> None:
        try:
            self.storage.resume_drill_session(chat_id, drill_id)
        except KeyError:
            self.show_activities(chat_id)
            return
        self.send_drill_item(chat_id, drill_id)

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
                [{"text": tr(language, "coach.open"), "callback_data": "coach:open"}],
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
        block_label = self._copy(chat_id, "practice.label")
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
            retry = self._copy(chat_id, "practice.retry")
            self._workspace(
                chat_id,
                f"{retry}\n{step.target_chunk}",
                surface="practice_retry",
            )
        else:
            fallback = self._copy(chat_id, "practice.fallback")
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
        keyboard = [
            [{"text": "🧩 Закрепить эту фразу", "callback_data": "drill:start"}],
            [{"text": "Выбрать ещё ситуацию", "callback_data": "scenarios:list"}],
        ]
        if self.storage.open_activity_count(chat_id):
            keyboard.insert(
                0,
                [
                    {
                        "text": self._t(chat_id, "action.activities"),
                        "callback_data": "activities:list",
                    }
                ],
            )
        self._workspace(
            chat_id,
            card(
                "Маршрут пройден",
                f"Проверка назначена через {interval} дн.\n\n"
                "Примените это в реальной ситуации. Вернуться к отметке результата можно позже в разделе «Реальные ситуации».",
                "ПОВТОР",
            ),
            [
                *keyboard,
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

    def show_outcomes(self, chat_id: int) -> None:
        """Ask about transfer only in a dedicated, voluntary follow-up flow."""
        user = self.storage.get_user(chat_id)
        language = self._language(chat_id)
        sessions = self.storage.pending_outcome_sessions(
            chat_id, str(user["target_language"])
        )
        copy = {
            "title": {
                "ru": "Реальные ситуации",
                "uk": "Реальні ситуації",
                "en": "Real situations",
                "pl": "Prawdziwe sytuacje",
            },
            "summary": {
                "ru": "Отмечайте только те ситуации, в которых уже использовали язык вне тренажёра.",
                "uk": "Позначайте лише ситуації, у яких уже використовували мову поза тренажером.",
                "en": "Mark only situations where you have already used the language outside practice.",
                "pl": "Oznaczaj tylko sytuacje, w których użyto już języka poza treningiem.",
            },
            "empty": {
                "ru": "Пока нет завершённых ситуаций без отметки. Вернитесь сюда после реального разговора.",
                "uk": "Поки немає завершених ситуацій без позначки. Поверніться сюди після реальної розмови.",
                "en": "There are no completed situations to mark yet. Return after a real conversation.",
                "pl": "Nie ma jeszcze ukończonych sytuacji do oznaczenia. Wróć po prawdziwej rozmowie.",
            },
        }
        text = lambda key: copy[key].get(language, copy[key]["ru"])
        if not sessions:
            self._workspace(
                chat_id,
                card(text("title"), text("empty")),
                [home_row(language)],
                surface="outcomes",
            )
            return
        scenarios = self._scenarios_for_user(user)
        keyboard: list[list[dict[str, str]]] = []
        for session in sessions:
            scenario = scenarios.get(str(session["scenario_id"]))
            label = scenario.title_pl if scenario else str(session["scenario_id"])
            keyboard.append(
                [
                    {
                        "text": label,
                        "callback_data": f"outcome:select:{session['id']}",
                    }
                ]
            )
        keyboard.append(home_row(language))
        self._workspace(
            chat_id,
            card(text("title"), text("summary")),
            keyboard,
            surface="outcomes",
        )

    def show_outcome_choices(self, chat_id: int, session_id: str) -> None:
        try:
            session = self.storage.session(session_id)
        except KeyError:
            self.show_outcomes(chat_id)
            return
        if int(session["chat_id"]) != chat_id or session["status"] != "completed":
            self.show_outcomes(chat_id)
            return
        language = self._language(chat_id)
        labels = {
            "success": {"ru": "✅ Получилось", "uk": "✅ Вийшло", "en": "✅ It worked", "pl": "✅ Udało się"},
            "partial": {"ru": "🟡 Частично", "uk": "🟡 Частково", "en": "🟡 Partly", "pl": "🟡 Częściowo"},
            "failed": {"ru": "🔴 Не получилось", "uk": "🔴 Не вийшло", "en": "🔴 It did not work", "pl": "🔴 Nie udało się"},
        }
        self._workspace(
            chat_id,
            card(
                {"ru": "Как прошло в реальности?", "uk": "Як пройшло в реальності?", "en": "How did it go in real life?", "pl": "Jak poszło w rzeczywistości?"}.get(language, "Как прошло в реальности?"),
                {"ru": "Выберите результат только этого реального разговора.", "uk": "Оберіть результат лише цієї реальної розмови.", "en": "Choose the result of this real conversation only.", "pl": "Wybierz wynik tylko tej prawdziwej rozmowy."}.get(language, "Выберите результат только этого реального разговора."),
            ),
            [
                [{"text": labels["success"].get(language, labels["success"]["ru"]), "callback_data": f"outcome:report:{session_id}:success"}],
                [{"text": labels["partial"].get(language, labels["partial"]["ru"]), "callback_data": f"outcome:report:{session_id}:partial"}],
                [{"text": labels["failed"].get(language, labels["failed"]["ru"]), "callback_data": f"outcome:report:{session_id}:failed"}],
                [{"text": {"ru": "Ещё не применял", "uk": "Ще не застосовував", "en": "I have not used it yet", "pl": "Jeszcze nie użyłem/am"}.get(language, "Ещё не применял"), "callback_data": "outcomes:list"}],
            ],
            surface="outcome_choices",
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

    def offer_text_actions(self, chat_id: int, text: str) -> None:
        phrase = text.strip()
        if not phrase:
            self.home(chat_id)
            return
        inbox_id = self.storage.save_text_inbox(chat_id, phrase)
        user = self.storage.get_user(chat_id)
        language = self._language(chat_id)
        target = LANGUAGE_LABELS.get(
            str(user["target_language"]), str(user["target_language"])
        )
        translation = LANGUAGE_LABELS.get(
            str(user["translation_language"]), str(user["translation_language"])
        )
        keyboard: list[list[dict[str, str]]] = [
            [
                {
                    "text": self._t(
                        chat_id, "text_action.translate_to", name=target
                    ),
                    "callback_data": f"text:translate:{inbox_id}:to_target",
                }
            ]
        ]
        if str(user["translation_language"]) != str(user["target_language"]):
            keyboard.append(
                [
                    {
                        "text": self._t(
                            chat_id,
                            "text_action.translate_to",
                            name=translation,
                        ),
                        "callback_data": f"text:translate:{inbox_id}:from_target",
                    }
                ]
            )
        keyboard.extend(
            [
                [
                    {
                        "text": self._t(chat_id, "text_action.check"),
                        "callback_data": f"text:check:{inbox_id}",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "text_action.rephrase"),
                        "callback_data": f"text:rephrase:{inbox_id}",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "text_action.grammar"),
                        "callback_data": f"text:grammar:{inbox_id}",
                    }
                ],
                home_row(language),
            ]
        )
        preview = phrase if len(phrase) <= 800 else phrase[:797] + "…"
        self._workspace(
            chat_id,
            card(
                self._t(chat_id, "text_action.title"),
                self._t(chat_id, "text_action.summary", text=preview),
            ),
            keyboard,
            force_new=True,
            surface="text_actions",
        )

    def translate_text_inbox(self, chat_id: int, inbox_id: int, mode: str) -> None:
        try:
            phrase = str(self.storage.text_inbox(chat_id, inbox_id)["text"])
        except KeyError:
            self._notice(chat_id, self._t(chat_id, "text_action.expired"))
            return
        self.storage.set_user_state(chat_id, toolkit_input_mode=mode)
        self.toolkit.handle_phrase(chat_id, phrase)

    def analyze_text_inbox(
        self,
        chat_id: int,
        inbox_id: int,
        *,
        grammar: bool = False,
        variants: bool = False,
    ) -> None:
        try:
            phrase = str(self.storage.text_inbox(chat_id, inbox_id)["text"])
        except KeyError:
            self._notice(chat_id, self._t(chat_id, "text_action.expired"))
            return
        if self.ai is None:
            self._workspace(
                chat_id,
                card(
                    self._t(chat_id, "toolkit.error_title"),
                    self._t(chat_id, "text_action.ai_unavailable"),
                ),
                [home_row(self._language(chat_id))],
                surface="text_action_error",
            )
            return
        user = self.storage.get_user(chat_id)
        step = ScenarioStep(
            id="standalone-phrase",
            interlocutor_pl="",
            context_ru=self._t(chat_id, "text_action.check_context"),
            hint_ru="",
            expected_groups=(),
            target_chunk=phrase,
            bottleneck_ru="",
            task_blocking=False,
        )
        try:
            self.telegram.send_chat_action(chat_id, "typing")
        except TelegramError:
            LOGGER.debug("Could not send text-check typing action", exc_info=True)
        self.storage.event(
            chat_id,
            "ai_analysis_requested",
            {"operation": "standalone_phrase", "inbox_id": inbox_id},
        )
        try:
            analysis = self.ai.analyze_response(
                step,
                phrase,
                str(user["instruction_language"]),
                str(user["target_language"]),
                0.5,
                practice_level(user),
            )
        except AIError:
            LOGGER.exception("Standalone phrase analysis failed")
            self._workspace(
                chat_id,
                card(
                    self._t(chat_id, "toolkit.error_title"),
                    self._t(chat_id, "text_action.ai_unavailable"),
                ),
                [
                    [
                        {
                            "text": self._t(chat_id, "toolkit.retry"),
                            "callback_data": (
                                f"text:grammar:{inbox_id}"
                                if grammar
                                else (
                                    f"text:rephrase:{inbox_id}"
                                    if variants
                                    else f"text:check:{inbox_id}"
                                )
                            ),
                        }
                    ],
                    home_row(self._language(chat_id)),
                ],
                surface="text_action_error",
            )
            return
        analysis_id = self.storage.add_ai_analysis(
            chat_id=chat_id,
            operation="standalone_phrase",
            target_language=str(user["target_language"]),
            source_text=phrase,
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
            {"operation": "standalone_phrase", "analysis_id": analysis_id},
        )
        if grammar:
            self.feedback.explain_grammar(chat_id, analysis_id, "all")
        elif variants:
            self.feedback.show_variants(chat_id, analysis_id)
        else:
            self.feedback.show_result(
                chat_id, analysis, analysis_id, standalone=True
            )

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
            keyboard.extend(self._scheduled_reminder_controls(chat_id))
        keyboard.append(
            [{"text": self._t(chat_id, "coach.open"), "callback_data": "coach:open"}]
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
        if self.storage.open_activity_count(chat_id):
            keyboard.insert(
                0,
                [
                    {
                        "text": self._t(chat_id, "action.activities"),
                        "callback_data": "activities:list",
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
        if self.storage.open_activity_count(chat_id):
            keyboard.insert(
                0,
                [
                    {
                        "text": self._t(chat_id, "action.activities"),
                        "callback_data": "activities:list",
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

    def _scheduled_reminder_controls(self, chat_id: int) -> list[list[dict[str, str]]]:
        """Keep every proactive card dismissible in the same way."""

        return [
            [
                {"text": self._t(chat_id, "action.snooze"), "callback_data": "reminder:snooze:2h"},
                {"text": self._t(chat_id, "action.pause"), "callback_data": "reminder:pause"},
            ],
            [{"text": self._t(chat_id, "action.reminders"), "callback_data": "reminders"}],
        ]

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
        if mode in {"intensive", "aggressive"} and self.send_background_card(
            chat_id, scheduled=True
        ):
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
        if stage == "quest" and user["current_quest"]:
            self.resume_quest(chat_id, scheduled=True)
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

    def send_reengagement_reminder(
        self, chat_id: int, mode: str, inactive_days: int
    ) -> None:
        key = (
            "reengagement.long"
            if inactive_days >= 14
            else "reengagement.week"
            if inactive_days >= 7
            else "reengagement.short"
        )
        self.telegram.send_message(
            chat_id,
            card(
                self._t(chat_id, "reengagement.title"),
                self._t(chat_id, key),
            ),
            [
                [
                    {
                        "text": self._t(chat_id, "reengagement.action"),
                        "callback_data": "reengagement:continue",
                    }
                ],
                [
                    {
                        "text": self._t(chat_id, "action.pause"),
                        "callback_data": "reminder:pause",
                    },
                    {
                        "text": self._t(chat_id, "action.reminders"),
                        "callback_data": "reminders",
                    },
                ],
            ],
        )
        self.storage.event(
            chat_id,
            "reengagement_prompt_shown",
            {"mode": mode, "inactive_days": inactive_days},
        )

    def show_privacy(self, chat_id: int, *, back_to_settings: bool = False) -> None:
        self.menu.show_privacy(chat_id, back_to_settings=back_to_settings)

    def cancel_activity(self, chat_id: int, notify: bool = True) -> None:
        current = self.storage.get_user(chat_id)
        if current["current_quest"]:
            self.storage.abandon_quest(str(current["current_quest"]), chat_id)
        elif current["current_drill"]:
            self.storage.abandon_drill(str(current["current_drill"]), chat_id)
        elif current["current_session"]:
            self.storage.abandon_session(current["current_session"])
        self.storage.set_user_state(
            chat_id,
            stage="idle",
            current_scenario=None,
            current_step=0,
            current_session=None,
            current_review=None,
            current_quest=None,
            pending_assignment=None,
            toolkit_input_mode=None,
        )
        if notify:
            keyboard = [home_row(self._language(chat_id))]
            if self.storage.open_activity_count(chat_id):
                keyboard.insert(
                    0,
                    [
                        {
                            "text": self._t(chat_id, "action.activities"),
                            "callback_data": "activities:list",
                        }
                    ],
                )
            self.telegram.send_message(
                chat_id,
                "Текущее занятие остановлено.",
                keyboard,
            )

    def handle_text(
        self,
        chat_id: int,
        first_name: str,
        text: str,
        message_id: int | None = None,
    ) -> None:
        if not self.is_allowed(chat_id):
            self.telegram.send_message(chat_id, "Сейчас доступна только закрытая alpha.")
            return
        user = self.storage.ensure_user(chat_id, first_name)
        self.storage.record_user_interaction(chat_id)
        command = parse_command(text)
        if command.startswith("/") and (
            user["toolkit_input_mode"]
            or user["profile_input_mode"]
            or user["coach_input_mode"]
            or user["background_card_id"]
        ):
            self.storage.set_user_state(
                chat_id,
                toolkit_input_mode=None,
                profile_input_mode=None,
                coach_input_mode=None,
            )
            if user["coach_session_id"]:
                self.storage.close_coach_session(chat_id)
            if user["background_card_id"]:
                self.storage.dismiss_background_card(chat_id)
            user = self.storage.get_user(chat_id)
        if command in PUBLIC_COMMANDS:
            self._focus_new_surface(chat_id, message_id)
        if command == "/start":
            self.start(chat_id, first_name)
            return
        if command == "/privacy":
            self.show_privacy(chat_id)
            return
        if command == "/delete_me":
            self.telegram.send_message(
                chat_id,
                self._t(chat_id, "delete.confirm"),
                [
                    [
                        {
                            "text": self._t(chat_id, "delete.action"),
                            "callback_data": "delete:confirm",
                        }
                    ],
                    [
                        {
                            "text": self._t(chat_id, "action.cancel"),
                            "callback_data": "home",
                        }
                    ],
                ],
            )
            return
        if not self._has_current_consent(user):
            self.start(chat_id, first_name)
            return
        navigation_action = reply_action(text)
        if navigation_action:
            self._focus_new_surface(chat_id, message_id)
            if user["toolkit_input_mode"]:
                self.storage.set_user_state(chat_id, toolkit_input_mode=None)
                user = self.storage.get_user(chat_id)
            if user["profile_input_mode"]:
                self.storage.set_user_state(chat_id, profile_input_mode=None)
                user = self.storage.get_user(chat_id)
            if user["coach_session_id"]:
                self.storage.close_coach_session(chat_id)
                user = self.storage.get_user(chat_id)
            if user["background_card_id"]:
                self.storage.dismiss_background_card(chat_id)
                user = self.storage.get_user(chat_id)
            self.menu.refresh_navigation(chat_id)
            if navigation_action == "home":
                self.home(chat_id)
            elif navigation_action == "learn":
                self.show_practice_hub(chat_id)
            elif navigation_action == "assistant":
                self.toolkit.show_menu(chat_id)
            elif navigation_action == "profile":
                self.show_settings(chat_id)
            return
        if command == "/activities":
            self.show_activities(chat_id)
            return
        if command == "/practice":
            self.show_practice_hub(chat_id)
            return
        if command == "/settings":
            self.show_settings(chat_id)
            return
        if command == "/progress":
            self.show_progress(chat_id)
            return
        if command == "/tools":
            self.toolkit.show_menu(chat_id)
            return
        if command == "/help":
            self.show_help(chat_id)
            return
        if command == "/grammar":
            command_parts = text.strip().split(maxsplit=1)
            fragment = command_parts[1].strip() if len(command_parts) == 2 else ""
            if fragment:
                self.explain_custom_grammar(chat_id, fragment)
            else:
                self._notice(chat_id, self._t(chat_id, "grammar.usage"))
                self.show_help(chat_id)
            return
        if command.startswith("/"):
            self._notice(chat_id, self._t(chat_id, "help.unknown_command"))
            self.show_help(chat_id)
            return
        if user["background_card_id"]:
            self.answer_background_card(
                chat_id, int(user["background_card_id"]), text
            )
            return
        if user["coach_session_id"]:
            self.answer_coach(
                chat_id, str(user["coach_input_mode"] or "question"), text
            )
            return
        if user["profile_input_mode"]:
            self.save_profile_text(chat_id, text, str(user["profile_input_mode"]))
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
        elif stage == "quest":
            self.handle_quest_text(chat_id, text, user)
        elif stage == "waiting":
            self.offer_text_actions(chat_id, text)
        else:
            self.offer_text_actions(chat_id, text)

    def handle_callback(
        self,
        chat_id: int,
        first_name: str,
        callback_id: str,
        data: str,
        message_id: int | None = None,
    ) -> None:
        if not self.is_allowed(chat_id):
            self.telegram.answer_callback(callback_id, "Закрытая alpha")
            return
        user = self.storage.ensure_user(chat_id, first_name)
        self.storage.record_user_interaction(chat_id)
        try:
            self.telegram.answer_callback(callback_id)
        except TelegramError:
            LOGGER.warning("Could not acknowledge callback", exc_info=True)
        if message_id is not None:
            self.workspace.focus_message(chat_id, message_id)
            user = self.storage.get_user(chat_id)

        if (
            user["toolkit_input_mode"]
            and data not in {"toolkit", "toolkit:resume"}
            and not data.startswith("toolkit:translate:")
        ):
            self.storage.set_user_state(chat_id, toolkit_input_mode=None)
            user = self.storage.get_user(chat_id)

        if user["profile_input_mode"] and not data.startswith("profile:input:"):
            self.storage.set_user_state(chat_id, profile_input_mode=None)
            user = self.storage.get_user(chat_id)

        if user["coach_session_id"] and not data.startswith("coach:"):
            self.storage.close_coach_session(chat_id)
            user = self.storage.get_user(chat_id)

        if user["background_card_id"] and not data.startswith("background:"):
            self.storage.dismiss_background_card(chat_id)
            user = self.storage.get_user(chat_id)

        if data == "consent:accept":
            self.storage.accept_consent(chat_id, CONSENT_VERSION)
            self.home(chat_id)
        elif data == "privacy":
            self.show_privacy(chat_id)
        elif data == "privacy:settings":
            self.show_privacy(chat_id, back_to_settings=True)
        elif data == "delete:confirm":
            language = self._language(chat_id)
            self.storage.delete_user(chat_id)
            self.telegram.send_message(
                chat_id, tr(language, "delete.done")
            )
        elif not self._has_current_consent(user):
            self.start(chat_id, first_name)
        elif data == "home":
            self.home(chat_id)
        elif data == "practice":
            self.show_practice_hub(chat_id)
        elif data == "learn:conversation":
            self.menu.show_conversation_choices(chat_id)
        elif data == "help":
            self.show_help(chat_id)
        elif data == "toolkit":
            self.toolkit.show_menu(chat_id)
        elif data == "coach:open":
            self.open_coach(chat_id)
        elif data == "coach:menu":
            current = self.storage.get_user(chat_id)
            self.storage.set_user_state(chat_id, coach_input_mode=None)
            if current["coach_session_id"]:
                self._show_coach_menu(chat_id, str(current["coach_session_id"]))
            else:
                self.open_coach(chat_id)
        elif data.startswith("coach:input:"):
            self.ask_coach_input(chat_id, data.rsplit(":", 1)[1])
        elif data.startswith("coach:ask:"):
            self.answer_coach(chat_id, data.rsplit(":", 1)[1])
        elif data == "coach:return":
            self.close_coach(chat_id)
        elif data.startswith("background:menu:"):
            self.show_semantic_practice(
                chat_id, return_to=data.rsplit(":", 1)[1]
            )
        elif data == "background:start":
            self.start_semantic_practice(chat_id)
        elif data.startswith("background:start:"):
            semantic_kind = data.rsplit(":", 1)[1]
            if semantic_kind in LEARNING_CARD_KINDS:
                self.start_semantic_practice(chat_id, semantic_kind)
            else:
                self.show_semantic_practice(chat_id)
        elif data.startswith("background:reveal:"):
            self.reveal_background_card(chat_id, int(data.rsplit(":", 1)[1]))
        elif data == "background:return":
            self.close_background_card(chat_id)
        elif data.startswith("text:translate:"):
            _, _, inbox_id, mode = data.split(":", 3)
            self.translate_text_inbox(chat_id, int(inbox_id), mode)
        elif data.startswith("text:check:"):
            self.analyze_text_inbox(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("text:rephrase:"):
            self.analyze_text_inbox(
                chat_id, int(data.rsplit(":", 1)[1]), variants=True
            )
        elif data.startswith("text:grammar:"):
            self.analyze_text_inbox(
                chat_id, int(data.rsplit(":", 1)[1]), grammar=True
            )
        elif data == "quests:list":
            self.show_quests(chat_id)
        elif data.startswith("quest:start:"):
            self.begin_quest(chat_id, data.split(":", 2)[2])
        elif data == "quest:resume":
            self.resume_quest(chat_id)
        elif data.startswith("quest:next:"):
            self.send_quest_node(chat_id, data.split(":", 2)[2])
        elif data.startswith("quest:hint:"):
            _, _, quest_session_id, node_id = data.split(":", 3)
            self.show_quest_hint(chat_id, quest_session_id, node_id)
        elif data == "quest:stop:confirm":
            self.confirm_stop_quest(chat_id)
        elif data == "quest:stop":
            self.stop_quest(chat_id)
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
            self.menu.show_reminder_preview(chat_id)
        elif data == "reengagement:continue":
            current = self.storage.get_user(chat_id)
            mode = str(current["reminder_mode"])
            self.send_scheduled_reminder(
                chat_id, mode if mode != "off" else "gentle"
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
        elif data == "reminder:snooze:2h":
            current = self.storage.get_user(chat_id)
            until = datetime.now(timezone.utc) + timedelta(hours=2)
            self.storage.snooze_reminders(chat_id, until)
            self._workspace(
                chat_id,
                card(
                    self._t(chat_id, "reminders.snoozed_title"),
                    self._t(chat_id, "reminders.snoozed_summary"),
                ),
                [home_row(self._language(chat_id))],
                surface="reminder_snooze",
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
        elif data == "settings:profile":
            self.show_learner_profile(chat_id)
        elif data == "profile:age":
            self.menu.show_profile_choices(chat_id, "age")
        elif data == "profile:role":
            self.menu.show_profile_choices(chat_id, "role")
        elif data.startswith("profile:set:"):
            _, _, kind, value = data.split(":", 3)
            if kind == "age":
                self.learner_profiles.set_age_band(chat_id, value)
            elif kind == "role":
                self.learner_profiles.set_life_role(chat_id, value)
            self.show_learner_profile(chat_id)
        elif data.startswith("profile:input:"):
            self.ask_profile_text(chat_id, data.rsplit(":", 1)[1])
        elif data == "profile:adaptive:toggle":
            profile = self.learner_profiles.get(chat_id)
            self.learner_profiles.set_adaptive_level(
                chat_id, not profile.adaptive_level_enabled
            )
            self.show_learner_profile(chat_id)
        elif data == "settings:languages":
            self.show_learning_settings(chat_id)
        elif data == "settings:target:keep":
            self.show_learning_settings(chat_id)
        elif data.startswith("settings:target:confirm:"):
            language = data.rsplit(":", 1)[1]
            if language in {"pl", "en"}:
                self._cancel_all_activities(chat_id)
                self.storage.set_language(chat_id, "target_language", language)
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
                if (
                    field == "target_language"
                    and language != current_language
                    and self.menu.resume_action(self.storage.get_user(chat_id))
                ):
                    self._confirm_target_language_change(chat_id, language)
                    return
                self.storage.set_language(chat_id, field, language)
                if field == "instruction_language":
                    self.menu.refresh_navigation(chat_id)
                    self.workspace.start_new_surface(chat_id)
                self.show_learning_settings(chat_id)
        elif data == "settings:level":
            self.show_level_choices(chat_id)
        elif data.startswith("settings:"):
            self.show_language_choices(chat_id, data.split(":", 1)[1])
        elif data == "scenarios:list":
            self.show_scenarios(chat_id)
        elif data == "activities:list":
            self.show_activities(chat_id)
        elif data.startswith("activity:resume:"):
            _, _, kind, session_id = data.split(":", 3)
            if kind == "scenario":
                self.resume_saved_scenario(chat_id, session_id)
            elif kind == "quest":
                self.resume_saved_quest(chat_id, session_id)
            elif kind == "drill":
                self.resume_saved_drill(chat_id, session_id)
            else:
                self.show_activities(chat_id)
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
        elif data == "outcomes:list":
            self.show_outcomes(chat_id)
        elif data.startswith("outcome:select:"):
            self.show_outcome_choices(chat_id, data.rsplit(":", 1)[1])
        elif data.startswith("outcome:report:"):
            _, _, session_id, result = data.split(":", 3)
            if self.storage.add_outcome(chat_id, session_id, result):
                self.telegram.send_message(
                    chat_id,
                    "Спасибо. Этот реальный результат поможет подобрать следующее повторение.",
                    [home_row(self._language(chat_id))],
                )
            else:
                self.show_outcomes(chat_id)
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
        else:
            self._notice(chat_id, self._t(chat_id, "navigation.expired"))
            self.home(chat_id)

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
            self.telegram.call("setMyCommands", {"commands": command_payload("ru")})
            for language in ("ru", "uk", "en", "pl"):
                self.telegram.call(
                    "setMyCommands",
                    {
                        "commands": command_payload(language),
                        "language_code": language,
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
