import ast
import unittest
from pathlib import Path

from tutorlaing.commands import (
    COMMAND_DESCRIPTIONS,
    PUBLIC_COMMANDS,
    command_payload,
    parse_command,
)


class CommandCatalogTests(unittest.TestCase):
    def test_every_language_registers_the_same_canonical_commands(self) -> None:
        expected = [command.removeprefix("/") for command in PUBLIC_COMMANDS]

        for language in ("ru", "uk", "en", "pl"):
            payload = command_payload(language)
            self.assertEqual(expected, [item["command"] for item in payload])
            self.assertTrue(all(item["description"] for item in payload))
            self.assertEqual(len(PUBLIC_COMMANDS), len(COMMAND_DESCRIPTIONS[language]))

    def test_parser_accepts_telegram_bot_suffix(self) -> None:
        self.assertEqual("/practice", parse_command("/Practice@TutorlaingBot now"))
        self.assertEqual("", parse_command("обычный текст"))

    def test_every_registered_command_has_a_text_route(self) -> None:
        app_path = Path(__file__).parents[1] / "src" / "tutorlaing" / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
        routed: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not isinstance(node.left, ast.Name) or node.left.id != "command":
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    routed.add(comparator.value)

        self.assertEqual(set(PUBLIC_COMMANDS), routed & set(PUBLIC_COMMANDS))


if __name__ == "__main__":
    unittest.main()
