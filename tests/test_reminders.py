import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tutorlaing.reminders import ReminderScheduler, next_reminder_at, pause_until_tomorrow
from tutorlaing.storage import Storage


class FakeReminderBot:
    def __init__(self):
        self.sent = []

    def send_scheduled_reminder(self, chat_id: int, mode: str) -> None:
        self.sent.append((chat_id, mode))


class ReminderTests(unittest.TestCase):
    def test_next_slot_respects_warsaw_quiet_hours(self) -> None:
        now = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)  # 22:00 Warsaw
        result = next_reminder_at("aggressive", now)
        local = result.astimezone(ZoneInfo("Europe/Warsaw"))
        self.assertEqual((2026, 8, 2, 8, 0), (local.year, local.month, local.day, local.hour, local.minute))

    def test_pause_ends_at_eight_next_local_day(self) -> None:
        now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        result = pause_until_tomorrow(now)
        self.assertEqual(8, result.astimezone(ZoneInfo("Europe/Warsaw")).hour)

    def test_scheduler_reserves_next_slot_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            storage.ensure_user(55, "Learner")
            storage.accept_consent(55, 2)
            now = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
            storage.set_reminder_mode(55, "gentle", now - timedelta(minutes=1))
            bot = FakeReminderBot()
            scheduler = ReminderScheduler(bot, storage, interval=60)
            self.assertEqual(1, scheduler.tick(now))
            self.assertEqual([(55, "gentle")], bot.sent)
            self.assertGreater(storage.get_user(55)["reminder_next_at"], now.isoformat())
            storage.close()

    def test_scheduler_delivers_while_drill_waits_for_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            storage.ensure_user(56, "Learner")
            storage.accept_consent(56, 2)
            item = {
                "type": "free_recall",
                "skill": "chunk",
                "prompt": "Answer",
                "context": "Context",
                "options": [],
                "correct_answer": "Answer",
                "accepted_answers": ["Answer"],
                "explanation": "Why",
                "hint": "Hint",
                "difficulty": 1,
            }
            storage.start_drill(56, None, "Test", "Test", [item])
            now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
            storage.set_reminder_mode(56, "normal", now - timedelta(minutes=1))
            bot = FakeReminderBot()
            scheduler = ReminderScheduler(bot, storage, interval=60)
            self.assertEqual(1, scheduler.tick(now))
            self.assertEqual([(56, "normal")], bot.sent)
            storage.close()

    def test_scheduler_does_not_deliver_during_quiet_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            storage.ensure_user(57, "Learner")
            storage.accept_consent(57, 2)
            now = datetime(2026, 8, 1, 21, 0, tzinfo=timezone.utc)
            storage.set_reminder_mode(57, "aggressive", now - timedelta(minutes=1))
            bot = FakeReminderBot()
            scheduler = ReminderScheduler(bot, storage, interval=60)
            self.assertEqual(0, scheduler.tick(now))
            self.assertEqual([], bot.sent)
            next_at = datetime.fromisoformat(storage.get_user(57)["reminder_next_at"])
            self.assertGreater(next_at, now)
            storage.close()

    def test_stale_scheduler_snapshot_cannot_send_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "test.sqlite3"
            first = Storage(database)
            second = Storage(database)
            first.ensure_user(58, "Learner")
            first.accept_consent(58, 2)
            now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
            due_at = now - timedelta(minutes=1)
            next_at = now + timedelta(hours=1)
            first.set_reminder_mode(58, "normal", due_at)
            stale_value = str(second.due_reminder_users(now)[0]["reminder_next_at"])

            self.assertTrue(first.reserve_next_reminder(58, stale_value, now, next_at))
            self.assertFalse(second.reserve_next_reminder(58, stale_value, now, next_at))
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
