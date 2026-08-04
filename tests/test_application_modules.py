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
from tutorlaing.telegram_api import TelegramAPI


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.edit_error: Exception | None = None
        self.deleted: list[int] = []
        self.reply_keyboards: list[list[list[str]]] = []

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

    def set_reply_keyboard(
        self, chat_id: int, keyboard: list[list[str]], placeholder: str | None = None
    ) -> None:
        self.reply_keyboards.append(keyboard)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    def send_temporary_message(self, chat_id: int, text: str, ttl_seconds: int = 5):
        return {"message_id": len(self.sent) + 1, "text": text}

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None


class Target:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.should_fail = False

    def handle_text(
        self, chat_id: int, first_name: str, text: str, message_id: int | None = None
    ) -> None:
        if self.should_fail:
            raise RuntimeError("failed")
        self.calls.append(("text", chat_id, first_name, text, message_id))

    def handle_callback(
        self,
        chat_id: int,
        first_name: str,
        callback_id: str,
        data: str,
        message_id: int | None = None,
    ) -> None:
        self.calls.append(
            ("callback", chat_id, first_name, callback_id, data, message_id)
        )


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
        self.assertEqual([1], telegram.deleted)

    def test_reply_keyboard_service_message_is_deleted(self) -> None:
        telegram = TelegramAPI("test-token")
        calls: list[tuple[str, dict[str, Any]]] = []

        def call(method, params=None, timeout=40):
            calls.append((method, params or {}))
            return {"message_id": 77} if method == "sendMessage" else True

        telegram.call = call

        telegram.set_reply_keyboard(42, [["▶ Учиться", "🧰 Инструменты"]])

        self.assertEqual(["sendMessage", "deleteMessage"], [item[0] for item in calls])
        self.assertTrue(calls[0][1]["reply_markup"]["is_persistent"])
        self.assertEqual(77, calls[1][1]["message_id"])

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

        self.assertEqual([("text", 42, "Igor", "Cześć", None)], target.calls)

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

        self.assertEqual([("text", 42, "", "retry", None)], target.calls)

    def test_update_dispatcher_forwards_visible_message_ids(self) -> None:
        target = Target()
        dispatcher = TelegramUpdateDispatcher(self.storage, FakeTelegram(), target)

        dispatcher.dispatch(
            {
                "update_id": 93,
                "message": {
                    "message_id": 501,
                    "chat": {"id": 42},
                    "from": {"first_name": "Igor"},
                    "text": "⌂ Главное меню",
                },
            }
        )
        dispatcher.dispatch(
            {
                "update_id": 94,
                "callback_query": {
                    "id": "cb",
                    "data": "home",
                    "from": {"first_name": "Igor"},
                    "message": {"message_id": 502, "chat": {"id": 42}},
                },
            }
        )

        self.assertEqual(
            [
                ("text", 42, "Igor", "⌂ Главное меню", 501),
                ("callback", 42, "Igor", "cb", "home", 502),
            ],
            target.calls,
        )

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
