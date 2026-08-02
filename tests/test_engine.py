import unittest
from datetime import datetime, timezone

from tutorlaing.content import load_scenarios
from tutorlaing.engine import (
    evaluate_response,
    normalize,
    review_due_at,
    review_interval_days,
    select_bottleneck,
)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenarios = load_scenarios()

    def test_content_contains_eight_unique_scenarios(self) -> None:
        self.assertEqual(8, len(self.scenarios))
        self.assertTrue(all(len(scenario.steps) >= 2 for scenario in self.scenarios.values()))

    def test_english_course_contains_equivalent_scenarios(self) -> None:
        scenarios = load_scenarios("en")
        self.assertEqual(set(self.scenarios), set(scenarios))
        pharmacy = scenarios["pharmacy"]
        self.assertEqual("At the pharmacy", pharmacy.title_pl)
        self.assertTrue(
            evaluate_response(
                pharmacy.steps[1], "For two days. I don't have a fever."
            ).successful
        )

    def test_normalize_accepts_missing_polish_diacritics(self) -> None:
        self.assertEqual("prosze isc w lewo", normalize("Proszę iść w lewo!"))

    def test_response_matches_communicative_groups(self) -> None:
        step = self.scenarios["pharmacy"].steps[0]
        result = evaluate_response(
            step, "Dzien dobry. Boli mnie gardlo i potrzebuje czegos na bol."
        )
        self.assertTrue(result.successful)
        self.assertEqual(1.0, result.score)

    def test_partial_response_exposes_missing_group(self) -> None:
        step = self.scenarios["pharmacy"].steps[1]
        result = evaluate_response(step, "Od dwóch dni.")
        self.assertEqual(0.5, result.score)
        self.assertEqual((1,), result.missing_groups)

    def test_task_blocking_step_wins_close_bottleneck_score(self) -> None:
        scenario = self.scenarios["introduce-yourself"]
        self.assertEqual(0, select_bottleneck(scenario, [(0, 0.7), (1, 0.6)]))

    def test_review_intervals_are_bounded_and_grow(self) -> None:
        self.assertEqual(2, review_interval_days(0.2))
        self.assertEqual(4, review_interval_days(0.7))
        self.assertEqual(14, review_interval_days(1.0, 7))
        self.assertEqual(30, review_interval_days(1.0, 20))
        base = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.assertEqual(4, (review_due_at(0.7, now=base) - base).days)


if __name__ == "__main__":
    unittest.main()
