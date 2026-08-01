import tempfile
import unittest
from pathlib import Path
from typing import Any

from tutorlaing.ai import Alternative, GrammarChunk, ResponseAnalysis
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
        self.bot.handle_text(
            chat_id, "Igor", "Od dwóch dni. Nie mam gorączki."
        )
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


if __name__ == "__main__":
    unittest.main()
