from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
            scenario_id TEXT NOT NULL,
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

        CREATE INDEX IF NOT EXISTS idx_reviews_due
            ON reviews(chat_id, status, due_at);
        CREATE INDEX IF NOT EXISTS idx_responses_session
            ON responses(session_id, phase, step_index);
        CREATE INDEX IF NOT EXISTS idx_ai_analyses_chat
            ON ai_analyses(chat_id, id DESC);
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

    def accept_consent(self, chat_id: int, version: int = 2) -> None:
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

    def set_user_state(self, chat_id: int, **values: Any) -> None:
        allowed = {
            "stage",
            "current_scenario",
            "current_step",
            "current_session",
            "current_review",
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

    def start_session(self, chat_id: int, scenario_id: str) -> str:
        session_id = str(uuid.uuid4())
        now = utc_now()
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
                "INSERT INTO sessions(id, chat_id, scenario_id, status, started_at) VALUES (?, ?, ?, 'active', ?)",
                (session_id, chat_id, scenario_id, now),
            )
        self.set_user_state(
            chat_id,
            stage="scenario",
            current_scenario=scenario_id,
            current_step=0,
            current_session=session_id,
            current_review=None,
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
    ) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO ai_analyses(
                    chat_id, session_id, scenario_id, step_index, operation,
                    source_text, result_json, provider, model, prompt_version,
                    latency_ms, usage_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    session_id,
                    scenario_id,
                    step_index,
                    operation,
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

    def latest_ai_analysis(self, chat_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                """
                SELECT * FROM ai_analyses
                WHERE chat_id = ? AND operation = 'response_analysis'
                ORDER BY id DESC LIMIT 1
                """,
                (chat_id,),
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
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO reviews(
                    chat_id, scenario_id, step_index, due_at,
                    interval_days, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    scenario_id,
                    step_index,
                    due_at.isoformat(),
                    interval_days,
                    utc_now(),
                ),
            )
            review_id = int(cursor.lastrowid)
        self.event(chat_id, "review_scheduled", {"review_id": review_id})
        return review_id

    def pending_reviews(self, chat_id: int, include_future: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM reviews WHERE chat_id = ? AND status = 'pending'"
        parameters: list[Any] = [chat_id]
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
