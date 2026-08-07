import tempfile
import unittest
from pathlib import Path
from typing import Any

from tutorlaing.ai import ResponseAnalysis
from tutorlaing.app import TutorlaingBot
from tutorlaing.config import Settings
from tutorlaing.quest_content import load_quests
from tutorlaing.quest_engine import answer_free
from tutorlaing.storage import Storage


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.notices: list[str] = []

    def send_message(self, chat_id, text, keyboard=None):
        message = {
            "chat_id": chat_id,
            "message_id": len(self.messages) + 1,
            "text": text,
            "keyboard": keyboard,
        }
        self.messages.append(message)
        return message

    def edit_message(self, chat_id, message_id, text, keyboard=None):
        result = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "keyboard": keyboard,
        }
        self.edits.append(result)
        return result

    def send_chat_action(self, chat_id, action="typing"):
        return None

    def set_reply_keyboard(self, chat_id, keyboard, placeholder=None, notice=None):
        return None

    def delete_message(self, chat_id, message_id):
        return None

    def send_temporary_message(self, chat_id, text, ttl_seconds=5):
        self.notices.append(text)
        return {"chat_id": chat_id, "text": text}

    def answer_callback(self, callback_id, text=""):
        return None


class QuestContentTests(unittest.TestCase):
    def test_catalog_has_four_branching_migrant_quests(self) -> None:
        quests = load_quests("pl")
        self.assertEqual(
            {"urzad_documents", "clinic_visit", "landlord_repair", "work_schedule"},
            set(quests),
        )
        for quest in quests.values():
            endings = {node.ending for node in quest.nodes.values() if node.ending}
            self.assertIn("success", endings)
            self.assertGreaterEqual(len(endings), 2)

    def test_free_answer_accepts_natural_variant_and_routes_weak_answer(self) -> None:
        node = load_quests()["urzad_documents"].nodes["u3"]
        success = answer_free(node, "Do jakiego dnia mogę donieść dokument?", {})
        retry = answer_free(node, "Nie rozumiem", {})
        self.assertEqual("u4", success.next_node)
        self.assertEqual("good", success.outcome)
        self.assertEqual("u3_help", retry.next_node)
        self.assertEqual("problem", retry.outcome)

    def test_legacy_choice_node_becomes_free_text_with_preserved_effects(self) -> None:
        node = load_quests()["urzad_documents"].nodes["u1"]
        source = {"existing": "fact"}
        self.assertEqual("free", node.mode)
        transition = answer_free(node, node.reference_answer, source)
        self.assertEqual("u2", transition.next_node)
        self.assertEqual("fact", transition.state["existing"])
        self.assertEqual("formal", transition.state["register"])
        self.assertNotEqual(source, transition.state)

    def test_all_callback_payloads_fit_telegram_limit(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000000"
        for quest in load_quests().values():
            for node in quest.nodes.values():
                if node.mode == "ending":
                    continue
                self.assertEqual("free", node.mode)
                for action in ("hint", "next"):
                    payload = f"quest:{action}:{session_id}:{node.id}"
                    self.assertLessEqual(len(payload.encode("utf-8")), 64, payload)


class QuestStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "test.sqlite3")
        self.storage.ensure_user(42, "Igor")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_transition_is_atomic_and_quest_survives_tool_overlay(self) -> None:
        quest_id = self.storage.start_quest(42, "urzad_documents", "u1")
        advanced = self.storage.advance_quest(
            quest_id,
            42,
            "u1",
            "u2",
            input_kind="choice",
            user_answer="answer",
            choice_id="a",
            score=1.0,
            outcome="good",
            state={"deadline": "7 dni"},
        )
        stale = self.storage.advance_quest(
            quest_id,
            42,
            "u1",
            "u2",
            input_kind="choice",
            user_answer="duplicate",
            choice_id="a",
            score=1.0,
            outcome="good",
            state={},
        )
        self.assertTrue(advanced)
        self.assertFalse(stale)
        self.storage.start_session(42, "pharmacy", preserve_active=True)
        self.assertIsNone(self.storage.get_user(42)["current_quest"])
        self.storage.resume_quest_session(42, quest_id)
        user = self.storage.get_user(42)
        self.assertEqual("quest", user["stage"])
        self.assertEqual(quest_id, user["current_quest"])
        self.assertEqual("u2", self.storage.quest_session(quest_id, 42)["current_node"])


class QuestAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)
        settings = Settings(
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
        self.bot = TutorlaingBot(settings, self.storage, self.telegram)
        self.bot.start(7, "Igor")
        self.bot.handle_callback(7, "Igor", "consent", "consent:accept")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_happy_path_completes_and_records_success(self) -> None:
        self.bot.begin_quest(7, "urzad_documents")
        session_id = str(self.storage.get_user(7)["current_quest"])
        quest = load_quests()["urzad_documents"]
        for _ in range(12):
            session = self.storage.quest_session(session_id, 7)
            node = quest.nodes[str(session["current_node"])]
            self.bot.send_quest_node(7, session_id)
            if node.mode == "ending":
                break
            self.bot.handle_text(7, "Igor", node.reference_answer)

        session = self.storage.quest_session(session_id, 7)
        self.assertEqual("completed", session["status"])
        self.assertEqual("success", session["ending"])
        self.assertEqual("idle", self.storage.get_user(7)["stage"])
        self.assertEqual(1, self.storage.quest_history(7, "pl")[0]["successes"])

    def test_ai_checks_a_natural_free_reply_before_selecting_quest_route(self) -> None:
        class QuestAI:
            provider = "test"
            model = "quest-evaluator"

            def __init__(self) -> None:
                self.calls = 0

            def analyze_response(self, *args: Any, **kwargs: Any) -> ResponseAnalysis:
                self.calls += 1
                return ResponseAnalysis(
                    task_achieved=True,
                    score=0.9,
                    confidence=0.9,
                    positive_feedback="Cel rozmowy jest jasny.",
                    meaning_gaps=(),
                    critical_corrections=(),
                    optional_improvements=(),
                    natural_response="Dzień dobry, chcę złożyć wniosek o zameldowanie.",
                    alternatives=(),
                    grammar_chunks=(),
                    pragmatic_note="",
                    explanation="",
                    provider=self.provider,
                    model=self.model,
                    prompt_version="test",
                    latency_ms=1,
                    usage={},
                )

        ai = QuestAI()
        self.bot.ai = ai
        self.bot.begin_quest(7, "urzad_documents")
        session_id = str(self.storage.get_user(7)["current_quest"])
        self.bot.handle_text(7, "Igor", "Dzień dobry, chcę załatwić meldunek.")

        self.assertEqual(1, ai.calls)
        self.assertEqual("u2", self.storage.quest_session(session_id, 7)["current_node"])

    def test_quest_node_offers_only_hint_and_stop_not_answer_options(self) -> None:
        self.bot.begin_quest(7, "urzad_documents")
        session_id = str(self.storage.get_user(7)["current_quest"])
        self.bot.send_quest_node(7, session_id)

        keyboard = self.telegram.edits[-1]["keyboard"]
        callbacks = [item["callback_data"] for row in keyboard for item in row]
        self.assertTrue(any(value.startswith("quest:hint:") for value in callbacks))
        self.assertIn("quest:stop:confirm", callbacks)
        self.assertFalse(any(value.startswith("quest:choice:") for value in callbacks))

    def test_scenario_can_be_foreground_while_quest_remains_resumable(self) -> None:
        self.bot.begin_quest(7, "clinic_visit")
        session_id = str(self.storage.get_user(7)["current_quest"])
        self.bot.begin_scenario(7, "pharmacy")
        user = self.storage.get_user(7)
        self.assertEqual("scenario", user["stage"])
        self.assertIsNone(user["current_quest"])
        self.assertIsNotNone(user["current_session"])
        self.assertEqual(
            "active", self.storage.quest_session(session_id, 7)["status"]
        )
        self.bot.resume_saved_quest(7, session_id)
        self.assertEqual("quest", self.storage.get_user(7)["stage"])

    def test_english_course_reports_quest_content_unavailable(self) -> None:
        self.storage.set_language(7, "target_language", "en")
        self.bot.show_quests(7)
        self.assertIn("доступны для польского", self.telegram.edits[-1]["text"].lower())
