from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import CONSENT_VERSION


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


ACTIVITY_FIELDS = (
    "stage",
    "current_scenario",
    "current_step",
    "current_session",
    "current_review",
    "current_drill",
    "pending_assignment",
)


class Storage:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            first_name TEXT NOT NULL DEFAULT '',
            consent_at TEXT,
            stage TEXT NOT NULL DEFAULT 'new',
            current_scenario TEXT,
            current_step INTEGER NOT NULL DEFAULT 0,
            current_session TEXT,
            current_review INTEGER,
            current_drill TEXT,
            toolkit_input_mode TEXT,
            suspended_activity_json TEXT,
            pending_assignment TEXT,
            workspace_message_id INTEGER,
            consent_version INTEGER NOT NULL DEFAULT 0,
            instruction_language TEXT NOT NULL DEFAULT 'ru',
            translation_language TEXT NOT NULL DEFAULT 'ru',
            target_language TEXT NOT NULL DEFAULT 'pl',
            learner_level TEXT NOT NULL DEFAULT 'A1',
            reminder_mode TEXT NOT NULL DEFAULT 'off',
            reminder_next_at TEXT,
            reminder_paused_until TEXT,
            last_reminder_at TEXT,
            timezone TEXT NOT NULL DEFAULT 'Europe/Warsaw',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
            scenario_id TEXT NOT NULL,
            target_language TEXT NOT NULL DEFAULT 'pl',
            status TEXT NOT NULL,
            bottleneck_step INTEGER,
            average_score REAL,
            started_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            step_index INTEGER NOT NULL,
            phase TEXT NOT NULL,
            response_text TEXT NOT NULL,
            score REAL NOT NULL,
            missing_groups TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
            scenario_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            target_language TEXT NOT NULL DEFAULT 'pl',
            due_at TEXT NOT NULL,
            interval_days INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            score REAL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS real_world_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            event_name TEXT NOT NULL,
            properties TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
            session_id TEXT,
            scenario_id TEXT,
            step_index INTEGER,
            operation TEXT NOT NULL,
            target_language TEXT,
            source_text TEXT NOT NULL,
            result_json TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            usage_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS telegram_updates (
            update_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS drill_sessions (
            id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
            target_language TEXT NOT NULL DEFAULT 'pl',
            mode TEXT NOT NULL DEFAULT 'adaptive',
            source_analysis_id INTEGER,
            title TEXT NOT NULL,
            focus TEXT NOT NULL,
            status TEXT NOT NULL,
            current_index INTEGER NOT NULL DEFAULT 0,
            total_items INTEGER NOT NULL,
            correct_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS drill_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drill_session_id TEXT NOT NULL REFERENCES drill_sessions(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            skill TEXT NOT NULL,
            prompt TEXT NOT NULL,
            context TEXT NOT NULL,
            options_json TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            accepted_answers_json TEXT NOT NULL,
            explanation TEXT NOT NULL,
            hint TEXT NOT NULL,
            difficulty INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            user_answer TEXT,
            score REAL,
            answered_at TEXT,
            UNIQUE(drill_session_id, position)
        );

        CREATE INDEX IF NOT EXISTS idx_reviews_due
            ON reviews(chat_id, status, due_at);
        CREATE INDEX IF NOT EXISTS idx_responses_session
            ON responses(session_id, phase, step_index);
        CREATE INDEX IF NOT EXISTS idx_ai_analyses_chat
            ON ai_analyses(chat_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_drill_sessions_chat
            ON drill_sessions(chat_id, status, started_at DESC);
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            self._ensure_column("users", "consent_version", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(
                "users", "instruction_language", "TEXT NOT NULL DEFAULT 'ru'"
            )
            self._ensure_column(
                "users", "translation_language", "TEXT NOT NULL DEFAULT 'ru'"
            )
            self._ensure_column(
                "users", "target_language", "TEXT NOT NULL DEFAULT 'pl'"
            )
            self._ensure_column("users", "current_drill", "TEXT")
            self._ensure_column("users", "toolkit_input_mode", "TEXT")
            self._ensure_column("users", "suspended_activity_json", "TEXT")
            self._ensure_column("users", "pending_assignment", "TEXT")
            self._ensure_column("users", "workspace_message_id", "INTEGER")
            self._ensure_column(
                "users", "learner_level", "TEXT NOT NULL DEFAULT 'A1'"
            )
            self._ensure_column(
                "users", "reminder_mode", "TEXT NOT NULL DEFAULT 'off'"
            )
            self._ensure_column("users", "reminder_next_at", "TEXT")
            self._ensure_column("users", "reminder_paused_until", "TEXT")
            self._ensure_column("users", "last_reminder_at", "TEXT")
            self._ensure_column(
                "users", "timezone", "TEXT NOT NULL DEFAULT 'Europe/Warsaw'"
            )
            self._ensure_column("ai_analyses", "target_language", "TEXT")
            self._ensure_column(
                "reviews", "target_language", "TEXT NOT NULL DEFAULT 'pl'"
            )
            self._ensure_column(
                "drill_sessions", "target_language", "TEXT NOT NULL DEFAULT 'pl'"
            )
            self._ensure_column(
                "drill_sessions", "mode", "TEXT NOT NULL DEFAULT 'adaptive'"
            )
            self._ensure_column(
                "sessions", "target_language", "TEXT NOT NULL DEFAULT 'pl'"
            )
            self._connection.execute(
                "UPDATE users SET stage = 'idle' WHERE stage = 'toolkit_input'"
            )
            self._connection.execute(
                "UPDATE ai_analyses SET target_language = 'pl' "
                "WHERE target_language IS NULL AND operation = 'response_analysis'"
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def ensure_user(self, chat_id: int, first_name: str = "") -> sqlite3.Row:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO users(chat_id, first_name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    updated_at = excluded.updated_at
                """,
                (chat_id, first_name, now, now),
            )
        return self.get_user(chat_id)

    def get_user(self, chat_id: int) -> sqlite3.Row:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown user: {chat_id}")
        return row

    def accept_consent(self, chat_id: int, version: int = CONSENT_VERSION) -> None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET consent_at = ?, consent_version = ?, stage = 'idle', updated_at = ? WHERE chat_id = ?",
                (now, version, now, chat_id),
            )
        self.event(chat_id, "onboarding_completed", {"consent_version": version})

    def set_language(self, chat_id: int, field: str, language: str) -> None:
        allowed = {
            "instruction_language",
            "translation_language",
            "target_language",
        }
        if field not in allowed:
            raise ValueError(f"Unsupported language field: {field}")
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE users SET {field} = ?, updated_at = ? WHERE chat_id = ?",
                (language, utc_now(), chat_id),
            )
        self.event(chat_id, "language_setting_changed", {"field": field, "value": language})

    def set_learner_level(self, chat_id: int, level: str) -> None:
        if level not in {"A0", "A1", "A2", "B1", "B2", "C1"}:
            raise ValueError(f"Unsupported learner level: {level}")
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET learner_level = ?, updated_at = ? WHERE chat_id = ?",
                (level, utc_now(), chat_id),
            )
        self.event(chat_id, "learner_level_changed", {"level": level})

    def set_user_state(self, chat_id: int, **values: Any) -> None:
        allowed = {
            "stage",
            "current_scenario",
            "current_step",
            "current_session",
            "current_review",
            "current_drill",
            "toolkit_input_mode",
            "suspended_activity_json",
            "pending_assignment",
            "workspace_message_id",
        }
        invalid = set(values) - allowed
        if invalid:
            raise ValueError(f"Unsupported user state fields: {sorted(invalid)}")
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        parameters = [*values.values(), chat_id]
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE users SET {assignments} WHERE chat_id = ?", parameters
            )

    def suspend_activity(self, chat_id: int) -> bool:
        """Move the current learning activity aside for a temporary tool drill."""
        with self._lock, self._connection:
            user = self._connection.execute(
                "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if user is None:
                raise KeyError(f"Unknown user: {chat_id}")
            if user["suspended_activity_json"] or user["stage"] in {"idle", "new"}:
                return False
            snapshot = {field: user[field] for field in ACTIVITY_FIELDS}
            self._connection.execute(
                """
                UPDATE users
                SET stage = 'idle', current_scenario = NULL, current_step = 0,
                    current_session = NULL, current_review = NULL,
                    current_drill = NULL, pending_assignment = NULL,
                    toolkit_input_mode = NULL, suspended_activity_json = ?,
                    updated_at = ?
                WHERE chat_id = ?
                """,
                (json.dumps(snapshot, ensure_ascii=False), utc_now(), chat_id),
            )
        self.event(chat_id, "activity_suspended", {"stage": snapshot["stage"]})
        return True

    def restore_suspended_activity(self, chat_id: int) -> bool:
        """Restore an activity after a temporary tool drill completes or stops."""
        with self._lock, self._connection:
            stage = self._restore_suspended_activity_locked(chat_id)
            if stage is None:
                return False
        self.event(chat_id, "activity_restored", {"stage": stage})
        return True

    def _restore_suspended_activity_locked(self, chat_id: int) -> str | None:
        user = self._connection.execute(
            "SELECT suspended_activity_json FROM users WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if user is None:
            raise KeyError(f"Unknown user: {chat_id}")
        if not user["suspended_activity_json"]:
            return None
        snapshot = json.loads(str(user["suspended_activity_json"]))
        values = [snapshot.get(field) for field in ACTIVITY_FIELDS]
        assignments = ", ".join(f"{field} = ?" for field in ACTIVITY_FIELDS)
        self._connection.execute(
            f"""
            UPDATE users SET {assignments}, suspended_activity_json = NULL,
                updated_at = ? WHERE chat_id = ?
            """,
            (*values, utc_now(), chat_id),
        )
        return str(snapshot.get("stage") or "idle")

    def start_session(self, chat_id: int, scenario_id: str) -> str:
        session_id = str(uuid.uuid4())
        now = utc_now()
        target_language = str(self.get_user(chat_id)["target_language"])
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE sessions
                SET status = 'abandoned', completed_at = ?
                WHERE chat_id = ? AND status = 'active'
                """,
                (now, chat_id),
            )
            self._connection.execute(
                "INSERT INTO sessions(id, chat_id, scenario_id, target_language, status, started_at) VALUES (?, ?, ?, ?, 'active', ?)",
                (session_id, chat_id, scenario_id, target_language, now),
            )
        self.set_user_state(
            chat_id,
            stage="scenario",
            current_scenario=scenario_id,
            current_step=0,
            current_session=session_id,
            current_review=None,
            pending_assignment=None,
        )
        self.event(chat_id, "scenario_started", {"scenario_id": scenario_id})
        return session_id

    def abandon_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE sessions
                SET status = 'abandoned', completed_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (utc_now(), session_id),
            )

    def add_response(
        self,
        session_id: str,
        step_index: int,
        phase: str,
        response_text: str,
        score: float,
        missing_groups: tuple[int, ...],
    ) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO responses(
                    session_id, step_index, phase, response_text, score,
                    missing_groups, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    step_index,
                    phase,
                    response_text,
                    score,
                    json.dumps(missing_groups),
                    utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def add_ai_analysis(
        self,
        chat_id: int,
        operation: str,
        source_text: str,
        result: dict[str, Any],
        provider: str,
        model: str,
        prompt_version: str,
        latency_ms: int,
        usage: dict[str, Any] | None = None,
        status: str = "completed",
        session_id: str | None = None,
        scenario_id: str | None = None,
        step_index: int | None = None,
        target_language: str | None = None,
    ) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO ai_analyses(
                    chat_id, session_id, scenario_id, step_index, operation, target_language,
                    source_text, result_json, provider, model, prompt_version,
                    latency_ms, usage_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    session_id,
                    scenario_id,
                    step_index,
                    operation,
                    target_language,
                    source_text,
                    json.dumps(result, ensure_ascii=False),
                    provider,
                    model,
                    prompt_version,
                    latency_ms,
                    json.dumps(usage or {}, ensure_ascii=False),
                    status,
                    utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def get_ai_analysis(self, analysis_id: int, chat_id: int) -> sqlite3.Row:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM ai_analyses WHERE id = ? AND chat_id = ?",
                (analysis_id, chat_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown AI analysis: {analysis_id}")
        return row

    def latest_ai_analysis(
        self, chat_id: int, target_language: str | None = None
    ) -> sqlite3.Row | None:
        language_filter = " AND target_language = ?" if target_language else ""
        parameters: tuple[Any, ...] = (
            (chat_id, target_language) if target_language else (chat_id,)
        )
        with self._lock:
            return self._connection.execute(
                f"""
                SELECT * FROM ai_analyses
                WHERE chat_id = ? AND operation = 'response_analysis'
                {language_filter}
                ORDER BY id DESC LIMIT 1
                """,
                parameters,
            ).fetchone()

    def claim_update(self, update_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO telegram_updates(update_id, status, created_at) VALUES (?, 'processing', ?)",
                (update_id, utc_now()),
            )
        return cursor.rowcount == 1

    def complete_update(self, update_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE telegram_updates SET status = 'completed', completed_at = ? WHERE update_id = ?",
                (utc_now(), update_id),
            )

    def release_update(self, update_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM telegram_updates WHERE update_id = ? AND status = 'processing'",
                (update_id,),
            )

    def start_drill(
        self,
        chat_id: int,
        source_analysis_id: int | None,
        title: str,
        focus: str,
        items: list[dict[str, Any]],
        mode: str = "adaptive",
        replace_active: bool = True,
    ) -> str:
        drill_id = str(uuid.uuid4())
        now = utc_now()
        target_language = str(self.get_user(chat_id)["target_language"])
        with self._lock, self._connection:
            if not replace_active:
                current = self._connection.execute(
                    "SELECT current_drill FROM users WHERE chat_id = ?", (chat_id,)
                ).fetchone()
                if current is None:
                    raise KeyError(f"Unknown user: {chat_id}")
                if current["current_drill"]:
                    return str(current["current_drill"])
            if replace_active:
                self._connection.execute(
                    "UPDATE drill_sessions SET status = 'abandoned', completed_at = ? WHERE chat_id = ? AND status = 'active'",
                    (now, chat_id),
                )
            self._connection.execute(
                """
                INSERT INTO drill_sessions(
                    id, chat_id, source_analysis_id, title, focus, target_language, mode, status,
                    total_items, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    drill_id,
                    chat_id,
                    source_analysis_id,
                    title,
                    focus,
                    target_language,
                    mode,
                    len(items),
                    now,
                ),
            )
            for position, item in enumerate(items):
                self._connection.execute(
                    """
                    INSERT INTO drill_items(
                        drill_session_id, position, item_type, skill, prompt,
                        context, options_json, correct_answer,
                        accepted_answers_json, explanation, hint, difficulty
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        drill_id,
                        position,
                        item["type"],
                        item["skill"],
                        item["prompt"],
                        item["context"],
                        json.dumps(item.get("options", []), ensure_ascii=False),
                        item["correct_answer"],
                        json.dumps(item.get("accepted_answers", []), ensure_ascii=False),
                        item["explanation"],
                        item["hint"],
                        int(item["difficulty"]),
                    ),
                )
            self._connection.execute(
                "UPDATE users SET stage = 'drill', current_drill = ?, updated_at = ? WHERE chat_id = ?",
                (drill_id, now, chat_id),
            )
        self.event(
            chat_id,
            "drill_started",
            {"drill_id": drill_id, "items": len(items), "mode": mode},
        )
        return drill_id

    def drill_session(self, drill_id: str, chat_id: int | None = None) -> sqlite3.Row:
        query = "SELECT * FROM drill_sessions WHERE id = ?"
        parameters: tuple[Any, ...] = (drill_id,)
        if chat_id is not None:
            query += " AND chat_id = ?"
            parameters = (drill_id, chat_id)
        with self._lock:
            row = self._connection.execute(query, parameters).fetchone()
        if row is None:
            raise KeyError(f"Unknown drill: {drill_id}")
        return row

    def active_drill(self, chat_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                """
                SELECT drill_sessions.* FROM drill_sessions
                JOIN users ON users.current_drill = drill_sessions.id
                WHERE users.chat_id = ? AND drill_sessions.status = 'active'
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()

    def drill_item(self, drill_id: str, position: int) -> sqlite3.Row:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM drill_items WHERE drill_session_id = ? AND position = ?",
                (drill_id, position),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown drill item: {drill_id}/{position}")
        return row

    def answer_drill_item(self, item_id: int, answer: str, score: float) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT drill_session_id, status FROM drill_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise KeyError(f"Drill item unavailable: {item_id}")
            self._connection.execute(
                "UPDATE drill_items SET status = 'answered', user_answer = ?, score = ?, answered_at = ? WHERE id = ?",
                (answer, score, utc_now(), item_id),
            )
            if score >= 0.6:
                self._connection.execute(
                    "UPDATE drill_sessions SET correct_count = correct_count + 1 WHERE id = ?",
                    (row["drill_session_id"],),
                )

    def advance_drill(self, drill_id: str, chat_id: int) -> bool:
        session = self.drill_session(drill_id, chat_id)
        next_index = int(session["current_index"]) + 1
        if next_index >= int(session["total_items"]):
            with self._lock, self._connection:
                self._connection.execute(
                    "UPDATE drill_sessions SET status = 'completed', completed_at = ? WHERE id = ?",
                    (utc_now(), drill_id),
                )
                restored_stage = self._restore_suspended_activity_locked(chat_id)
                if restored_stage is None:
                    self._connection.execute(
                        "UPDATE users SET stage = 'idle', current_drill = NULL, updated_at = ? WHERE chat_id = ?",
                        (utc_now(), chat_id),
                    )
            self.event(chat_id, "drill_completed", {"drill_id": drill_id})
            if restored_stage is not None:
                self.event(chat_id, "activity_restored", {"stage": restored_stage})
            return False
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE drill_sessions SET current_index = ? WHERE id = ?",
                (next_index, drill_id),
            )
        return True

    def abandon_drill(self, drill_id: str, chat_id: int) -> None:
        self.drill_session(drill_id, chat_id)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE drill_sessions SET status = 'abandoned', completed_at = ? WHERE id = ? AND status = 'active'",
                (utc_now(), drill_id),
            )
            restored_stage = self._restore_suspended_activity_locked(chat_id)
            if restored_stage is None:
                self._connection.execute(
                    "UPDATE users SET stage = 'idle', current_drill = NULL, updated_at = ? WHERE chat_id = ?",
                    (utc_now(), chat_id),
                )
        self.event(chat_id, "drill_abandoned", {"drill_id": drill_id})
        if restored_stage is not None:
            self.event(chat_id, "activity_restored", {"stage": restored_stage})

    def set_reminder_mode(
        self, chat_id: int, mode: str, next_at: datetime | None
    ) -> None:
        if mode not in {"off", "gentle", "normal", "intensive", "aggressive"}:
            raise ValueError(f"Unsupported reminder mode: {mode}")
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE users SET reminder_mode = ?, reminder_next_at = ?,
                    reminder_paused_until = NULL, updated_at = ?
                WHERE chat_id = ?
                """,
                (mode, next_at.isoformat() if next_at else None, utc_now(), chat_id),
            )
        self.event(chat_id, "reminder_mode_changed", {"mode": mode})

    def pause_reminders(self, chat_id: int, until: datetime) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET reminder_paused_until = ?, updated_at = ? WHERE chat_id = ?",
                (until.isoformat(), utc_now(), chat_id),
            )
        self.event(chat_id, "reminders_paused", {"until": until.isoformat()})

    def due_reminder_users(self, now: datetime) -> list[sqlite3.Row]:
        current = now.isoformat()
        with self._lock:
            return list(
                self._connection.execute(
                    """
                    SELECT * FROM users
                    WHERE consent_version >= ?
                      AND reminder_mode != 'off'
                      AND reminder_next_at IS NOT NULL
                      AND reminder_next_at <= ?
                      AND (reminder_paused_until IS NULL OR reminder_paused_until <= ?)
                      AND toolkit_input_mode IS NULL
                      AND stage IN ('idle', 'drill', 'waiting')
                    ORDER BY reminder_next_at ASC
                    """,
                    (CONSENT_VERSION, current, current),
                ).fetchall()
            )

    def reserve_next_reminder(
        self, chat_id: int, expected_at: str, sent_at: datetime, next_at: datetime
    ) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE users SET last_reminder_at = ?, reminder_next_at = ?
                WHERE chat_id = ? AND reminder_next_at = ?
                """,
                (sent_at.isoformat(), next_at.isoformat(), chat_id, expected_at),
            )
        return cursor.rowcount == 1

    def schedule_next_reminder(
        self, chat_id: int, next_at: datetime | None
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET reminder_next_at = ?, updated_at = ? WHERE chat_id = ?",
                (next_at.isoformat() if next_at else None, utc_now(), chat_id),
            )

    def queue_assignment(self, chat_id: int, assignment: str) -> None:
        self.set_user_state(
            chat_id, stage="waiting", pending_assignment=assignment
        )
        self.event(chat_id, "assignment_queued", {"assignment": assignment})

    def claim_pending_assignment(self, chat_id: int) -> str | None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT pending_assignment FROM users WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row is None or not row["pending_assignment"]:
                return None
            assignment = str(row["pending_assignment"])
            self._connection.execute(
                "UPDATE users SET pending_assignment = NULL, updated_at = ? WHERE chat_id = ?",
                (utc_now(), chat_id),
            )
        return assignment

    def scenario_scores(self, session_id: str) -> list[tuple[int, float]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT step_index, MIN(score) AS score
                FROM responses
                WHERE session_id = ? AND phase = 'scenario'
                GROUP BY step_index
                ORDER BY step_index
                """,
                (session_id,),
            ).fetchall()
        return [(int(row["step_index"]), float(row["score"])) for row in rows]

    def response_count(self, session_id: str, phase: str) -> int:
        with self._lock:
            return int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM responses WHERE session_id = ? AND phase = ?",
                    (session_id, phase),
                ).fetchone()[0]
            )

    def complete_session(
        self, session_id: str, bottleneck_step: int, average_score: float
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE sessions
                SET status = 'completed', bottleneck_step = ?, average_score = ?, completed_at = ?
                WHERE id = ?
                """,
                (bottleneck_step, average_score, utc_now(), session_id),
            )
        row = self.session(session_id)
        self.event(
            int(row["chat_id"]),
            "scenario_completed",
            {"scenario_id": row["scenario_id"], "average_score": average_score},
        )

    def session(self, session_id: str) -> sqlite3.Row:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown session: {session_id}")
        return row

    def schedule_review(
        self,
        chat_id: int,
        scenario_id: str,
        step_index: int,
        due_at: datetime,
        interval_days: int,
    ) -> int:
        target_language = str(self.get_user(chat_id)["target_language"])
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO reviews(
                    chat_id, scenario_id, step_index, target_language, due_at,
                    interval_days, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    scenario_id,
                    step_index,
                    target_language,
                    due_at.isoformat(),
                    interval_days,
                    utc_now(),
                ),
            )
            review_id = int(cursor.lastrowid)
        self.event(chat_id, "review_scheduled", {"review_id": review_id})
        return review_id

    def pending_reviews(self, chat_id: int, include_future: bool = False) -> list[sqlite3.Row]:
        target_language = str(self.get_user(chat_id)["target_language"])
        query = (
            "SELECT * FROM reviews WHERE chat_id = ? AND target_language = ? "
            "AND status = 'pending'"
        )
        parameters: list[Any] = [chat_id, target_language]
        if not include_future:
            query += " AND due_at <= ?"
            parameters.append(utc_now())
        query += " ORDER BY due_at ASC"
        with self._lock:
            return list(self._connection.execute(query, parameters).fetchall())

    def get_review(self, review_id: int, chat_id: int) -> sqlite3.Row:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM reviews WHERE id = ? AND chat_id = ?",
                (review_id, chat_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown review: {review_id}")
        return row

    def complete_review(self, review_id: int, score: float) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE reviews SET status = 'completed', score = ?, completed_at = ? WHERE id = ?",
                (score, utc_now(), review_id),
            )

    def add_outcome(self, chat_id: int, session_id: str, result: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO real_world_outcomes(session_id, chat_id, result, created_at) VALUES (?, ?, ?, ?)",
                (session_id, chat_id, result, utc_now()),
            )
        self.event(chat_id, "real_world_outcome_reported", {"result": result})

    def event(
        self, chat_id: int | None, event_name: str, properties: dict[str, Any] | None = None
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events(chat_id, event_name, properties, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, event_name, json.dumps(properties or {}, ensure_ascii=False), utc_now()),
            )

    def delete_user(self, chat_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))

    def health(self) -> dict[str, Any]:
        with self._lock:
            self._connection.execute("SELECT 1").fetchone()
            user_count = int(
                self._connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            )
        return {"database": "ok", "users": user_count}

    def progress_evidence(self, chat_id: int) -> dict[str, Any]:
        user = self.get_user(chat_id)
        target_language = str(user["target_language"])
        with self._lock:
            sessions = list(
                self._connection.execute(
                    """
                    SELECT scenario_id, COUNT(*) AS attempts,
                           MAX(COALESCE(average_score, 0)) AS best_score
                    FROM sessions
                    WHERE chat_id = ? AND target_language = ? AND status = 'completed'
                    GROUP BY scenario_id
                    """,
                    (chat_id, target_language),
                ).fetchall()
            )
            reviews = list(
                self._connection.execute(
                    """
                    SELECT scenario_id,
                           SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                           MAX(CASE WHEN status = 'completed' THEN COALESCE(score, 0) ELSE 0 END) AS best_score,
                           SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
                    FROM reviews
                    WHERE chat_id = ? AND target_language = ?
                    GROUP BY scenario_id
                    """,
                    (chat_id, target_language),
                ).fetchall()
            )
            drills = self._connection.execute(
                """
                SELECT COUNT(*) AS completed,
                       COALESCE(AVG(CASE WHEN status = 'completed' THEN correct_count * 1.0 / total_items END), 0) AS average
                FROM drill_sessions
                WHERE chat_id = ? AND target_language = ? AND status = 'completed'
                """,
                (chat_id, target_language),
            ).fetchone()
        return {
            "level": str(user["learner_level"]),
            "target_language": target_language,
            "sessions": [dict(row) for row in sessions],
            "reviews": [dict(row) for row in reviews],
            "completed_drills": int(drills["completed"] or 0),
            "drill_average": float(drills["average"] or 0),
        }
