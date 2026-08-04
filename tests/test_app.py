import tempfile
import unittest
from pathlib import Path
from typing import Any

from tutorlaing.ai import (
    AIError,
    Alternative,
    DrillEvaluation,
    DrillItem,
    DrillPack,
    GrammarChunk,
    PhraseTranslation,
    ResponseAnalysis,
)
from tutorlaing.app import TutorlaingBot
from tutorlaing.config import Settings
from tutorlaing.privacy import CONSENT_VERSION
from tutorlaing.storage import Storage


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.callbacks: list[str] = []
        self.reply_keyboards: list[list[list[str]]] = []
        self.deleted: list[tuple[int, int]] = []
        self.temporary: list[dict[str, Any]] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> dict[str, Any]:
        message = {
            "chat_id": chat_id,
            "message_id": len(self.messages) + 1,
            "text": text,
            "keyboard": keyboard,
        }
        self.messages.append(message)
        return message

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> dict[str, Any]:
        edit = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "keyboard": keyboard,
        }
        for message in self.messages:
            if message["chat_id"] == chat_id and message["message_id"] == message_id:
                message.update(edit)
                break
        self.edits.append(edit)
        return edit

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        return None

    def set_reply_keyboard(
        self,
        chat_id: int,
        keyboard: list[list[str]],
        placeholder: str | None = None,
        notice: str | None = None,
    ) -> None:
        self.reply_keyboards.append(keyboard)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    def send_temporary_message(
        self, chat_id: int, text: str, ttl_seconds: int = 5
    ) -> dict[str, Any]:
        notice = {"chat_id": chat_id, "text": text, "ttl_seconds": ttl_seconds}
        self.temporary.append(notice)
        return notice

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append(callback_id)


class FakeAI:
    provider = "gemini"
    model = "fake-pro"

    def analyze_response(self, *_args, **_kwargs) -> ResponseAnalysis:
        return ResponseAnalysis(
            task_achieved=True,
            score=0.9,
            confidence=0.95,
            positive_feedback="Смысл полностью понятен.",
            meaning_gaps=(),
            critical_corrections=(),
            optional_improvements=("Можно сделать фразу короче.",),
            natural_response="Boli mnie gardło od dwóch dni.",
            alternatives=(Alternative("Od dwóch dni boli mnie gardło.", "neutral", "Тот же смысл."),),
            grammar_chunks=(GrammarChunk("od dwóch dni", "длительность"),),
            pragmatic_note="Фраза уместна.",
            explanation="Коммуникативная задача решена.",
            provider=self.provider,
            model=self.model,
            prompt_version="test-v1",
            latency_ms=10,
            usage={"promptTokenCount": 12},
        )

    def translate(self, text: str, *_args, **_kwargs) -> dict[str, str]:
        return {"translation": f"TRANSLATED: {text}", "note": ""}

    def explain_grammar(self, *_args, **_kwargs) -> dict[str, str]:
        return {
            "meaning": "в течение двух дней",
            "explanation": "Предлог od требует родительного падежа.",
            "contrast_example": "od dnia / przez dwa dni",
            "common_error": "Не использовать *od dwa dni.",
        }

    def generate_drill_pack(self, *_args, **_kwargs) -> DrillPack:
        return DrillPack(
            title="Формы в аптеке",
            focus="Длительность и описание симптома",
            items=(
                DrillItem("choose_form", "case", "Выберите форму", "Od ___ dni", ("dwóch", "dwa", "dwoma"), "dwóch", ("dwóch",), "После od нужен родительный.", "Спросите: от скольких?", 1),
                DrillItem("fill_ending", "ending", "Вставьте окончание", "Nie mam gorączk__", (), "Nie mam gorączki", ("Nie mam gorączki",), "Родительный после nie mam.", "Форма: gorączki", 1),
                DrillItem("transform", "number", "Скажите о двух симптомах", "Boli mnie gardło", (), "Bolą mnie gardło i głowa", ("Bolą mnie gardło i głowa",), "Два подлежащих требуют bolą.", "Boli → bolą", 2),
                DrillItem("meaning_choice", "meaning", "Выберите смысл", "Od dwóch dni", ("в течение двух дней", "через два дня"), "в течение двух дней", ("в течение двух дней",), "Od отмечает начало длительности.", "Это началось раньше.", 1),
                DrillItem("free_recall", "chunk", "Ответьте фармацевту", "Od kiedy?", (), "Od dwóch dni", ("Od dwóch dni", "Od dwóch dni boli mnie gardło"), "Короткий ответ достаточен.", "Начните с Od…", 2),
            ),
        )

    def generate_toolkit_pack(self, mode, *_args, **_kwargs) -> DrillPack:
        if mode == "topic":
            return self.generate_drill_pack()
        return DrillPack(
            title="Полезные фразы",
            focus="Бытовые ситуации",
            items=tuple(
                DrillItem(
                    "flashcard",
                    "meaning",
                    "Что означает эта фраза?",
                    phrase,
                    (meaning, "Попросить документ", "Отказаться", "Уточнить цену"),
                    meaning,
                    (meaning,),
                    explanation,
                    "Вспомните ситуацию.",
                    1,
                )
                for phrase, meaning, explanation in (
                    ("Boli mnie gardło.", "У меня болит горло", "Фраза описывает симптом."),
                    ("Od dwóch dni.", "Уже два дня", "Так называют длительность."),
                    ("Czy może pani powtórzyć?", "Можете повторить?", "Просьба повторить."),
                    ("Piątek mi pasuje.", "Пятница мне подходит", "Подтверждение времени."),
                    ("Kran przecieka.", "Кран протекает", "Описание неисправности."),
                    ("Potrzebuję pomocy.", "Мне нужна помощь", "Просьба о помощи."),
                    ("Gdzie jest przystanek?", "Где остановка?", "Вопрос о дороге."),
                    ("Poproszę rachunek.", "Счёт, пожалуйста", "Просьба в кафе."),
                    ("Nie rozumiem.", "Я не понимаю", "Сообщение о непонимании."),
                    ("Ile to kosztuje?", "Сколько это стоит?", "Вопрос о цене."),
                )
            ),
        )

    def translate_with_variants(self, text, *_args, **_kwargs) -> PhraseTranslation:
        return PhraseTranslation(
            source_text=text,
            primary="Czy może pan mówić wolniej?",
            alternatives=(
                Alternative(
                    "Czy mógłby pan mówić trochę wolniej?",
                    "formal",
                    "Более вежливо.",
                ),
            ),
            usage_note="Форма pan уместна с незнакомым мужчиной.",
        )

    def evaluate_drill_answer(self, item, response, *_args, **_kwargs) -> DrillEvaluation:
        correct = response in item.accepted_answers
        return DrillEvaluation(correct, 1.0 if correct else 0.0, "Проверено.", item.correct_answer)

    def glossary_notes(self, *_args, **_kwargs) -> list[dict[str, str]]:
        return []


