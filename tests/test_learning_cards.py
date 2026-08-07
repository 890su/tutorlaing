import unittest

from tutorlaing.content import load_scenarios
from tutorlaing.learning_cards import LearningCardSeed, learning_card_seeds


class LearningCardSeedTests(unittest.TestCase):
    def test_valid_seed_normalizes_and_keeps_free_text_variants(self) -> None:
        seed = LearningCardSeed.from_dict(
            {
                "kind": "synonym",
                "cue": "  Zastąp   wyrażenie: nie pasuje. ",
                "answer": "nie jest odpowiedni",
                "accepted_answers": ["nie jest odpowiedni", "jest niewłaściwy"],
                "explanation": "Dwa naturalne warianty w tym kontekście.",
            }
        )

        self.assertEqual("Zastąp wyrażenie: nie pasuje.", seed.cue)
        self.assertEqual(
            ("nie jest odpowiedni", "jest niewłaściwy"), seed.accepted_answers
        )

    def test_seed_rejects_a_cue_that_contains_the_answer(self) -> None:
        with self.assertRaisesRegex(ValueError, "reveals"):
            LearningCardSeed.from_dict(
                {
                    "kind": "definition_to_word",
                    "cue": "Odpowiedź to gorączka.",
                    "answer": "gorączka",
                }
            )

    def test_seed_rejects_a_cue_that_contains_an_accepted_variant(self) -> None:
        with self.assertRaisesRegex(ValueError, "accepted answer"):
            LearningCardSeed.from_dict(
                {
                    "kind": "synonym",
                    "cue": "Możesz napisać: ten termin mi odpowiada.",
                    "answer": "termin jest dla mnie odpowiedni",
                    "accepted_answers": ["ten termin mi odpowiada"],
                }
            )

    def test_runtime_parser_ignores_invalid_external_seed(self) -> None:
        parsed = learning_card_seeds(
            [
                {"kind": "unknown", "cue": "x", "answer": "y"},
                {
                    "kind": "antonym",
                    "cue": "Podaj przeciwieństwo słowa «bliższy».",
                    "answer": "dalszy",
                },
            ]
        )

        self.assertEqual(1, len(parsed))
        self.assertEqual("antonym", parsed[0].kind)

    def test_polish_scenarios_expose_reviewable_semantic_material(self) -> None:
        scenarios = load_scenarios("pl")
        kinds = {
            card.kind
            for scenario in scenarios.values()
            for step in scenario.steps
            for card in step.learning_cards
        }

        self.assertTrue(
            {
                "synonym",
                "antonym",
                "definition_to_word",
                "meaning_in_context",
                "grammar_transform",
            }.issubset(kinds)
        )


if __name__ == "__main__":
    unittest.main()
