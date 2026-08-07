import tempfile
import unittest
from pathlib import Path

from tutorlaing.ai import Alternative, CoachResponse
from tutorlaing.coach import CoachService
from tutorlaing.storage import Storage


class OverhelpfulAI:
    provider = "test"
    model = "test"

    def coach(self, _context, _question, _operation) -> CoachResponse:
        return CoachResponse(
            answer="Use this complete answer",
            suggested_phrases=(Alternative("Gotowa odpowiedź", "neutral", ""),),
            translation="",
            disclosed_help_level=4,
        )


class CoachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "coach.sqlite3")
        self.storage.ensure_user(42, "Learner")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_hint_policy_rejects_a_complete_ai_answer(self) -> None:
        service = CoachService(self.storage, OverhelpfulAI())
        session = service.open(
            42,
            "scenario",
            "session-1",
            {"task": "Explain the problem", "hint": "Name the problem and duration"},
        )

        response = service.answer(42, session.id, "hint", "hint")

        self.assertEqual("Name the problem and duration", response.answer)
        self.assertEqual((), response.suggested_phrases)
        self.assertEqual(1, response.disclosed_help_level)


if __name__ == "__main__":
    unittest.main()
