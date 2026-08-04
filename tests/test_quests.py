import tempfile
import unittest
from pathlib import Path
from typing import Any

from tutorlaing.app import TutorlaingBot
from tutorlaing.config import Settings
from tutorlaing.quest_content import load_quests
from tutorlaing.quest_engine import answer_free, choose
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

    def set_reply_keyboard(self, chat_id, keyboard, placeholder=None):
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

    def test_choice_effects_are_merged_without_mutating_source_state(self) -> None:
        node = load_quests()["urzad_documents"].nodes["u1"]
        source = {"existing": "fact"}
        transition = choose(node, "a", source)
        self.assertEqual("u2", transition.next_node)
        self.assertEqual("fact", transition.state["existing"])
        self.assertNotEqual(source, transition.state)

    def test_all_callback_payloads_fit_telegram_limit(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000000"
        for quest in load_quests().values():
            for node in quest.nodes.values():
                for choice in node.choices:
                    payload = f"quest:choice:{session_id}:{node.id}:{choice.id}"
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
        self.assertTrue(self.storage.suspend_activity(42))
        self.assertIsNone(self.storage.get_user(42)["current_quest"])
        self.assertTrue(self.storage.restore_suspended_activity(42))
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
        self.bot.send_quest_node(7, session_id)
        self.bot.answer_quest_choice(7, session_id, "u1", "a")
        self.bot.send_quest_node(7, session_id)
        self.bot.answer_quest_choice(7, session_id, "u2", "a")
        self.bot.send_quest_node(7, session_id)
        self.bot.handle_text(7, "Igor", "Do kiedy mogę dostarczyć dokument?")
        self.bot.send_quest_node(7, session_id)
        self.bot.answer_quest_choice(7, session_id, "u4", "a")
        self.bot.send_quest_node(7, session_id)
        self.bot.handle_text(7, "Igor", "Wyślę dokument. Dziękuję za pomoc.")
        self.bot.send_quest_node(7, session_id)

        session = self.storage.quest_session(session_id, 7)
        self.assertEqual("completed", session["status"])
        self.assertEqual("success", session["ending"])
        self.assertEqual("idle", self.storage.get_user(7)["stage"])
        self.assertEqual(1, self.storage.quest_history(7, "pl")[0]["successes"])

    def test_active_quest_is_not_overwritten_by_old_scenario_button(self) -> None:
        self.bot.begin_quest(7, "clinic_visit")
        session_id = str(self.storage.get_user(7)["current_quest"])
        self.bot.begin_scenario(7, "pharmacy")
        user = self.storage.get_user(7)
        self.assertEqual("quest", user["stage"])
        self.assertEqual(session_id, user["current_quest"])
        self.assertIsNone(user["current_session"])

    def test_english_course_reports_quest_content_unavailable(self) -> None:
        self.storage.set_language(7, "target_language", "en")
        self.bot.show_quests(7)
        self.assertIn("доступны для польского", self.telegram.edits[-1]["text"].lower())
