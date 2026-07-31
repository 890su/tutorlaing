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


if __name__ == "__main__":
    unittest.main()
