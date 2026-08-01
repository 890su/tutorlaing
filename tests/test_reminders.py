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


if __name__ == "__main__":
    unittest.main()
