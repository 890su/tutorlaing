from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .content import Scenario, load_scenarios
from .engine import (
    evaluate_response,
    review_due_at,
    review_interval_days,
    select_bottleneck,
)
from .storage import Storage
from .telegram_api import TelegramAPI, TelegramError


LOGGER = logging.getLogger(__name__)


class TutorlaingBot:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        telegram: TelegramAPI | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.telegram = telegram or TelegramAPI(settings.telegram_bot_token)
        self.scenarios = load_scenarios()
        self.offset = 0
        self.running = True

    def is_allowed(self, chat_id: int) -> bool:
        allowed = self.settings.allowed_chat_ids
        return allowed is None or chat_id in allowed

    def home(self, chat_id: int) -> None:
        due_count = len(self.storage.pending_reviews(chat_id))
        review_label = (
            f"🔁 Повторения ({due_count})" if due_count else "🔁 Проверить повторения"
        )
        self.telegram.send_message(
            chat_id,
            "Что тренируем сегодня? Выберите ближайшую реальную ситуацию.",
            [
                [{"text": "🎯 Выбрать ситуацию", "callback_data": "scenarios:list"}],
                [{"text": review_label, "callback_data": "reviews:list"}],
                [{"text": "🔒 Данные и приватность", "callback_data": "privacy"}],
            ],
        )

    def start(self, chat_id: int, first_name: str = "") -> None:
        user = self.storage.ensure_user(chat_id, first_name)
        if not user["consent_at"]:
            self.telegram.send_message(
                chat_id,
                "Cześć! Я помогу подготовиться к реальным разговорам на польском.\n\n"
                "Alpha-версия сохраняет ваши текстовые ответы, результаты и Telegram ID. "
                "Голос не записывается. Данные можно удалить командой /delete_me.\n\n"
                "Продолжить?",
                [
                    [{"text": "✅ Согласен и начать", "callback_data": "consent:accept"}],
                    [{"text": "ℹ️ Подробнее", "callback_data": "privacy"}],
                ],
            )
            return
        self.home(chat_id)

    def show_scenarios(self, chat_id: int) -> None:
        keyboard = [
            [
                {
                    "text": f"{scenario.title_ru} · {scenario.title_pl}",
                    "callback_data": f"scenario:{scenario.id}",
                }
            ]
            for scenario in self.scenarios.values()
        ]
        keyboard.append([{"text": "← Назад", "callback_data": "home"}])
        self.telegram.send_message(
            chat_id,
            "Выберите ситуацию, которая вам реально пригодится. Сессия займёт около 5 минут.",
            keyboard,
        )

    def begin_scenario(self, chat_id: int, scenario_id: str) -> None:
        scenario = self.scenarios.get(scenario_id)
        if not scenario:
            self.telegram.send_message(chat_id, "Этот сценарий не найден.")
            return
        self.storage.start_session(chat_id, scenario_id)
        self.telegram.send_message(
            chat_id,
            f"{scenario.title_ru} · {scenario.title_pl}\n\n"
            f"Цель: {scenario.objective_ru}\n"
            f"Ситуация: {scenario.opening_ru}\n\n"
            "Отвечайте по-польски. Ошибаться можно: сейчас важнее выполнить задачу.",
        )
        self.send_scenario_step(chat_id, scenario, 0)

    def send_scenario_step(
        self, chat_id: int, scenario: Scenario, step_index: int
    ) -> None:
        step = scenario.steps[step_index]
        self.telegram.send_message(
            chat_id,
            f"Собеседник: {step.interlocutor_pl}\n\n"
            f"Ваша задача: {step.context_ru}",
            [
                [{"text": "💡 Подсказка", "callback_data": "hint"}],
                [{"text": "✖ Завершить", "callback_data": "cancel"}],
            ],
        )

    def handle_scenario_response(self, chat_id: int, text: str, user: Any) -> None:
        scenario = self.scenarios[user["current_scenario"]]
        step_index = int(user["current_step"])
        step = scenario.steps[step_index]
        evaluation = evaluate_response(step, text)
        session_id = str(user["current_session"])
        self.storage.add_response(
            session_id,
            step_index,
            "scenario",
            text,
            evaluation.score,
            evaluation.missing_groups,
        )

        if evaluation.successful:
            self.telegram.send_message(chat_id, "✅ Коммуникативная задача выполнена.")
        else:
            self.telegram.send_message(
                chat_id,
                "Ответ принят. В конце выберу одно главное затруднение для короткой тренировки.",
            )

        next_step = step_index + 1
        if next_step < len(scenario.steps):
            self.storage.set_user_state(chat_id, current_step=next_step)
            self.send_scenario_step(chat_id, scenario, next_step)
            return
        self.begin_practice(chat_id, scenario, session_id)

    def begin_practice(
        self, chat_id: int, scenario: Scenario, session_id: str
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
        self.telegram.send_message(
            chat_id,
            "Главное узкое место этой попытки:\n"
            f"— {step.bottleneck_ru}.\n\n"
            "Полезный блок:\n"
            f"{step.target_chunk}\n\n"
            "Напишите эту мысль по-польски своими словами. Можно использовать образец, "
            "но не копируйте его механически.",
            [[{"text": "Пропустить тренировку", "callback_data": "practice:skip"}]],
        )

    def handle_practice_response(self, chat_id: int, text: str, user: Any) -> None:
        scenario = self.scenarios[user["current_scenario"]]
        step_index = int(user["current_step"])
        step = scenario.steps[step_index]
        session_id = str(user["current_session"])
        evaluation = evaluate_response(step, text)
        self.storage.add_response(
            session_id,
            step_index,
            "practice",
            text,
            evaluation.score,
            evaluation.missing_groups,
        )
        attempts = self.storage.response_count(session_id, "practice")
        if evaluation.successful:
            self.telegram.send_message(
                chat_id, "✅ Получилось. Теперь важно проверить фразу позже без подсказки."
            )
            self.finish_session(chat_id, scenario, session_id, step_index, evaluation.score)
        elif attempts < 2:
            self.telegram.send_message(
                chat_id,
                f"Пока не хватает части смысла. Попробуйте ещё раз:\n{step.target_chunk}",
            )
        else:
            self.telegram.send_message(
                chat_id,
                f"Зафиксируем образец и вернёмся к нему позже:\n{step.target_chunk}",
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
        self.telegram.send_message(
            chat_id,
            f"Сессия завершена. Проверка назначена через {interval} дн.\n\n"
            "Когда примените польский в реальной ситуации, отметьте результат:",
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
                [{"text": "Выбрать ещё ситуацию", "callback_data": "scenarios:list"}],
            ],
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
            self.telegram.send_message(chat_id, text, keyboard)
            return
        review = reviews[0]
        scenario = self.scenarios[review["scenario_id"]]
        self.telegram.send_message(
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
        )

    def begin_review(self, chat_id: int, review_id: int) -> None:
        review = self.storage.get_review(review_id, chat_id)
        if review["status"] != "pending":
            self.telegram.send_message(chat_id, "Эта проверка уже завершена.")
            return
        scenario = self.scenarios[review["scenario_id"]]
        step = scenario.steps[int(review["step_index"])]
        self.storage.set_user_state(
            chat_id,
            stage="review",
            current_scenario=scenario.id,
            current_step=int(review["step_index"]),
            current_review=review_id,
            current_session=None,
        )
        self.telegram.send_message(
            chat_id,
            f"Похожая ситуация, без образца.\n\n"
            f"Собеседник: {step.interlocutor_pl}\n\n"
            f"Ваша задача: {step.context_ru}",
            [[{"text": "Отменить", "callback_data": "cancel"}]],
        )

    def handle_review_response(self, chat_id: int, text: str, user: Any) -> None:
        review_id = int(user["current_review"])
        review = self.storage.get_review(review_id, chat_id)
        scenario = self.scenarios[review["scenario_id"]]
        step_index = int(review["step_index"])
        evaluation = evaluate_response(scenario.steps[step_index], text)
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
        self.telegram.send_message(
            chat_id, message, [[{"text": "В меню", "callback_data": "home"}]]
        )

    def show_privacy(self, chat_id: int) -> None:
        self.telegram.send_message(
            chat_id,
            "Alpha хранит Telegram ID, имя, текст ответов, оценки и расписание "
            "повторений. Голос и контакты не собираются. Тексты не отправляются "
            "в продуктовую аналитику или LLM.\n\n"
            "Удалить все данные можно командой /delete_me.",
            [[{"text": "← Назад", "callback_data": "home"}]],
        )

    def cancel_activity(self, chat_id: int) -> None:
        current = self.storage.get_user(chat_id)
        self.storage.abandon_session(current["current_session"])
        self.storage.set_user_state(
            chat_id,
            stage="idle",
            current_scenario=None,
            current_step=0,
            current_session=None,
            current_review=None,
        )
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
        if command == "/scenarios":
            if user["consent_at"]:
                self.show_scenarios(chat_id)
            else:
                self.start(chat_id, first_name)
            return
        if command == "/review":
            self.show_reviews(chat_id)
            return
        if command == "/review_now":
            self.show_reviews(chat_id, include_future=True)
            return
        if command == "/privacy":
            self.show_privacy(chat_id)
            return
        if command == "/delete_me":
            self.telegram.send_message(
                chat_id,
                "Удалить все ваши ответы, результаты и расписание? Это необратимо.",
                [
                    [{"text": "Да, удалить", "callback_data": "delete:confirm"}],
                    [{"text": "Отмена", "callback_data": "home"}],
                ],
            )
            return
        if not user["consent_at"]:
            self.start(chat_id, first_name)
            return

        stage = user["stage"]
        if stage == "scenario":
            self.handle_scenario_response(chat_id, text, user)
        elif stage == "practice":
            self.handle_practice_response(chat_id, text, user)
        elif stage == "review":
            self.handle_review_response(chat_id, text, user)
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
            self.storage.accept_consent(chat_id)
            self.home(chat_id)
        elif data == "home":
            self.home(chat_id)
        elif data == "scenarios:list":
            self.show_scenarios(chat_id)
        elif data.startswith("scenario:"):
            if user["consent_at"]:
                self.begin_scenario(chat_id, data.split(":", 1)[1])
            else:
                self.start(chat_id, first_name)
        elif data == "hint":
            current = self.storage.get_user(chat_id)
            if current["stage"] == "scenario":
                scenario = self.scenarios[current["current_scenario"]]
                step = scenario.steps[int(current["current_step"])]
                self.storage.event(
                    chat_id,
                    "hint_used",
                    {"scenario_id": scenario.id, "step_index": current["current_step"]},
                )
                self.telegram.send_message(chat_id, f"Подсказка: {step.hint_ru}")
        elif data == "practice:skip":
            current = self.storage.get_user(chat_id)
            if current["stage"] == "practice":
                scenario = self.scenarios[current["current_scenario"]]
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
        elif data == "privacy":
            self.show_privacy(chat_id)
        elif data == "delete:confirm":
            self.storage.delete_user(chat_id)
            self.telegram.send_message(
                chat_id, "Все связанные с вашим Telegram ID данные удалены."
            )
        elif data == "cancel":
            self.cancel_activity(chat_id)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            message = update["message"]
            text = message.get("text")
            if not text:
                self.telegram.send_message(
                    int(message["chat"]["id"]),
                    "В этой версии используйте текстовые ответы. Голос появится после проверки качества.",
                )
                return
            chat_id = int(message["chat"]["id"])
            first_name = str(message.get("from", {}).get("first_name", ""))
            self.handle_text(chat_id, first_name, text)
        elif "callback_query" in update:
            callback = update["callback_query"]
            message = callback.get("message")
            if not message:
                return
            self.handle_callback(
                int(message["chat"]["id"]),
                str(callback.get("from", {}).get("first_name", "")),
                str(callback["id"]),
                str(callback.get("data", "")),
            )

    def run_polling(self) -> None:
        LOGGER.info("Tutorlaing Telegram polling started")
        try:
            self.telegram.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "start", "description": "Открыть главное меню"},
                        {"command": "scenarios", "description": "Выбрать ситуацию"},
                        {"command": "review", "description": "Повторения на сегодня"},
                        {"command": "review_now", "description": "Dogfooding: повторить сейчас"},
                        {"command": "privacy", "description": "Как хранятся данные"},
                        {"command": "delete_me", "description": "Удалить мои данные"},
                    ]
                },
            )
        except TelegramError:
            LOGGER.warning("Could not register bot commands", exc_info=True)
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
