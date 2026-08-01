import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tutorlaing.storage import Storage


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

    def test_new_session_abandons_previous_active_session(self) -> None:
        self.storage.ensure_user(9)
        first = self.storage.start_session(9, "pharmacy")
        second = self.storage.start_session(9, "directions")
        self.assertEqual("abandoned", self.storage.session(first)["status"])
        self.assertEqual("active", self.storage.session(second)["status"])

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
        drill_id = self.storage.start_drill(88, None, "Test", "Chunk", [item, item])
        first = self.storage.drill_item(drill_id, 0)
        self.storage.answer_drill_item(int(first["id"]), "Od dwóch dni", 1.0)
        self.assertTrue(self.storage.advance_drill(drill_id, 88))
        second = self.storage.drill_item(drill_id, 1)
        self.storage.answer_drill_item(int(second["id"]), "Od dwóch dni", 1.0)
        self.assertFalse(self.storage.advance_drill(drill_id, 88))
        self.assertEqual("completed", self.storage.drill_session(drill_id)["status"])
        self.assertEqual("idle", self.storage.get_user(88)["stage"])


if __name__ == "__main__":
    unittest.main()
