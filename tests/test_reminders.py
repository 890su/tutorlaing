import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tutorlaing.reminders import (
    ReminderScheduler,
    next_reminder_at,
    pause_until_tomorrow,
    reengagement_inactive_days,
)
from tutorlaing.privacy import CONSENT_VERSION
from tutorlaing.storage import Storage


class FakeReminderBot:
    def __init__(self):
        self.sent = []
        self.reengagement = []

    def send_scheduled_reminder(self, chat_id: int, mode: str) -> None:
        self.sent.append((chat_id, mode))

    def send_reengagement_reminder(
        self, chat_id: int, mode: str, inactive_days: int
    ) -> None:
        self.reengagement.append((chat_id, mode, inactive_days))


class FailingReminderBot(FakeReminderBot):
    def send_scheduled_reminder(self, chat_id: int, mode: str) -> None:
        raise RuntimeError("temporary Telegram failure")


class ReminderTests(unittest.TestCase):
    def test_reengagement_threshold_depends_on_selected_mode(self) -> None:
        now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
        base = {
            "last_interaction_at": (now - timedelta(days=2)).isoformat(),
            "last_reengagement_at": None,
            "consent_at": now.isoformat(),
            "created_at": now.isoformat(),
        }
        self.assertEqual(
            2,
            reengagement_inactive_days(
                {**base, "reminder_mode": "intensive"}, now
            ),
        )
        self.assertIsNone(
            reengagement_inactive_days({**base, "reminder_mode": "normal"}, now)
        )

    def test_inactive_learner_receives_motivation_instead_of_another_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            storage.ensure_user(61, "Learner")
            storage.accept_consent(61, CONSENT_VERSION)
            now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
            storage.record_user_interaction(61, now - timedelta(days=4))
            storage.set_reminder_mode(61, "normal", now - timedelta(minutes=1))
            bot = FakeReminderBot()

            sent = ReminderScheduler(bot, storage, interval=60).tick(now)

            self.assertEqual(1, sent)
            self.assertEqual([], bot.sent)
            self.assertEqual([(61, "normal", 4)], bot.reengagement)
            self.assertEqual(now.isoformat(), storage.get_user(61)["last_reengagement_at"])
            storage.close()

    def test_inactive_cooldown_suppresses_extra_tasks_and_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            storage.ensure_user(62, "Learner")
            storage.accept_consent(62, CONSENT_VERSION)
            now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
            storage.record_user_interaction(62, now - timedelta(days=4))
            storage.set_reminder_mode(62, "normal", now - timedelta(minutes=1))
            storage.record_reengagement_delivery(62, now - timedelta(hours=2))
            bot = FakeReminderBot()

            sent = ReminderScheduler(bot, storage, interval=60).tick(now)

            self.assertEqual(0, sent)
            self.assertEqual([], bot.sent)
            self.assertEqual([], bot.reengagement)
            self.assertGreater(storage.get_user(62)["reminder_next_at"], now.isoformat())
            storage.close()

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
            storage.accept_consent(55, CONSENT_VERSION)
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
            storage.accept_consent(56, CONSENT_VERSION)
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

    def test_scheduler_delivers_while_scenario_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            storage.ensure_user(59, "Learner")
            storage.accept_consent(59, CONSENT_VERSION)
            storage.start_session(59, "pharmacy")
            now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
            storage.set_reminder_mode(59, "normal", now - timedelta(minutes=1))
            bot = FakeReminderBot()

            sent = ReminderScheduler(bot, storage, interval=60).tick(now)

            self.assertEqual(1, sent)
            self.assertEqual([(59, "normal")], bot.sent)
            storage.close()

    def test_failed_delivery_is_retried_in_five_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            storage.ensure_user(60, "Learner")
            storage.accept_consent(60, CONSENT_VERSION)
            now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
            storage.set_reminder_mode(60, "normal", now - timedelta(minutes=1))

            sent = ReminderScheduler(
                FailingReminderBot(), storage, interval=60
            ).tick(now)

            self.assertEqual(0, sent)
            retry_at = datetime.fromisoformat(
                str(storage.get_user(60)["reminder_next_at"])
            )
            self.assertEqual(now + timedelta(minutes=5), retry_at)
            event = storage._connection.execute(
                "SELECT properties FROM events WHERE chat_id = ? AND event_name = ? ORDER BY id DESC LIMIT 1",
                (60, "reminder_delivery"),
            ).fetchone()
            self.assertIsNotNone(event)
            self.assertIn('"outcome": "failed"', event["properties"])
            storage.close()

    def test_scheduler_does_not_deliver_during_quiet_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            storage.ensure_user(57, "Learner")
            storage.accept_consent(57, CONSENT_VERSION)
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
            first.accept_consent(58, CONSENT_VERSION)
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
