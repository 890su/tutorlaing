import unittest

from tutorlaing.toolkit_fallback import build_toolkit_fallback


class ToolkitFallbackTests(unittest.TestCase):
    def test_cards_are_five_four_option_items(self) -> None:
        material = {
            "phrases": [
                {
                    "target_phrase": f"Phrase {index}",
                    "practical_meaning_ru": f"Meaning {index}",
                }
                for index in range(6)
            ]
        }

        pack = build_toolkit_fallback("cards", material, "en")

        self.assertEqual(5, len(pack.items))
        self.assertTrue(all(item.type == "flashcard" for item in pack.items))
        self.assertTrue(all(len(item.options) == 4 for item in pack.items))
        self.assertTrue(all(item.correct_answer in item.options for item in pack.items))

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


if __name__ == "__main__":
    unittest.main()
