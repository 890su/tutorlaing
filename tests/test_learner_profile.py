import tempfile
import unittest
from pathlib import Path

from tutorlaing.activities import ActivityService
from tutorlaing.learner_profile import LearnerProfileService
from tutorlaing.storage import Storage


class LearnerProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "profile.sqlite3")
        self.storage.ensure_user(42, "Learner")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_optional_profile_is_created_with_privacy_safe_defaults(self) -> None:
        profile = LearnerProfileService(self.storage).get(42)

        self.assertEqual("unset", profile.age_band)
        self.assertEqual("", profile.weekly_context)
        self.assertTrue(profile.adaptive_level_enabled)

    def test_profile_service_validates_choices_and_normalizes_context(self) -> None:
        service = LearnerProfileService(self.storage)

        service.set_age_band(42, "25_34")
        service.set_life_role(42, "working")
        service.set_weekly_context(42, "  Магазин,   работа и школа ребёнка. ")

        profile = service.get(42)
        self.assertEqual("25_34", profile.age_band)
        self.assertEqual("working", profile.life_role)
        self.assertEqual("Магазин, работа и школа ребёнка.", profile.weekly_context)
        with self.assertRaises(ValueError):
            service.set_age_band(42, "1990-01-01")

    def test_open_activity_projection_keeps_one_foreground(self) -> None:
        scenario_id = self.storage.start_session(42, "pharmacy")
        quest_id = self.storage.start_quest(
            42, "ticket_control", "start", preserve_active=True
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
        drill_id = self.storage.start_drill(
            42, None, "Cards", "Meaning", [item], replace_active=False
        )

        activities = ActivityService(self.storage).list_open(42)

        self.assertEqual({"scenario", "quest", "drill"}, {item.kind for item in activities})
        self.assertEqual([drill_id], [item.session_id for item in activities if item.is_foreground])
        self.storage.resume_quest_session(42, quest_id)
        self.assertEqual("quest", self.storage.get_user(42)["stage"])
        self.storage.resume_scenario_session(42, scenario_id)
        self.assertEqual("scenario", self.storage.get_user(42)["stage"])


if __name__ == "__main__":
    unittest.main()
