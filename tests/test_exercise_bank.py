import tempfile
import unittest
from pathlib import Path

from tutorlaing.ai import DrillItem, DrillPack
from tutorlaing.exercise_bank import ExerciseBank, material_signature
from tutorlaing.storage import Storage


def varied_pack() -> DrillPack:
    types = [
        "choose_form",
        "fill_ending",
        "complete_sentence",
        "transform",
        "word_order",
        "correct_error",
        "meaning_choice",
        "free_recall",
    ]
    items = []
    for index, item_type in enumerate(types):
        options = ("dobrze", "źle") if index in {0, 6} else ()
        items.append(
            DrillItem(
                type=item_type,
                skill=f"skill-{index}",
                prompt=f"Prompt {index}",
                context="Context",
                options=options,
                correct_answer="dobrze" if options else f"answer-{index}",
                accepted_answers=("dobrze",) if options else (f"answer-{index}",),
                explanation="Explanation",
                hint="Hint",
                difficulty=2,
            )
        )
    return DrillPack("Pack", "Focus", tuple(items))


class ExerciseBankTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "test.sqlite3")
        self.storage.ensure_user(1, "One")
        self.storage.ensure_user(2, "Two")
        self.bank = ExerciseBank(self.storage)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_private_pack_is_reused_only_by_its_owner(self) -> None:
        self.bank.add_pack(
            1,
            varied_pack(),
            target_language="pl",
            instruction_language="ru",
            translation_language="ru",
            learner_level="B1",
            mode="adaptive",
            private=True,
            tags=["personal:errors"],
        )

        owner_pack = self.bank.find_pack(
            1,
            target_language="pl",
            instruction_language="ru",
            translation_language="ru",
            learner_level="B1",
            mode="adaptive",
        )
        other_pack = self.bank.find_pack(
            2,
            target_language="pl",
            instruction_language="ru",
            translation_language="ru",
            learner_level="B1",
            mode="adaptive",
        )

        self.assertIsNotNone(owner_pack)
        self.assertIsNone(other_pack)
        self.assertIsNone(
            self.bank.find_pack(
                1,
                target_language="pl",
                instruction_language="ru",
                translation_language="ru",
                learner_level="B1",
                mode="adaptive",
            )
        )

    def test_saved_exercise_has_formal_adaptation_metadata(self) -> None:
        self.bank.add_pack(
            1,
            varied_pack(),
            target_language="pl",
            instruction_language="ru",
            translation_language="ru",
            learner_level="B1",
            mode="adaptive",
            private=True,
        )
        rows = self.storage.exercise_candidates(
            1,
            target_language="pl",
            instruction_language="ru",
            translation_language="ru",
            learner_level="B1",
            mode="adaptive",
        )
        choice = next(row for row in rows if row["options_json"] != "[]")
        recall = next(row for row in rows if row["options_json"] == "[]")
        self.assertEqual("choice", choice["response_mode"])
        self.assertEqual(0.4, choice["evidence_weight"])
        self.assertEqual("text", recall["response_mode"])
        self.assertTrue(recall["variant_group"])
        self.assertIn('"cognitive"', recall["difficulty_vector_json"])
        self.assertIn('"accepted_answers"', recall["rubric_json"])

    def test_material_signature_is_stable_but_changes_with_new_evidence(self) -> None:
        first = material_signature({"errors": ["case"], "level": "B1"})
        reordered = material_signature({"level": "B1", "errors": ["case"]})
        changed = material_signature({"errors": ["case", "aspect"], "level": "B1"})

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_attempt_updates_global_and_learner_statistics(self) -> None:
        saved = self.bank.add_pack(
            1,
            varied_pack(),
            target_language="pl",
            instruction_language="ru",
            translation_language="ru",
            learner_level="B1",
            mode="adaptive",
            private=True,
        )
        drill_id = self.storage.start_drill(
            1, None, saved.pack.title, saved.pack.focus, saved.drill_items()
        )
        item = self.storage.drill_item(drill_id, 0)

        self.storage.answer_drill_item(int(item["id"]), "dobrze", 1.0)

        with self.storage._lock:
            global_stats = self.storage._connection.execute(
                "SELECT answer_count, correct_count, avg_score FROM exercise_bank WHERE id = ?",
                (saved.exercise_ids[0],),
            ).fetchone()
            learner_stats = self.storage._connection.execute(
                "SELECT answer_count, mastery_strength, next_due_at FROM learner_exercise_stats WHERE chat_id = 1 AND exercise_id = ?",
                (saved.exercise_ids[0],),
            ).fetchone()
        self.assertEqual(1, global_stats["answer_count"])
        self.assertEqual(1, global_stats["correct_count"])
        self.assertEqual(1.0, global_stats["avg_score"])
        self.assertEqual(1, learner_stats["answer_count"])
        self.assertGreater(learner_stats["mastery_strength"], 0)
        self.assertIsNotNone(learner_stats["next_due_at"])

    def test_level_and_language_are_hard_bank_boundaries(self) -> None:
        self.bank.add_pack(
            1,
            varied_pack(),
            target_language="pl",
            instruction_language="ru",
            translation_language="ru",
            learner_level="B1",
            mode="adaptive",
            private=True,
        )
        self.assertIsNone(
            self.bank.find_pack(
                1,
                target_language="pl",
                instruction_language="pl",
                translation_language="ru",
                learner_level="B1",
                mode="adaptive",
            )
        )
        self.assertIsNone(
            self.bank.find_pack(
                1,
                target_language="pl",
                instruction_language="ru",
                translation_language="ru",
                learner_level="A2",
                mode="adaptive",
            )
        )


if __name__ == "__main__":
    unittest.main()
