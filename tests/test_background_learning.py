import unittest

from tutorlaing.background_learning import BackgroundLearningService


class FakeStore:
    def __init__(
        self,
        recent: list[str] | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        self.recent = recent or []
        self.reasons = reasons or []

    def recent_background_card_types(
        self, chat_id: int, activity_id: str, limit: int = 3
    ) -> list[str]:
        return self.recent[:limit]

    def recent_background_card_reasons(
        self, chat_id: int, limit: int = 20
    ) -> list[str]:
        return self.reasons[:limit]


class BackgroundLearningTests(unittest.TestCase):
    def test_recall_uses_task_context_without_revealing_reference(self) -> None:
        service = BackgroundLearningService(FakeStore())

        draft = service.build(
            42,
            "scenario",
            "session-1",
            {
                "title": "W aptece",
                "task": "Powiedz, od jak dawna boli cię gardło.",
                "reference": "Od dwóch dni boli mnie gardło.",
                "step": 1,
            },
        )

        self.assertIsNotNone(draft)
        self.assertEqual("recall", draft.card_type)
        self.assertEqual("Powiedz, od jak dawna boli cię gardło.", draft.context)
        self.assertNotIn(draft.correct_answer, draft.context)
        self.assertEqual("1", draft.source_step)

    def test_card_types_rotate_for_the_same_activity(self) -> None:
        context = {
            "title": "W aptece",
            "task": "Odpowiedz farmaceucie.",
            "reference": "Od dwóch dni boli mnie gardło",
        }

        cloze = BackgroundLearningService(FakeStore(["recall"])).build(
            42, "scenario", "session-1", context
        )
        order = BackgroundLearningService(FakeStore(["cloze"])).build(
            42, "scenario", "session-1", context
        )
        paraphrase = BackgroundLearningService(FakeStore(["word_order"])).build(
            42, "scenario", "session-1", context
        )

        self.assertEqual("cloze", cloze.card_type)
        self.assertIn("_____", cloze.context)
        self.assertEqual("word_order", order.card_type)
        self.assertIn(" · ", order.context)
        self.assertEqual("paraphrase", paraphrase.card_type)

    def test_exact_free_text_answer_is_checked_without_ai(self) -> None:
        service = BackgroundLearningService(FakeStore())
        row = {
            "correct_answer": "Od dwóch dni",
            "accepted_answers_json": '["Od dwóch dni"]',
            "card_type": "recall",
            "prompt": "Przypomnij sobie frazę",
            "context": "Od kiedy?",
            "explanation": "Od dwóch dni",
        }

        score, corrected = service.evaluate(row, "od dwoch dni", "ru", "pl")

        self.assertEqual(1.0, score)
        self.assertEqual("Od dwóch dni", corrected)

    def test_selector_balances_current_related_and_older_due_sources(self) -> None:
        service = BackgroundLearningService(
            FakeStore(reasons=["current_activity", "related_activity", "current_activity"])
        )

        draft = service.build(
            42,
            "scenario",
            "session-1",
            {
                "title": "W aptece",
                "task": "Powiedz, co cię boli.",
                "reference": "Boli mnie gardło.",
                "related_candidates": [
                    {
                        "title": "W aptece",
                        "task": "Powiedz, od kiedy.",
                        "reference": "Od dwóch dni.",
                        "step": 2,
                    }
                ],
                "due_candidates": [
                    {
                        "title": "W urzędzie",
                        "task": "Poproś o powtórzenie.",
                        "reference": "Czy może pani powtórzyć?",
                        "step": 1,
                    }
                ],
            },
        )

        self.assertEqual("older_due", draft.reason)
        self.assertEqual("Czy może pani powtórzyć?", draft.correct_answer)
        self.assertEqual("1", draft.source_step)

    def test_selector_rebalances_when_only_current_source_exists(self) -> None:
        service = BackgroundLearningService(
            FakeStore(reasons=["current_activity"] * 10)
        )

        draft = service.build(
            42,
            "quest",
            "mission-1",
            {"reference": "Proszę wystawić paragon.", "task": "Poproś o paragon."},
        )

        self.assertEqual("current_activity", draft.reason)

    def test_semantic_card_is_interleaved_and_does_not_reveal_answer(self) -> None:
        service = BackgroundLearningService(FakeStore(["recall"]))

        draft = service.build(
            42,
            "scenario",
            "session-1",
            {
                "reference": "Piątek o czternastej mi pasuje.",
                "task": "Potwierdź termin.",
                "learning_cards": [
                    {
                        "kind": "meaning_in_context",
                        "cue": "Co znaczy «pasuje» w rozmowie o terminie wizyty?",
                        "answer": "termin jest dla mnie odpowiedni",
                        "accepted_answers": ["ten termin mi odpowiada"],
                        "explanation": "Tu chodzi o akceptację terminu.",
                    }
                ],
            },
        )

        self.assertEqual("meaning_in_context", draft.card_type)
        self.assertEqual("termin jest dla mnie odpowiedni", draft.correct_answer)
        self.assertNotIn(draft.correct_answer, draft.context)
        self.assertIn("ten termin mi odpowiada", draft.accepted_answers)


if __name__ == "__main__":
    unittest.main()
