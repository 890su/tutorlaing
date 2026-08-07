import unittest

from tutorlaing.content import load_scenarios
from tutorlaing.engine import normalize
from tutorlaing.quest_content import load_quests


class HintContentTests(unittest.TestCase):
    def test_scenario_hints_do_not_show_the_reference_phrase(self) -> None:
        for target_language in ("pl", "en"):
            for scenario in load_scenarios(target_language).values():
                for step in scenario.steps:
                    hint = normalize(step.hint_ru)
                    self.assertNotIn(normalize(step.target_chunk), hint)
                    self.assertNotIn("«", step.hint_ru)

    def test_quest_hints_do_not_show_the_reference_phrase(self) -> None:
        for quest in load_quests().values():
            for node in quest.nodes.values():
                if node.mode == "ending":
                    continue
                hint = normalize(node.hint_ru)
                self.assertNotIn(normalize(node.reference_answer), hint)
                self.assertNotIn("«", node.hint_ru)


if __name__ == "__main__":
    unittest.main()
