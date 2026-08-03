import unittest

from tutorlaing.drill_fallback import build_adaptive_fallback


class AdaptiveFallbackTests(unittest.TestCase):
    def test_history_pack_is_varied_and_keeps_reference_answers(self) -> None:
        material = {
            "learner_level": "B1",
            "recent_learner_material": [
                {
                    "learner_response": "Ja potrzebuje pomoc",
                    "natural_response": "Potrzebuję pomocy.",
                    "corrections": ["dopełniacz"],
                },
                {
                    "learner_response": "Od dwa dni boli gardło",
                    "natural_response": "Od dwóch dni boli mnie gardło.",
                    "corrections": ["liczebnik"],
                },
            ],
            "recurring_problem_material": [
                {
                    "target_chunk": "Czy może pan mówić wolniej?",
                    "context": "Rozmówca mówi za szybko",
                }
            ],
        }

        pack = build_adaptive_fallback(material, "ru")

        self.assertEqual(8, len(pack.items))
        self.assertGreaterEqual(len({item.type for item in pack.items}), 4)
        self.assertTrue(all(not item.options for item in pack.items))
        self.assertTrue(
            all(item.correct_answer in item.accepted_answers for item in pack.items)
        )
        self.assertEqual({2}, {item.difficulty for item in pack.items})

    def test_fallback_uses_instruction_language(self) -> None:
        pack = build_adaptive_fallback(
            {
                "learner_level": "A2",
                "recent_learner_material": [
                    {
                        "learner_response": "I need help",
                        "natural_response": "I need some help.",
                    }
                ],
            },
            "pl",
        )

        self.assertEqual("Powtórka z historii", pack.title)
        self.assertIn("zdanie", pack.items[0].prompt)


if __name__ == "__main__":
    unittest.main()
