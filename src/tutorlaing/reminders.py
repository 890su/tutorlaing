from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .app import TutorlaingBot
    from .storage import Storage


LOGGER = logging.getLogger(__name__)

REMINDER_SLOTS = {
    "gentle": ((19, 0),),
    "normal": ((9, 0), (19, 0)),
    "intensive": ((8, 0), (12, 0), (17, 0), (21, 0)),
    "aggressive": ((8, 0), (10, 30), (13, 0), (15, 30), (18, 0), (21, 0)),
}


def is_delivery_time(
    now: datetime | None = None, timezone_name: str = "Europe/Warsaw"
) -> bool:
    current_utc = now or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    local_now = current_utc.astimezone(ZoneInfo(timezone_name))
    return 8 <= local_now.hour < 22


def next_reminder_at(
    mode: str,
    now: datetime | None = None,
    timezone_name: str = "Europe/Warsaw",
) -> datetime | None:
    slots = REMINDER_SLOTS.get(mode)
    if not slots:
        return None
    current_utc = now or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    zone = ZoneInfo(timezone_name)
    local_now = current_utc.astimezone(zone)
    for day_offset in range(8):
        date = (local_now + timedelta(days=day_offset)).date()
        for hour, minute in slots:
            candidate = datetime(
                date.year, date.month, date.day, hour, minute, tzinfo=zone
            )
            if candidate > local_now + timedelta(minutes=1):
                return candidate.astimezone(timezone.utc)
    raise RuntimeError("Could not calculate next reminder")


def pause_until_tomorrow(
    now: datetime | None = None, timezone_name: str = "Europe/Warsaw"
) -> datetime:
    current_utc = now or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    zone = ZoneInfo(timezone_name)
    tomorrow = current_utc.astimezone(zone).date() + timedelta(days=1)
    return datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, 8, 0, tzinfo=zone
    ).astimezone(timezone.utc)


class ReminderScheduler:
    def __init__(self, bot: "TutorlaingBot", storage: "Storage", interval: int = 60):
        self.bot = bot
        self.storage = storage
        self.interval = interval
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="tutorlaing-reminders", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def tick(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        sent = 0
        for user in self.storage.due_reminder_users(current):
            chat_id = int(user["chat_id"])
            mode = str(user["reminder_mode"])
            next_at = next_reminder_at(mode, current, str(user["timezone"]))
            if next_at is None:
                continue
            if not is_delivery_time(current, str(user["timezone"])):
                self.storage.schedule_next_reminder(chat_id, next_at)
                continue
            if not self.storage.reserve_next_reminder(
                chat_id, str(user["reminder_next_at"]), current, next_at
            ):
                continue
            try:
                self.bot.send_scheduled_reminder(chat_id, mode)
                sent += 1
            except Exception:
                LOGGER.exception("Scheduled reminder failed for a user")
        return sent

    def _run(self) -> None:
        while not self._stopped.wait(self.interval):
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Reminder scheduler tick failed")