class CountingAI(FakeAI):
    def __init__(self) -> None:
        self.toolkit_calls = 0

    def generate_toolkit_pack(self, mode, *_args, **_kwargs) -> DrillPack:
        self.toolkit_calls += 1
        return super().generate_toolkit_pack(mode, *_args, **_kwargs)


class AppFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)
        self.settings = Settings(
            telegram_bot_token="test",
            allowed_chat_ids=None,
            data_dir=data_dir,
            health_host="127.0.0.1",
            health_port=0,
            poll_timeout=5,
            log_level="INFO",
            telegram_webhook_url="",
            telegram_webhook_secret="",
        )
        self.storage = Storage(data_dir / "test.sqlite3")
        self.telegram = FakeTelegram()
        self.bot = TutorlaingBot(self.settings, self.storage, self.telegram)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_complete_learning_loop_schedules_review(self) -> None:
        chat_id = 100
        self.bot.start(chat_id, "Igor")
        self.bot.handle_callback(chat_id, "Igor", "cb1", "consent:accept")
        self.bot.begin_scenario(chat_id, "pharmacy")
        session_id = self.storage.get_user(chat_id)["current_session"]

        self.bot.handle_text(
            chat_id,
            "Igor",
            "Dzień dobry. Boli mnie gardło i potrzebuję czegoś na ból.",
        )
        self.bot.handle_callback(chat_id, "Igor", "next1", "assignment:next")
        self.bot.handle_text(
            chat_id, "Igor", "Od dwóch dni. Nie mam gorączki."
        )
        self.bot.handle_callback(chat_id, "Igor", "next2", "assignment:next")
        self.assertEqual("practice", self.storage.get_user(chat_id)["stage"])

        self.bot.handle_text(
            chat_id,
            "Igor",
            "Boli mnie gardło. Szukam czegoś na ból.",
        )
        user = self.storage.get_user(chat_id)
        self.assertEqual("idle", user["stage"])
        self.assertEqual("completed", self.storage.session(session_id)["status"])
        self.assertEqual(1, len(self.storage.pending_reviews(chat_id, include_future=True)))

    def test_delete_callback_removes_user(self) -> None:
        self.storage.ensure_user(7, "Test")
        self.bot.handle_callback(7, "Test", "cb", "delete:confirm")
        with self.assertRaises(KeyError):
            self.storage.get_user(7)

    def test_allowlist_blocks_unknown_chat(self) -> None:
        settings = Settings(
            telegram_bot_token="test",
            allowed_chat_ids=frozenset({1}),
            data_dir=Path(self.temp_dir.name),
            health_host="127.0.0.1",
            health_port=0,
            poll_timeout=5,
            log_level="INFO",
            telegram_webhook_url="",
            telegram_webhook_secret="",
        )
        bot = TutorlaingBot(settings, self.storage, self.telegram)
        bot.handle_text(2, "Unknown", "/start")
        self.assertIn("закрытая alpha", self.telegram.messages[-1]["text"])

    def test_existing_consent_requires_ai_disclosure_upgrade(self) -> None:
        self.storage.ensure_user(8, "Existing")
        self.storage.accept_consent(8, 2)
        self.bot.start(8, "Existing")
        self.assertIn("OpenAI", self.telegram.messages[-1]["text"])
        self.assertIn("Google Gemini", self.telegram.messages[-1]["text"])

    def test_language_setting_is_persisted(self) -> None:
        self.storage.ensure_user(9, "Learner")
        self.storage.accept_consent(9, CONSENT_VERSION)
        self.bot.handle_callback(9, "Learner", "cb", "settings:set:instruction:uk")
        self.assertEqual("uk", self.storage.get_user(9)["instruction_language"])

    def test_polish_explanations_and_english_target_are_available(self) -> None:
        chat_id = 10
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.bot.handle_callback(
            chat_id, "Learner", "cb1", "settings:set:instruction:pl"
        )
        self.bot.handle_callback(
            chat_id, "Learner", "cb2", "settings:set:target:en"
        )
        user = self.storage.get_user(chat_id)
        self.assertEqual("pl", user["instruction_language"])
        self.assertEqual("en", user["target_language"])
        self.bot.begin_scenario(chat_id, "pharmacy")
        self.assertIn("AT THE PHARMACY", self.telegram.messages[-1]["text"])
        self.assertIn("How can I help you?", self.telegram.messages[-1]["text"])

    def test_ai_feedback_variants_and_grammar_are_available(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 11
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "cb1", "consent:accept")
        bot.begin_scenario(chat_id, "pharmacy")
        bot.handle_text(chat_id, "Learner", "Boli mnie gardło.")

        feedback = next(
            message for message in reversed(self.telegram.messages) if "Естественнее" in message["text"]
        )
        self.assertIn("Boli mnie gardło od dwóch dni", feedback["text"])
        analysis = self.storage.latest_ai_analysis(chat_id)
        self.assertIsNotNone(analysis)

        bot.show_variants(chat_id, int(analysis["id"]))
        self.assertIn("Od dwóch dni boli mnie gardło", self.telegram.messages[-1]["text"])
        bot.explain_grammar(chat_id, int(analysis["id"]), "0")
        self.assertIn("родительного падежа", self.telegram.messages[-1]["text"])

    def test_contextual_drill_can_answer_choice_and_free_recall(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 12
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "cb1", "consent:accept")
        bot.begin_scenario(chat_id, "pharmacy")
        bot.handle_text(chat_id, "Learner", "Boli mnie gardło.")
        bot.handle_callback(chat_id, "Learner", "next1", "assignment:next")
        bot.handle_text(chat_id, "Learner", "Od dwóch dni. Nie mam gorączki.")
        bot.handle_callback(chat_id, "Learner", "next2", "assignment:next")
        bot.handle_text(chat_id, "Learner", "Boli mnie gardło od dwóch dni.")
        bot.start_drill(chat_id)
        session = self.storage.active_drill(chat_id)
        self.assertIsNotNone(session)
        first = self.storage.drill_item(session["id"], 0)
        bot.answer_drill_choice(chat_id, int(first["id"]), 0)
        self.assertIn("ПОЛУЧИЛОСЬ", self.telegram.messages[-1]["text"])
        bot.advance_drill(chat_id, session["id"])
        second = self.storage.drill_item(session["id"], 1)
        bot.answer_drill(chat_id, int(second["id"]), "Nie mam gorączki")
        self.assertIn("ПОЛУЧИЛОСЬ", self.telegram.messages[-1]["text"])

    def test_adaptive_drill_uses_history_fallback_when_all_ai_routes_fail(self) -> None:
        class BrokenAI(FakeAI):
            def generate_drill_pack(self, *_args, **_kwargs) -> DrillPack:
                raise AIError("all providers unavailable")

        chat_id = 44
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=BrokenAI())
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")
        analysis = FakeAI().analyze_response()
        self.storage.add_ai_analysis(
            chat_id=chat_id,
            scenario_id="pharmacy",
            step_index=0,
            operation="response_analysis",
            target_language="pl",
            source_text="Ja potrzebuje pomoc",
            result=analysis.to_dict(),
            provider=analysis.provider,
            model=analysis.model,
            prompt_version=analysis.prompt_version,
            latency_ms=analysis.latency_ms,
            usage=analysis.usage,
        )

        bot.start_drill(chat_id)

        session = self.storage.active_drill(chat_id)
        self.assertIsNotNone(session)
        self.assertEqual(8, session["total_items"])
        first = self.storage.drill_item(str(session["id"]), 0)
        with self.storage._lock:
            source = self.storage._connection.execute(
                "SELECT source FROM exercise_bank WHERE id = ?",
                (first["exercise_id"],),
            ).fetchone()["source"]
        self.assertEqual("recovery", source)
        self.assertNotIn("Не удалось собрать задания", self.telegram.messages[-1]["text"])

    def test_reminder_mode_is_configurable(self) -> None:
        chat_id = 13
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.bot.handle_callback(chat_id, "Learner", "cb", "reminder:set:aggressive")
        user = self.storage.get_user(chat_id)
        self.assertEqual("aggressive", user["reminder_mode"])
        self.assertIsNotNone(user["reminder_next_at"])

    def test_reminder_settings_include_a_manual_delivery_check(self) -> None:
        chat_id = 34
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        self.bot.show_reminders(chat_id)

        callbacks = [
            button["callback_data"]
            for row in self.telegram.messages[-1]["keyboard"]
            for button in row
        ]
        self.assertIn("reminder:test", callbacks)

    def test_reengagement_card_starts_one_task_only_after_user_action(self) -> None:
        chat_id = 403
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        self.bot.send_reengagement_reminder(chat_id, "normal", 8)

        message = self.telegram.messages[-1]
        self.assertIn("прогресс сохранён", message["text"].lower())
        self.assertEqual(
            "reengagement:continue", message["keyboard"][0][0]["callback_data"]
        )
        before = len(self.telegram.messages)
        self.bot.handle_callback(
            chat_id, "Learner", "continue", "reengagement:continue"
        )
        self.assertEqual(before + 1, len(self.telegram.messages))
        self.assertIsNotNone(self.storage.get_user(chat_id)["last_interaction_at"])

    def test_home_installs_persistent_bottom_navigation(self) -> None:
        chat_id = 35
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        self.bot.home(chat_id)

        self.assertEqual(
            [
                ["⌂ Главное меню"],
                ["▶ Учиться", "🧰 Инструменты"],
                ["📍 Прогресс", "⚙ Настройки"],
            ],
            self.telegram.reply_keyboards[-1],
        )

    def test_bottom_navigation_install_is_versioned_and_language_aware(self) -> None:
        chat_id = 36
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        self.bot.home(chat_id)
        self.bot.home(chat_id)
        self.assertEqual(1, len(self.telegram.reply_keyboards))

        self.storage.set_language(chat_id, "instruction_language", "pl")
        self.bot.menu.refresh_navigation(chat_id)
        self.assertEqual(2, len(self.telegram.reply_keyboards))
        self.assertEqual(["⌂ Menu główne"], self.telegram.reply_keyboards[-1][0])

    def test_bottom_navigation_opens_tools_and_cancels_phrase_input(self) -> None:
        chat_id = 36
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.storage.set_user_state(chat_id, toolkit_input_mode="to_target")

        self.bot.handle_text(chat_id, "Learner", "🧰 Инструменты")

        self.assertIsNone(self.storage.get_user(chat_id)["toolkit_input_mode"])
        self.assertIn("МАСТЕРСКАЯ", self.telegram.messages[-1]["text"])

    def test_scenario_scaffolding_changes_with_learner_level(self) -> None:
        beginner = 37
        advanced = 38
        for chat_id, level in ((beginner, "A1"), (advanced, "B2")):
            self.storage.ensure_user(chat_id, "Learner")
            self.storage.accept_consent(chat_id, CONSENT_VERSION)
            self.storage.set_learner_level(chat_id, level)
            self.bot.begin_scenario(chat_id, "pharmacy")

        beginner_text = next(
            message["text"]
            for message in reversed(self.telegram.messages)
            if message["chat_id"] == beginner
        )
        advanced_text = next(
            message["text"]
            for message in reversed(self.telegram.messages)
            if message["chat_id"] == advanced
        )
        self.assertIn("Уровень: A1", beginner_text)
        self.assertIn("Опора:", beginner_text)
        self.assertIn("Уровень: B2", advanced_text)
        self.assertIn("без готовой опоры", advanced_text)
        self.assertNotEqual(beginner_text, advanced_text)

    def test_long_drill_options_are_full_in_message_and_buttons_are_compact(self) -> None:
        chat_id = 39
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        long_option = (
            "Очень длинный вариант ответа, который должен полностью отображаться "
            "в тексте задания и переноситься клиентом Telegram на несколько строк"
        )
        item = {
            "type": "meaning_choice",
            "skill": "meaning",
            "prompt": "Выберите точное значение",
            "context": "Przykładowe zdanie",
            "options": [long_option, "Вариант B", "Вариант C", "Вариант D"],
            "correct_answer": long_option,
            "accepted_answers": [long_option],
            "explanation": "Контекст",
            "hint": "Смысл",
            "difficulty": 2,
        }
        drill_id = self.storage.start_drill(
            chat_id, None, "Long", "UI", [item]
        )

        self.bot.send_drill_item(chat_id, drill_id)

        message = self.telegram.messages[-1]
        self.assertIn(f"A. {long_option}", message["text"])
        self.assertEqual(
            ["A", "B", "C", "D"],
            [button["text"] for button in message["keyboard"][0]],
        )

    def test_stale_drill_action_uses_temporary_notice(self) -> None:
        chat_id = 40
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        self.bot.answer_drill_choice(chat_id, 999, 0)

        self.assertEqual("Это задание уже закрыто.", self.telegram.temporary[-1]["text"])

    def test_scheduled_reminder_advances_answered_drill_one_item(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 14
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "cb1", "consent:accept")
        bot.begin_scenario(chat_id, "pharmacy")
        bot.handle_text(chat_id, "Learner", "Boli mnie gardło.")
        bot.handle_callback(chat_id, "Learner", "next1", "assignment:next")
        bot.handle_text(chat_id, "Learner", "Od dwóch dni. Nie mam gorączki.")
        bot.handle_callback(chat_id, "Learner", "next2", "assignment:next")
        bot.handle_text(chat_id, "Learner", "Boli mnie gardło od dwóch dni.")
        bot.set_reminder_mode(chat_id, "normal")
        bot.start_drill(chat_id)
        session = self.storage.active_drill(chat_id)
        first = self.storage.drill_item(session["id"], 0)
        bot.answer_drill_choice(chat_id, int(first["id"]), 0)
        before = len(self.telegram.messages)

        bot.send_scheduled_reminder(chat_id, "normal")

        self.assertEqual(before + 1, len(self.telegram.messages))
        self.assertIn("2/5", self.telegram.messages[-1]["text"])
        self.assertEqual(1, self.storage.active_drill(chat_id)["current_index"])

    def test_normal_reminder_starts_exactly_one_assignment(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 15
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "cb1", "consent:accept")
        bot.begin_scenario(chat_id, "pharmacy")
        bot.handle_text(chat_id, "Learner", "Boli mnie gardło.")
        bot.handle_callback(chat_id, "Learner", "next1", "assignment:next")
        bot.handle_text(chat_id, "Learner", "Od dwóch dni. Nie mam gorączki.")
        bot.handle_callback(chat_id, "Learner", "next2", "assignment:next")
        bot.handle_text(chat_id, "Learner", "Boli mnie gardło od dwóch dni.")
        before = len(self.telegram.messages)

        bot.send_scheduled_reminder(chat_id, "normal")

        self.assertEqual(before + 1, len(self.telegram.messages))
        self.assertIn("1/5", self.telegram.messages[-1]["text"])

    def test_scenario_waits_for_button_or_scheduled_next_assignment(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 16
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "cb1", "consent:accept")
        bot.set_reminder_mode(chat_id, "normal")
        bot.begin_scenario(chat_id, "pharmacy")
        bot.handle_text(chat_id, "Learner", "Boli mnie gardło.")
        self.assertEqual("waiting", self.storage.get_user(chat_id)["stage"])
        self.assertEqual(
            "Следующее задание →",
            self.telegram.messages[-1]["keyboard"][0][0]["text"],
        )
        before = len(self.telegram.messages)

        bot.send_scheduled_reminder(chat_id, "normal")

        self.assertEqual(before + 1, len(self.telegram.messages))
        self.assertIn("2/2", self.telegram.messages[-1]["text"])
        self.assertEqual("scenario", self.storage.get_user(chat_id)["stage"])

    def test_scheduled_reminder_resends_only_the_current_scenario_task(self) -> None:
        chat_id = 33
        self.bot.start(chat_id, "Learner")
        self.bot.handle_callback(chat_id, "Learner", "cb1", "consent:accept")
        self.bot.begin_scenario(chat_id, "pharmacy")
        before = len(self.telegram.messages)

        self.bot.send_scheduled_reminder(chat_id, "normal")

        self.assertEqual(before + 1, len(self.telegram.messages))
        self.assertIn("1/2", self.telegram.messages[-1]["text"])
        self.assertEqual("scenario", self.storage.get_user(chat_id)["stage"])

    def test_immediate_learning_flow_edits_one_workspace_message(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 17
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")
        bot.begin_scenario(chat_id, "pharmacy")
        self.assertEqual(1, len(self.telegram.messages))

        bot.handle_text(chat_id, "Learner", "Boli mnie gardło.")
        self.assertEqual(2, len(self.telegram.messages))
        self.assertGreaterEqual(len(self.telegram.edits), 2)
        self.assertEqual("Следующее задание →", self.telegram.messages[-1]["keyboard"][0][0]["text"])

        bot.handle_callback(chat_id, "Learner", "next", "assignment:next")
        self.assertEqual(2, len(self.telegram.messages))
        self.assertIn("2/2", self.telegram.messages[-1]["text"])

    def test_progress_and_interface_follow_instruction_language(self) -> None:
        chat_id = 18
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.storage.set_language(chat_id, "instruction_language", "pl")
        self.storage.set_learner_level(chat_id, "B1")

        self.bot.show_progress(chat_id)

        self.assertIn("Bieżący poziom roboczy: B1", self.telegram.messages[-1]["text"])
        self.assertIn("NAJBLIŻSZY PLAN", self.telegram.messages[-1]["text"])
        self.bot.show_reminders(chat_id)
        self.assertIn("PRZYPOMNIENIA", self.telegram.messages[-1]["text"])

    def test_two_levels_harder_term_can_get_translation_footnote(self) -> None:
        class GlossaryAI(FakeAI):
            def glossary_notes(self, *_args, **_kwargs) -> list[dict[str, str]]:
                return [{"term": "pomóc", "translation": "помочь", "cefr": "C1"}]

        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=GlossaryAI())
        chat_id = 19
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")
        self.storage.set_learner_level(chat_id, "B1")

        bot.begin_scenario(chat_id, "pharmacy")

        self.assertIn("СНОСКА", self.telegram.messages[-1]["text"])
        self.assertIn("pomóc — помочь (C1)", self.telegram.messages[-1]["text"])

    def test_toolkit_flashcards_have_options_and_forgot_action(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 20
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")

        bot.handle_callback(chat_id, "Learner", "tools", "toolkit:start:cards")

        session = self.storage.active_drill(chat_id)
        self.assertEqual("toolkit_cards", session["mode"])
        self.assertIn("КАРТОЧКИ", self.telegram.messages[-1]["text"])
        buttons = [
            button["text"]
            for row in self.telegram.messages[-1]["keyboard"]
            for button in row
        ]
        self.assertIn("Не помню", buttons)
        self.assertEqual(["A", "B", "C", "D"], buttons[:4])
        self.assertIn("A. ", self.telegram.messages[-1]["text"])
        self.assertIn("У меня болит горло", self.telegram.messages[-1]["text"])

        first = self.storage.drill_item(str(session["id"]), 0)
        bot.handle_callback(
            chat_id, "Learner", "forgot", f"drill:skip:{first['id']}"
        )
        self.assertIn("ПОКАЖУ ОТВЕТ", self.telegram.messages[-1]["text"])
        self.assertIn("У меня болит горло", self.telegram.messages[-1]["text"])

    def test_toolkit_flashcards_work_without_an_ai_provider(self) -> None:
        chat_id = 31
        self.bot.start(chat_id, "Learner")
        self.bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")

        self.bot.handle_callback(chat_id, "Learner", "cards", "toolkit:start:cards")

        session = self.storage.active_drill(chat_id)
        self.assertIsNotNone(session)
        self.assertEqual("toolkit_cards", session["mode"])
        self.assertEqual(10, session["total_items"])

    def test_toolkit_reuses_a_valid_bank_pack_without_a_second_ai_call(self) -> None:
        ai = CountingAI()
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=ai)
        chat_id = 33
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")

        bot.handle_callback(chat_id, "Learner", "cards-1", "toolkit:start:cards")
        bot.stop_drill(chat_id)
        bot.handle_callback(chat_id, "Learner", "cards-2", "toolkit:start:cards")

        self.assertEqual(1, ai.toolkit_calls)
        session = self.storage.active_drill(chat_id)
        self.assertIsNotNone(session)
        self.assertTrue(
            all(
                self.storage.drill_item(str(session["id"]), index)["exercise_id"]
                for index in range(int(session["total_items"]))
            )
        )

    def test_toolkit_card_material_prioritizes_a_failed_scenario_phrase(self) -> None:
        chat_id = 32
        self.storage.ensure_user(chat_id, "Learner")
        session_id = self.storage.start_session(chat_id, "pharmacy")
        self.storage.add_response(
            session_id, 1, "scenario", "Nie wiem", 0.1, (0,)
        )
        user = self.storage.get_user(chat_id)

        material = self.bot.toolkit._pack_material(user, "cards", None)

        self.assertEqual(10, len(material["phrases"]))
        failed = [
            item
            for item in material["phrases"]
            if item.get("scenario_id") == "pharmacy" and item.get("step_index") == 1
        ]
        self.assertEqual(1, len(failed))
        self.assertEqual("problem", failed[0]["priority"])
        self.assertGreaterEqual(material["selection_policy"]["problem_items"], 1)

    def test_phrase_tool_translates_both_directions_with_variants(self) -> None:
        class RecordingAI(FakeAI):
            def __init__(self) -> None:
                self.directions = []

            def translate_with_variants(
                self, text, source_language, target_language, instruction_language
            ) -> PhraseTranslation:
                self.directions.append((source_language, target_language))
                return super().translate_with_variants(text)

        ai = RecordingAI()
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=ai)
        chat_id = 21
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")

        bot.handle_callback(
            chat_id, "Learner", "translate", "toolkit:translate:to_target"
        )
        self.assertEqual("idle", self.storage.get_user(chat_id)["stage"])
        self.assertEqual(
            "to_target", self.storage.get_user(chat_id)["toolkit_input_mode"]
        )
        before = len(self.telegram.messages)
        bot.handle_text(chat_id, "Learner", "Можете говорить медленнее?")

        self.assertEqual(before + 1, len(self.telegram.messages))
        self.assertEqual("idle", self.storage.get_user(chat_id)["stage"])
        self.assertIn("Czy może pan mówić wolniej?", self.telegram.messages[-1]["text"])
        self.assertIn("Более вежливо", self.telegram.messages[-1]["text"])
        analysis = self.storage.get_ai_analysis(1, chat_id)
        self.assertEqual("phrase_translation", analysis["operation"])
        buttons = [
            button["callback_data"]
            for row in self.telegram.messages[-1]["keyboard"]
            for button in row
        ]
        self.assertIn("toolkit:translate:from_target", buttons)

        bot.handle_callback(
            chat_id, "Learner", "reverse", "toolkit:translate:from_target"
        )
        bot.handle_text(chat_id, "Learner", "Czy może pan mówić wolniej?")
        self.assertEqual([("ru", "pl"), ("pl", "ru")], ai.directions)

    def test_topic_tool_creates_varied_drill_without_prior_analysis(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 22
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")

        bot.handle_callback(chat_id, "Learner", "topic", "toolkit:topic:pharmacy")

        session = self.storage.active_drill(chat_id)
        self.assertEqual("toolkit_topic", session["mode"])
        item_types = {
            self.storage.drill_item(str(session["id"]), index)["item_type"]
            for index in range(int(session["total_items"]))
        }
        self.assertGreaterEqual(len(item_types), 3)
        self.assertIn("СЦЕНАРИЙ", self.telegram.messages[-1]["text"])

    def test_phrase_tool_does_not_replace_an_active_scenario(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 28
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")
        bot.begin_scenario(chat_id, "pharmacy")
        before = self.storage.get_user(chat_id)
        session_id = str(before["current_session"])

        bot.handle_callback(
            chat_id, "Learner", "translate", "toolkit:translate:to_target"
        )
        during = self.storage.get_user(chat_id)
        self.assertEqual("scenario", during["stage"])
        self.assertEqual(session_id, during["current_session"])
        self.assertEqual("to_target", during["toolkit_input_mode"])

        bot.handle_text(chat_id, "Learner", "Можете говорить медленнее?")

        after = self.storage.get_user(chat_id)
        self.assertEqual("scenario", after["stage"])
        self.assertEqual(session_id, after["current_session"])
        self.assertIsNone(after["toolkit_input_mode"])
        self.assertEqual(0, self.storage.response_count(session_id, "scenario"))
        callbacks = [
            button["callback_data"]
            for row in self.telegram.messages[-1]["keyboard"]
            for button in row
        ]
        self.assertIn("task:resume", callbacks)

    def test_toolkit_cards_restore_an_active_scenario_after_completion(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 29
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")
        bot.begin_scenario(chat_id, "pharmacy")
        scenario_session = str(self.storage.get_user(chat_id)["current_session"])

        bot.handle_callback(chat_id, "Learner", "cards", "toolkit:start:cards")

        overlay = self.storage.get_user(chat_id)
        self.assertEqual("drill", overlay["stage"])
        self.assertIsNotNone(overlay["suspended_activity_json"])
        self.assertEqual("active", self.storage.session(scenario_session)["status"])

        drill_id = str(overlay["current_drill"])
        for _ in range(10):
            session = self.storage.drill_session(drill_id, chat_id)
            item = self.storage.drill_item(drill_id, int(session["current_index"]))
            bot.handle_callback(
                chat_id, "Learner", "skip", f"drill:skip:{item['id']}"
            )
            bot.handle_callback(
                chat_id, "Learner", "next", f"drill:next:{drill_id}"
            )

        restored = self.storage.get_user(chat_id)
        self.assertEqual("scenario", restored["stage"])
        self.assertEqual(scenario_session, restored["current_session"])
        self.assertIsNone(restored["suspended_activity_json"])
        self.assertEqual("active", self.storage.session(scenario_session)["status"])
        self.assertEqual(
            "task:resume", self.telegram.messages[-1]["keyboard"][0][0]["callback_data"]
        )
        completion_callbacks = [
            button["callback_data"]
            for row in self.telegram.messages[-1]["keyboard"]
            for button in row
        ]
        self.assertIn("toolkit:start:cards", completion_callbacks)

    def test_toolkit_drill_can_temporarily_overlay_another_drill(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 30
        bot.start(chat_id, "Learner")
        bot.handle_callback(chat_id, "Learner", "consent", "consent:accept")
        pack = FakeAI().generate_drill_pack()
        original_drill = self.storage.start_drill(
            chat_id,
            None,
            pack.title,
            pack.focus,
            [item.to_dict() for item in pack.items],
        )

        bot.handle_callback(chat_id, "Learner", "cards", "toolkit:start:cards")

        overlay_drill = str(self.storage.get_user(chat_id)["current_drill"])
        self.assertNotEqual(original_drill, overlay_drill)
        self.assertEqual("active", self.storage.drill_session(original_drill)["status"])
        self.assertEqual("toolkit_cards", self.storage.drill_session(overlay_drill)["mode"])

        bot.handle_callback(chat_id, "Learner", "stop", "drill:stop")

        restored = self.storage.get_user(chat_id)
        self.assertEqual("drill", restored["stage"])
        self.assertEqual(original_drill, restored["current_drill"])
        self.assertEqual(original_drill, self.storage.active_drill(chat_id)["id"])
        self.assertEqual(
            "drill:resume", self.telegram.messages[-1]["keyboard"][0][0]["callback_data"]
        )

    def test_home_has_one_recommended_action_without_duplicate_reviews(self) -> None:
        chat_id = 23
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        self.bot.home(chat_id)

        callbacks = [
            button["callback_data"]
            for row in self.telegram.messages[-1]["keyboard"]
            for button in row
        ]
        self.assertTrue(callbacks[0].startswith("scenario:"))
        self.assertEqual(1, callbacks.count("reviews:list"))

    def test_home_resumes_active_task_as_the_primary_action(self) -> None:
        chat_id = 24
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.bot.begin_scenario(chat_id, "pharmacy")

        self.bot.home(chat_id)

        keyboard = self.telegram.messages[-1]["keyboard"]
        self.assertEqual("task:resume", keyboard[0][0]["callback_data"])

    def test_settings_use_sections_and_explicit_parent_navigation(self) -> None:
        chat_id = 25
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        self.bot.show_settings(chat_id)
        callbacks = [row[0]["callback_data"] for row in self.telegram.messages[-1]["keyboard"]]
        self.assertEqual(
            ["settings:languages", "reminders", "privacy:settings", "home"],
            callbacks,
        )

        self.bot.handle_callback(chat_id, "Learner", "languages", "settings:languages")
        callbacks = [row[0]["callback_data"] for row in self.telegram.messages[-1]["keyboard"]]
        self.assertEqual("settings", callbacks[-1])

        self.bot.handle_callback(chat_id, "Learner", "privacy", "privacy:settings")
        self.assertEqual(
            "settings", self.telegram.messages[-1]["keyboard"][-1][0]["callback_data"]
        )

    def test_difficulty_proposal_requires_callback_and_preserves_profile(self) -> None:
        chat_id = 250
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.storage.set_learner_level(chat_id, "B1")
        proposal = self.storage.create_difficulty_proposal(
            chat_id,
            "pl",
            1,
            "mixed_production",
            {
                "average_score": 0.96,
                "severe_rate": 0.0,
                "hint_rate": 0.0,
                "production_attempts": 12,
                "distinct_days": 2,
            },
        )

        self.bot.handle_callback(
            chat_id,
            "Learner",
            "difficulty",
            f"difficulty:accept:{proposal['id']}",
        )

        user = self.storage.get_user(chat_id)
        self.assertEqual("B1", user["learner_level"])
        self.assertEqual(1, user["practice_difficulty_offset"])
        self.assertIn("B2", self.telegram.messages[-1]["text"])

    def test_finish_confirmation_preserves_active_task_until_confirmed(self) -> None:
        chat_id = 26
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.bot.begin_scenario(chat_id, "pharmacy")

        self.bot.handle_callback(chat_id, "Learner", "stop", "cancel:confirm")

        self.assertEqual("scenario", self.storage.get_user(chat_id)["stage"])
        callbacks = [
            button["callback_data"]
            for row in self.telegram.messages[-1]["keyboard"]
            for button in row
        ]
        self.assertEqual(["task:resume", "cancel"], callbacks)

        self.bot.handle_callback(chat_id, "Learner", "confirm", "cancel")
        self.assertEqual("idle", self.storage.get_user(chat_id)["stage"])

    def test_empty_review_screen_always_offers_home(self) -> None:
        chat_id = 27
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        self.bot.show_reviews(chat_id)

        callbacks = [
            button["callback_data"]
            for row in self.telegram.messages[-1]["keyboard"]
            for button in row
        ]
        self.assertIn("home", callbacks)

    def test_bottom_navigation_opens_a_fresh_visible_surface(self) -> None:
        chat_id = 280
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.bot.home(chat_id)
        old_workspace = int(self.storage.get_user(chat_id)["workspace_message_id"])

        self.bot.handle_text(chat_id, "Learner", "📍 Прогресс", message_id=991)

        new_workspace = int(self.storage.get_user(chat_id)["workspace_message_id"])
        self.assertNotEqual(old_workspace, new_workspace)
        self.assertIn((chat_id, 991), self.telegram.deleted)
        self.assertIn("ПРОГРЕСС", self.telegram.messages[-1]["text"])
        self.assertEqual(["⌂ Главное меню"], self.telegram.reply_keyboards[-1][0])

    def test_inline_callback_edits_the_card_that_was_clicked(self) -> None:
        chat_id = 281
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.bot.home(chat_id)
        clicked_message_id = int(self.storage.get_user(chat_id)["workspace_message_id"])
        self.bot.workspace.start_new_surface(chat_id)
        self.bot.show_progress(chat_id)

        self.bot.handle_callback(
            chat_id,
            "Learner",
            "home",
            "home",
            message_id=clicked_message_id,
        )

        self.assertEqual(clicked_message_id, self.telegram.edits[-1]["message_id"])

    def test_free_text_outside_task_offers_actions_and_can_be_checked(self) -> None:
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, ai=FakeAI())
        chat_id = 282
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)

        bot.handle_text(chat_id, "Learner", "Ja potrzebuje pomocy.", message_id=992)

        message = self.telegram.messages[-1]
        self.assertIn("ЧТО СДЕЛАТЬ С ФРАЗОЙ", message["text"])
        callbacks = [
            button["callback_data"]
            for row in message["keyboard"]
            for button in row
        ]
        check_callback = next(value for value in callbacks if value.startswith("text:check:"))
        self.assertTrue(any(value.endswith(":to_target") for value in callbacks))
        self.assertTrue(any(value.endswith(":from_target") for value in callbacks))

        bot.handle_callback(
            chat_id,
            "Learner",
            "check",
            check_callback,
            message_id=int(message["message_id"]),
        )

        analysis = self.storage.latest_ai_analysis(chat_id, "pl")
        self.assertEqual("standalone_phrase", analysis["operation"])
        self.assertIn("Естественнее", self.telegram.edits[-1]["text"])


if __name__ == "__main__":
    unittest.main()
