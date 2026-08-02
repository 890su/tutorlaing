import tempfile
import unittest
from pathlib import Path
from typing import Any

from tutorlaing.ai import (
    Alternative,
    DrillEvaluation,
    DrillItem,
    DrillPack,
    GrammarChunk,
    ResponseAnalysis,
)
from tutorlaing.app import TutorlaingBot
from tutorlaing.config import Settings
from tutorlaing.storage import Storage


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.callbacks: list[str] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.messages.append({"chat_id": chat_id, "text": text, "keyboard": keyboard})

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

    def evaluate_drill_answer(self, item, response, *_args, **_kwargs) -> DrillEvaluation:
        correct = response in item.accepted_answers
        return DrillEvaluation(correct, 1.0 if correct else 0.0, "Проверено.", item.correct_answer)


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
        self.storage.accept_consent(8, 1)
        self.bot.start(8, "Existing")
        self.assertIn("Google Gemini", self.telegram.messages[-1]["text"])

    def test_language_setting_is_persisted(self) -> None:
        self.storage.ensure_user(9, "Learner")
        self.storage.accept_consent(9, 2)
        self.bot.handle_callback(9, "Learner", "cb", "settings:set:instruction:uk")
        self.assertEqual("uk", self.storage.get_user(9)["instruction_language"])

    def test_polish_explanations_and_english_target_are_available(self) -> None:
        chat_id = 10
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, 2)
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
        self.assertIn("AT THE PHARMACY", self.telegram.messages[-2]["text"])
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

    def test_reminder_mode_is_configurable(self) -> None:
        chat_id = 13
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, 2)
        self.bot.handle_callback(chat_id, "Learner", "cb", "reminder:set:aggressive")
        user = self.storage.get_user(chat_id)
        self.assertEqual("aggressive", user["reminder_mode"])
        self.assertIsNotNone(user["reminder_next_at"])

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


if __name__ == "__main__":
    unittest.main()
