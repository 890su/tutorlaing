import ast
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tutorlaing.catalog import ScenarioCatalog
from tutorlaing.contracts import TransportError
from tutorlaing.storage import Storage
from tutorlaing.update_dispatcher import TelegramUpdateDispatcher
from tutorlaing.workspace import TelegramWorkspace


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.edit_error: Exception | None = None

    def send_message(self, chat_id: int, text: str, keyboard=None) -> dict[str, Any]:
        message = {"message_id": len(self.sent) + 1, "text": text}
        self.sent.append(message)
        return message

    def edit_message(self, chat_id: int, message_id: int, text: str, keyboard=None):
        if self.edit_error:
            raise self.edit_error
        self.edited.append({"message_id": message_id, "text": text})
        return self.edited[-1]

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        return None

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None


class Target:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.should_fail = False

    def handle_text(self, chat_id: int, first_name: str, text: str) -> None:
        if self.should_fail:
            raise RuntimeError("failed")
        self.calls.append(("text", chat_id, first_name, text))

    def handle_callback(
        self, chat_id: int, first_name: str, callback_id: str, data: str
    ) -> None:
        self.calls.append(("callback", chat_id, first_name, callback_id, data))


class ApplicationModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "modules.sqlite3")
        self.storage.ensure_user(42, "Learner")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_workspace_edits_current_card_and_force_new_sends(self) -> None:
        telegram = FakeTelegram()
        workspace = TelegramWorkspace(self.storage, telegram)

        self.assertEqual(1, workspace.show(42, "first"))
        self.assertEqual(1, workspace.show(42, "second"))
        self.assertEqual(2, workspace.show(42, "scheduled", force_new=True))

        self.assertEqual([{"message_id": 1, "text": "second"}], telegram.edited)
        self.assertEqual(2, len(telegram.sent))

    def test_workspace_recovers_from_transport_edit_failure(self) -> None:
        telegram = FakeTelegram()
        workspace = TelegramWorkspace(self.storage, telegram)
        workspace.show(42, "first")
        telegram.edit_error = TransportError("temporary failure")

        self.assertEqual(2, workspace.show(42, "replacement"))
        self.assertEqual("replacement", telegram.sent[-1]["text"])

    def test_update_dispatcher_deduplicates_and_routes_messages(self) -> None:
        telegram = FakeTelegram()
        target = Target()
        dispatcher = TelegramUpdateDispatcher(self.storage, telegram, target)
        update = {
            "update_id": 91,
            "message": {
                "chat": {"id": 42},
                "from": {"first_name": "Igor"},
                "text": "Cześć",
            },
        }

        dispatcher.dispatch(update)
        dispatcher.dispatch(update)

        self.assertEqual([("text", 42, "Igor", "Cześć")], target.calls)

    def test_update_dispatcher_releases_failed_update_for_retry(self) -> None:
        telegram = FakeTelegram()
        target = Target()
        dispatcher = TelegramUpdateDispatcher(self.storage, telegram, target)
        update = {
            "update_id": 92,
            "message": {"chat": {"id": 42}, "text": "retry"},
        }
        target.should_fail = True
        with self.assertRaises(RuntimeError):
            dispatcher.dispatch(update)
        target.should_fail = False

        dispatcher.dispatch(update)

        self.assertEqual([("text", 42, "", "retry")], target.calls)

    def test_catalog_has_isolated_polish_and_english_courses(self) -> None:
        catalog = ScenarioCatalog()

        self.assertEqual(("pl", "en"), catalog.supported_languages)
        self.assertEqual(8, len(catalog.for_language("pl")))
        self.assertEqual(8, len(catalog.for_language("en")))
        with self.assertRaises(ValueError):
            catalog.for_language("de")

    def test_application_modules_do_not_import_composition_or_adapters(self) -> None:
        package = Path(__file__).parents[1] / "src" / "tutorlaing"
        modules = (
            "catalog.py",
            "evaluation_service.py",
            "feedback.py",
            "language_support.py",
            "menu.py",
            "navigation.py",
            "progress_service.py",
            "reminders.py",
            "toolkit.py",
            "update_dispatcher.py",
            "workspace.py",
        )
        forbidden = {"app", "storage", "telegram_api"}
        violations: list[str] = []
        for module in modules:
            tree = ast.parse((package / module).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported = node.module.lstrip(".").split(".")[0]
                    if imported in forbidden:
                        violations.append(f"{module}: {node.module}")

        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
