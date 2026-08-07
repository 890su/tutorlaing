import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tutorlaing.storage import Storage
from tutorlaing.privacy import CONSENT_VERSION


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_session_review_and_delete_flow(self) -> None:
        self.storage.ensure_user(42, "Igor")
        self.storage.accept_consent(42)
        session_id = self.storage.start_session(42, "pharmacy")
        self.storage.add_response(session_id, 0, "scenario", "test", 0.5, (1,))
        self.storage.add_response(session_id, 1, "scenario", "test", 1.0, ())

        self.assertEqual([(0, 0.5), (1, 1.0)], self.storage.scenario_scores(session_id))
        self.storage.complete_session(session_id, 0, 0.75)
        review_id = self.storage.schedule_review(
            42, "pharmacy", 0, datetime.now(timezone.utc), 2
        )
        self.assertEqual(review_id, self.storage.pending_reviews(42)[0]["id"])

        self.storage.complete_review(review_id, 1.0)
        self.assertEqual([], self.storage.pending_reviews(42))
        self.storage.add_outcome(42, session_id, "success")
        self.storage.delete_user(42)
        with self.assertRaises(KeyError):
            self.storage.get_user(42)

    def test_health_reports_database_state(self) -> None:
        self.storage.ensure_user(1)
        self.assertEqual({"database": "ok", "users": 1}, self.storage.health())

    def test_manual_profile_level_resets_temporary_difficulty(self) -> None:
        self.storage.ensure_user(5)
        proposal = self.storage.create_difficulty_proposal(
            5,
            "pl",
            1,
            "production",
            {
                "average_score": 0.95,
                "severe_rate": 0.0,
                "hint_rate": 0.0,
                "production_attempts": 12,
                "distinct_days": 2,
            },
        )
        self.storage.resolve_difficulty_proposal(
            5,
            int(proposal["id"]),
            True,
            datetime.now(timezone.utc) + timedelta(days=7),
        )
        self.assertEqual(1, self.storage.get_user(5)["practice_difficulty_offset"])
        self.storage.set_learner_level(5, "A2")
        user = self.storage.get_user(5)
        self.assertEqual("A2", user["learner_level"])
        self.assertEqual(0, user["practice_difficulty_offset"])

    def test_new_session_abandons_previous_active_session(self) -> None:
        self.storage.ensure_user(9)
        first = self.storage.start_session(9, "pharmacy")
        second = self.storage.start_session(9, "directions")
        self.assertEqual("abandoned", self.storage.session(first)["status"])
        self.assertEqual("active", self.storage.session(second)["status"])

    def test_abandon_all_activities_closes_every_open_session(self) -> None:
        chat_id = 91
        self.storage.ensure_user(chat_id, "Learner")
        first = self.storage.start_session(chat_id, "pharmacy")
        second = self.storage.start_session(
            chat_id, "directions", preserve_active=True
        )
        quest = self.storage.start_quest(
            chat_id, "ticket_control", "start", preserve_active=True
        )
        item = {
            "type": "free_recall",
            "skill": "meaning",
            "prompt": "Reply",
            "context": "Context",
            "options": [],
            "correct_answer": "Answer",
            "accepted_answers": ["Answer"],
            "explanation": "Why",
            "hint": "Hint",
            "difficulty": 1,
        }
        drill = self.storage.start_drill(
            chat_id, None, "Cards", "Meaning", [item], replace_active=False
        )

        closed = self.storage.abandon_all_activities(chat_id)

        self.assertEqual(4, closed)
        self.assertEqual("abandoned", self.storage.session(first)["status"])
        self.assertEqual("abandoned", self.storage.session(second)["status"])
        self.assertEqual("abandoned", self.storage.quest_session(quest, chat_id)["status"])
        self.assertEqual("abandoned", self.storage.drill_session(drill)["status"])
        self.assertEqual(0, self.storage.open_activity_count(chat_id))
        user = self.storage.get_user(chat_id)
        self.assertEqual("idle", user["stage"])

    def test_language_settings_and_ai_data_are_deleted_with_user(self) -> None:
        self.storage.ensure_user(77, "Learner")
        self.storage.accept_consent(77, 2)
        self.storage.set_language(77, "instruction_language", "uk")
        analysis_id = self.storage.add_ai_analysis(
            chat_id=77,
            operation="response_analysis",
            source_text="Dzień dobry",
            result={"task_achieved": True},
            provider="gemini",
            model="test-model",
            prompt_version="test-v1",
            latency_ms=12,
        )
        self.assertEqual("uk", self.storage.get_user(77)["instruction_language"])
        self.assertEqual(analysis_id, self.storage.latest_ai_analysis(77)["id"])
        self.storage.delete_user(77)
        self.assertIsNone(self.storage.latest_ai_analysis(77))

    def test_latest_analysis_is_scoped_to_target_language(self) -> None:
        self.storage.ensure_user(78, "Learner")
        polish = self.storage.add_ai_analysis(
            chat_id=78,
            operation="response_analysis",
            target_language="pl",
            source_text="Dzień dobry",
            result={"task_achieved": True},
            provider="gemini",
            model="test-model",
            prompt_version="test-v1",
            latency_ms=1,
        )
        english = self.storage.add_ai_analysis(
            chat_id=78,
            operation="response_analysis",
            target_language="en",
            source_text="Good morning",
            result={"task_achieved": True},
            provider="gemini",
            model="test-model",
            prompt_version="test-v1",
            latency_ms=1,
        )
        self.assertEqual(polish, self.storage.latest_ai_analysis(78, "pl")["id"])
        self.assertEqual(english, self.storage.latest_ai_analysis(78, "en")["id"])

    def test_reviews_are_scoped_to_target_language(self) -> None:
        self.storage.ensure_user(79, "Learner")
        self.storage.accept_consent(79, 2)
        self.storage.schedule_review(
            79, "pharmacy", 0, datetime.now(timezone.utc), 2
        )
        self.assertEqual(1, len(self.storage.pending_reviews(79)))
        self.storage.set_language(79, "target_language", "en")
        self.assertEqual([], self.storage.pending_reviews(79))

    def test_telegram_update_is_claimed_once_and_can_be_retried_after_failure(self) -> None:
        self.assertTrue(self.storage.claim_update(123))
        self.assertFalse(self.storage.claim_update(123))
        self.storage.release_update(123)
        self.assertTrue(self.storage.claim_update(123))
        self.storage.complete_update(123)
        self.assertFalse(self.storage.claim_update(123))

    def test_drill_state_survives_item_transitions(self) -> None:
        self.storage.ensure_user(88, "Learner")
        self.storage.accept_consent(88, 2)
        item = {
            "type": "free_recall",
            "skill": "chunk",
            "prompt": "Odpowiedz",
            "context": "Od kiedy?",
            "options": [],
            "correct_answer": "Od dwóch dni",
            "accepted_answers": ["Od dwóch dni"],
            "explanation": "Poprawnie",
            "hint": "Od…",
            "difficulty": 1,
        }
        drill_id = self.storage.start_drill(
            88, None, "Test", "Chunk", [item, item], mode="toolkit_topic"
        )
        self.assertEqual(
            "toolkit_topic", self.storage.drill_session(drill_id)["mode"]
        )
        first = self.storage.drill_item(drill_id, 0)
        self.storage.answer_drill_item(int(first["id"]), "Od dwóch dni", 1.0)
        self.assertTrue(self.storage.advance_drill(drill_id, 88))
        second = self.storage.drill_item(drill_id, 1)
        self.storage.answer_drill_item(int(second["id"]), "Od dwóch dni", 1.0)
        self.assertFalse(self.storage.advance_drill(drill_id, 88))
        self.assertEqual("completed", self.storage.drill_session(drill_id)["status"])
        self.assertEqual("idle", self.storage.get_user(88)["stage"])

    def test_toolkit_columns_are_added_to_an_existing_database(self) -> None:
        path = self.storage.path
        self.storage.close()
        connection = sqlite3.connect(path)
        connection.execute("ALTER TABLE users DROP COLUMN toolkit_input_mode")
        connection.execute("ALTER TABLE drill_sessions DROP COLUMN mode")
        connection.commit()
        connection.close()

        self.storage = Storage(path)

        user_columns = {
            row[1]
            for row in self.storage._connection.execute("PRAGMA table_info(users)")
        }
        drill_columns = {
            row[1]
            for row in self.storage._connection.execute(
                "PRAGMA table_info(drill_sessions)"
            )
        }
        self.assertIn("toolkit_input_mode", user_columns)
        self.assertNotIn("suspended_activity_json", user_columns)
        self.assertIn("reply_keyboard_version", user_columns)
        self.assertIn("mode", drill_columns)

    def test_phrase_input_temporarily_suppresses_scheduled_delivery(self) -> None:
        chat_id = 89
        self.storage.ensure_user(chat_id, "Learner")
        self.storage.accept_consent(chat_id, CONSENT_VERSION)
        now = datetime.now(timezone.utc)
        self.storage.set_reminder_mode(chat_id, "normal", now - timedelta(minutes=1))
        self.storage.set_user_state(
            chat_id,
            stage="waiting",
            pending_assignment='{"kind":"scenario"}',
            toolkit_input_mode="to_target",
        )

        self.assertEqual([], self.storage.due_reminder_users(now))

        self.storage.set_user_state(chat_id, toolkit_input_mode=None)
        self.assertEqual(chat_id, self.storage.due_reminder_users(now)[0]["chat_id"])

    def test_problem_history_combines_scenario_and_failed_card_evidence(self) -> None:
        chat_id = 90
        self.storage.ensure_user(chat_id, "Learner")
        session_id = self.storage.start_session(chat_id, "pharmacy")
        self.storage.add_response(
            session_id, 1, "scenario", "wrong form", 0.25, (0,)
        )
        item = {
            "type": "flashcard",
            "skill": "meaning",
            "prompt": "Meaning?",
            "context": "Od dwóch dni.",
            "options": ["two days", "tomorrow", "yesterday", "never"],
            "correct_answer": "two days",
            "accepted_answers": ["two days"],
            "explanation": "Duration",
            "hint": "od",
            "difficulty": 1,
        }
        drill_id = self.storage.start_drill(
            chat_id, None, "Cards", "History", [item], mode="toolkit_cards"
        )
        row = self.storage.drill_item(drill_id, 0)
        self.storage.answer_drill_item(int(row["id"]), "never", 0.0)

        history = self.storage.problem_history(chat_id, "pl")

        self.assertEqual("pharmacy", history["scenario_steps"][0]["scenario_id"])
        self.assertEqual(1, history["scenario_steps"][0]["step_index"])
        self.assertEqual("flashcard", history["drill_items"][0]["item_type"])
        self.assertEqual("Od dwóch dni.", history["drill_items"][0]["context"])

    def test_text_inbox_is_scoped_to_owner_and_cascades_on_delete(self) -> None:
        self.storage.ensure_user(101, "One")
        self.storage.ensure_user(102, "Two")
        inbox_id = self.storage.save_text_inbox(101, "Potrzebuję pomocy.")

        self.assertEqual(
            "Potrzebuję pomocy.", self.storage.text_inbox(101, inbox_id)["text"]
        )
        with self.assertRaises(KeyError):
            self.storage.text_inbox(102, inbox_id)
        self.storage.delete_user(101)
        with self.assertRaises(KeyError):
            self.storage.text_inbox(101, inbox_id)


if __name__ == "__main__":
    unittest.main()
