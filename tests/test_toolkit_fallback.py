import unittest
from random import Random

from tutorlaing.toolkit_fallback import build_toolkit_fallback
from tutorlaing.toolkit import shuffle_flashcard_options


class ToolkitFallbackTests(unittest.TestCase):
    def test_cards_are_ten_four_option_items(self) -> None:
        material = {
            "phrases": [
                {
                    "target_phrase": f"Phrase {index}",
                    "practical_meaning_ru": f"Meaning {index}",
                }
                for index in range(12)
            ]
        }

        pack = build_toolkit_fallback("cards", material, "en")

        self.assertEqual(10, len(pack.items))
        self.assertTrue(all(item.type == "flashcard" for item in pack.items))
        self.assertTrue(all(len(item.options) == 4 for item in pack.items))
        self.assertTrue(all(item.correct_answer in item.options for item in pack.items))

    def test_flashcard_correct_answers_use_all_button_positions(self) -> None:
        material = {
            "phrases": [
                {
                    "target_phrase": f"Phrase {index}",
                    "practical_meaning_ru": f"Meaning {index}",
                }
                for index in range(12)
            ]
        }

        pack = shuffle_flashcard_options(
            build_toolkit_fallback("cards", material, "en"), Random(7)
        )

        correct_positions = [
            item.options.index(item.correct_answer) for item in pack.items
        ]
        self.assertEqual({0, 1, 2, 3}, set(correct_positions))
        self.assertTrue(all(len(set(item.options)) == 4 for item in pack.items))

    def test_topic_pack_preserves_variety_and_active_recall(self) -> None:
        material = {
            "title": "Pharmacy",
            "objective_ru": "Ask for medicine",
            "steps": [
                {
                    "target_chunk": f"Target {index}",
                    "learner_goal_ru": f"Goal {index}",
                }
                for index in range(3)
            ],
        }

        pack = build_toolkit_fallback("topic", material, "en")

        self.assertEqual(5, len(pack.items))
        self.assertGreaterEqual(len({item.type for item in pack.items}), 3)
        self.assertGreaterEqual(sum(not item.options for item in pack.items), 2)

    def test_topic_pack_uses_level_policy_for_scaffolding_and_difficulty(self) -> None:
        base = {
            "title": "Pharmacy",
            "objective_ru": "Ask for medicine",
            "steps": [
                {
                    "target_chunk": f"Target {index}",
                    "learner_goal_ru": f"Goal {index}",
                }
                for index in range(4)
            ],
        }

        beginner = build_toolkit_fallback(
            "topic", {**base, "learner_level": "A1"}, "en"
        )
        advanced = build_toolkit_fallback(
            "topic", {**base, "learner_level": "B2"}, "en"
        )

        self.assertEqual(3, sum(bool(item.options) for item in beginner.items))
        self.assertEqual(1, sum(bool(item.options) for item in advanced.items))
        self.assertEqual({1}, {item.difficulty for item in beginner.items})
        self.assertEqual({3}, {item.difficulty for item in advanced.items})


if __name__ == "__main__":
    unittest.main()
