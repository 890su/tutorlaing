from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
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
            exercise_id INTEGER REFERENCES exercise_bank(id) ON DELETE SET NULL,
            UNIQUE(drill_session_id, position)
        );

        CREATE TABLE IF NOT EXISTS exercise_bank (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL CHECK(scope IN ('global', 'user_private', 'curated')),
            owner_chat_id INTEGER REFERENCES users(chat_id) ON DELETE CASCADE,
            target_language TEXT NOT NULL,
            instruction_language TEXT NOT NULL,
            translation_language TEXT NOT NULL,
            learner_level TEXT NOT NULL,
            mode TEXT NOT NULL,
            scenario_id TEXT NOT NULL DEFAULT '',
            pack_title TEXT NOT NULL,
            pack_focus TEXT NOT NULL,
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
            source TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            quality_score REAL NOT NULL DEFAULT 0.6,
            use_count INTEGER NOT NULL DEFAULT 0,
            answer_count INTEGER NOT NULL DEFAULT 0,
            correct_count INTEGER NOT NULL DEFAULT 0,
            skip_count INTEGER NOT NULL DEFAULT 0,
            avg_score REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS exercise_tags (
            exercise_id INTEGER NOT NULL REFERENCES exercise_bank(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            PRIMARY KEY(exercise_id, tag)
        );

        CREATE TABLE IF NOT EXISTS learner_exercise_stats (
            chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
            exercise_id INTEGER NOT NULL REFERENCES exercise_bank(id) ON DELETE CASCADE,
            seen_count INTEGER NOT NULL DEFAULT 0,
            answer_count INTEGER NOT NULL DEFAULT 0,
            correct_count INTEGER NOT NULL DEFAULT 0,
            skip_count INTEGER NOT NULL DEFAULT 0,
            avg_score REAL NOT NULL DEFAULT 0,
            mastery_strength REAL NOT NULL DEFAULT 0,
            last_score REAL,
            last_seen_at TEXT,
            next_due_at TEXT,
            PRIMARY KEY(chat_id, exercise_id)
        );

        CREATE INDEX IF NOT EXISTS idx_reviews_due
            ON reviews(chat_id, status, due_at);
        CREATE INDEX IF NOT EXISTS idx_responses_session
            ON responses(session_id, phase, step_index);
        CREATE INDEX IF NOT EXISTS idx_ai_analyses_chat
            ON ai_analyses(chat_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_drill_sessions_chat
            ON drill_sessions(chat_id, status, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_exercise_bank_lookup
            ON exercise_bank(target_language, instruction_language, translation_language,
                             learner_level, mode, scenario_id, status);
        CREATE INDEX IF NOT EXISTS idx_exercise_tags_tag
            ON exercise_tags(tag, exercise_id);
        CREATE INDEX IF NOT EXISTS idx_learner_exercise_due
            ON learner_exercise_stats(chat_id, next_due_at);
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
            self._ensure_column("drill_items", "exercise_id", "INTEGER")
            self._ensure_column("exercise_bank", "provider", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("exercise_bank", "model", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                "exercise_bank", "prompt_version", "TEXT NOT NULL DEFAULT ''"
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

    def recent_ai_analyses(
        self, chat_id: int, target_language: str, limit: int = 5
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    """
                    SELECT * FROM ai_analyses
                    WHERE chat_id = ? AND operation = 'response_analysis'
                      AND target_language = ? AND status = 'completed'
                    ORDER BY id DESC LIMIT ?
                    """,
                    (chat_id, target_language, max(1, min(20, limit))),
                ).fetchall()
            )

    def problem_history(
        self, chat_id: int, target_language: str, limit: int = 12
    ) -> dict[str, list[dict[str, Any]]]:
        """Return recent low-scoring evidence without teaching-policy decisions."""

        bounded = max(1, min(50, limit))
        with self._lock:
            scenario_steps = list(
                self._connection.execute(
                    """
                    SELECT s.scenario_id, r.step_index,
                           MIN(r.score) AS worst_score,
                           COUNT(*) AS failures,
                           MAX(r.created_at) AS last_failed_at
                    FROM responses r
                    JOIN sessions s ON s.id = r.session_id
                    WHERE s.chat_id = ? AND s.target_language = ?
                      AND r.score < 0.6
                    GROUP BY s.scenario_id, r.step_index
                    ORDER BY last_failed_at DESC, failures DESC
                    LIMIT ?
                    """,
                    (chat_id, target_language, bounded),
                ).fetchall()
            )
            drill_items = list(
                self._connection.execute(
                    """
                    SELECT di.item_type, di.skill, di.prompt, di.context,
                           di.correct_answer, di.user_answer, di.score,
                           di.answered_at, ds.mode
                    FROM drill_items di
                    JOIN drill_sessions ds ON ds.id = di.drill_session_id
                    WHERE ds.chat_id = ? AND ds.target_language = ?
                      AND di.status = 'answered' AND COALESCE(di.score, 0) < 0.6
                    ORDER BY di.answered_at DESC
                    LIMIT ?
                    """,
                    (chat_id, target_language, bounded),
                ).fetchall()
            )
        return {
            "scenario_steps": [dict(row) for row in scenario_steps],
            "drill_items": [dict(row) for row in drill_items],
        }

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

    def exercise_candidates(
        self,
        chat_id: int,
        *,
        target_language: str,
        instruction_language: str,
        translation_language: str,
        learner_level: str,
        mode: str,
        scenario_id: str = "",
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        """Return reusable content; private material never crosses learner boundaries."""
        now = utc_now()
        with self._lock:
            return self._connection.execute(
                """
                SELECT exercise_bank.*, learner_exercise_stats.mastery_strength,
                       learner_exercise_stats.next_due_at,
                       learner_exercise_stats.last_seen_at,
                       learner_exercise_stats.seen_count AS learner_seen_count
                FROM exercise_bank
                LEFT JOIN learner_exercise_stats
                  ON learner_exercise_stats.exercise_id = exercise_bank.id
                 AND learner_exercise_stats.chat_id = ?
                WHERE exercise_bank.target_language = ?
                  AND exercise_bank.instruction_language = ?
                  AND exercise_bank.translation_language = ?
                  AND exercise_bank.learner_level = ?
                  AND exercise_bank.mode = ?
                  AND exercise_bank.scenario_id = ?
                  AND exercise_bank.status = 'active'
                  AND exercise_bank.quality_score >= 0.45
                  AND (
                    exercise_bank.scope IN ('global', 'curated')
                    OR (exercise_bank.scope = 'user_private' AND exercise_bank.owner_chat_id = ?)
                  )
                ORDER BY
                  CASE
                    WHEN learner_exercise_stats.next_due_at IS NOT NULL
                     AND learner_exercise_stats.next_due_at <= ? THEN 0
                    WHEN learner_exercise_stats.seen_count IS NULL THEN 1
                    ELSE 2
                  END,
                  COALESCE(learner_exercise_stats.mastery_strength, 0) ASC,
                  COALESCE(learner_exercise_stats.last_seen_at, '') ASC,
                  exercise_bank.quality_score DESC,
                  exercise_bank.use_count ASC
                LIMIT ?
                """,
                (
                    chat_id,
                    target_language,
                    instruction_language,
                    translation_language,
                    learner_level,
                    mode,
                    scenario_id,
                    chat_id,
                    now,
                    max(1, min(500, limit)),
                ),
            ).fetchall()

    def save_exercise_pack(
        self,
        chat_id: int,
        *,
        target_language: str,
        instruction_language: str,
        translation_language: str,
        learner_level: str,
        mode: str,
        scenario_id: str,
        title: str,
        focus: str,
        items: list[dict[str, Any]],
        source: str,
        private: bool,
        provider: str = "",
        model: str = "",
        prompt_version: str = "",
        tags: list[str] | None = None,
    ) -> list[int]:
        """Persist a validated pack and return stable IDs for drill attempts."""
        scope = (
            "user_private"
            if private
            else ("curated" if source == "fallback" else "global")
        )
        owner_chat_id = chat_id if private else None
        now = utc_now()
        base_tags = {
            f"mode:{mode}",
            f"target:{target_language}",
            f"level:{learner_level}",
            *(tags or []),
        }
        ids: list[int] = []
        with self._lock, self._connection:
            for item in items:
                canonical = {
                    "scope": scope,
                    "owner_chat_id": owner_chat_id,
                    "target_language": target_language,
                    "instruction_language": instruction_language,
                    "translation_language": translation_language,
                    "learner_level": learner_level,
                    "mode": mode,
                    "scenario_id": scenario_id,
                    "item": item,
                }
                content_hash = hashlib.sha256(
                    json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode(
                        "utf-8"
                    )
                ).hexdigest()
                quality = 0.75 if source == "fallback" else 0.6
                self._connection.execute(
                    """
                    INSERT INTO exercise_bank(
                        content_hash, scope, owner_chat_id, target_language,
                        instruction_language, translation_language, learner_level,
                        mode, scenario_id, pack_title, pack_focus, item_type, skill,
                        prompt, context, options_json, correct_answer,
                        accepted_answers_json, explanation, hint, difficulty,
                        source, provider, model, prompt_version, quality_score,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(content_hash) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        status = 'active'
                    """,
                    (
                        content_hash,
                        scope,
                        owner_chat_id,
                        target_language,
                        instruction_language,
                        translation_language,
                        learner_level,
                        mode,
                        scenario_id,
                        title,
                        focus,
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
                        source,
                        provider,
                        model,
                        prompt_version,
                        quality,
                        now,
                        now,
                    ),
                )
                row = self._connection.execute(
                    "SELECT id FROM exercise_bank WHERE content_hash = ?", (content_hash,)
                ).fetchone()
                exercise_id = int(row["id"])
                item_tags = base_tags | {
                    f"type:{item['type']}",
                    f"skill:{str(item['skill']).strip().lower()}",
                    f"difficulty:{int(item['difficulty'])}",
                }
                if scenario_id:
                    item_tags.add(f"scenario:{scenario_id}")
                self._connection.executemany(
                    "INSERT OR IGNORE INTO exercise_tags(exercise_id, tag) VALUES (?, ?)",
                    ((exercise_id, tag[:200]) for tag in sorted(item_tags) if tag),
                )
                ids.append(exercise_id)
        return ids

    def mark_exercises_used(self, chat_id: int, exercise_ids: list[int]) -> None:
        if not exercise_ids:
            return
        now = utc_now()
        with self._lock, self._connection:
            self._connection.executemany(
                """
                UPDATE exercise_bank SET use_count = use_count + 1,
                    last_used_at = ?, updated_at = ? WHERE id = ?
                """,
                ((now, now, exercise_id) for exercise_id in exercise_ids),
            )
            self._connection.executemany(
                """
                INSERT INTO learner_exercise_stats(chat_id, exercise_id, seen_count, last_seen_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(chat_id, exercise_id) DO UPDATE SET
                    seen_count = learner_exercise_stats.seen_count + 1,
                    last_seen_at = excluded.last_seen_at
                """,
                ((chat_id, exercise_id, now) for exercise_id in exercise_ids),
            )

    def _record_exercise_attempt_locked(
        self, chat_id: int, exercise_id: int, answer: str, score: float
    ) -> None:
        skipped = not answer.strip()
        now_dt = datetime.now(timezone.utc)
        current = self._connection.execute(
            "SELECT * FROM learner_exercise_stats WHERE chat_id = ? AND exercise_id = ?",
            (chat_id, exercise_id),
        ).fetchone()
        old_answers = int(current["answer_count"]) if current else 0
        old_average = float(current["avg_score"]) if current else 0.0
        old_mastery = float(current["mastery_strength"]) if current else 0.0
        new_average = ((old_average * old_answers) + score) / (old_answers + 1)
        mastery = (old_mastery * 0.7) + (score * 0.3)
        if skipped or score < 0.4:
            due_after = timedelta(days=1)
        elif score < 0.8:
            due_after = timedelta(days=3)
        elif mastery < 0.7:
            due_after = timedelta(days=7)
        else:
            due_after = timedelta(days=14)
        next_due = (now_dt + due_after).isoformat()
        self._connection.execute(
            """
            INSERT INTO learner_exercise_stats(
                chat_id, exercise_id, answer_count, correct_count, skip_count,
                avg_score, mastery_strength, last_score, last_seen_at, next_due_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, exercise_id) DO UPDATE SET
                answer_count = learner_exercise_stats.answer_count + 1,
                correct_count = learner_exercise_stats.correct_count + excluded.correct_count,
                skip_count = learner_exercise_stats.skip_count + excluded.skip_count,
                avg_score = excluded.avg_score,
                mastery_strength = excluded.mastery_strength,
                last_score = excluded.last_score,
                last_seen_at = excluded.last_seen_at,
                next_due_at = excluded.next_due_at
            """,
            (
                chat_id,
                exercise_id,
                int(score >= 0.6),
                int(skipped),
                new_average,
                mastery,
                score,
                now_dt.isoformat(),
                next_due,
            ),
        )
        global_row = self._connection.execute(
            "SELECT answer_count, avg_score FROM exercise_bank WHERE id = ?",
            (exercise_id,),
        ).fetchone()
        if global_row is not None:
            count = int(global_row["answer_count"])
            average = float(global_row["avg_score"])
            aggregate = ((average * count) + score) / (count + 1)
            self._connection.execute(
                """
                UPDATE exercise_bank SET answer_count = answer_count + 1,
                    correct_count = correct_count + ?, skip_count = skip_count + ?,
                    avg_score = ?, updated_at = ? WHERE id = ?
                """,
                (int(score >= 0.6), int(skipped), aggregate, now_dt.isoformat(), exercise_id),
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
                        accepted_answers_json, explanation, hint, difficulty, exercise_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        item.get("exercise_id"),
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
                """
                SELECT drill_items.drill_session_id, drill_items.status,
                       drill_items.exercise_id, drill_sessions.chat_id
                FROM drill_items
                JOIN drill_sessions ON drill_sessions.id = drill_items.drill_session_id
                WHERE drill_items.id = ?
                """,
                (item_id,),
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
            if row["exercise_id"] is not None:
                self._record_exercise_attempt_locked(
                    int(row["chat_id"]),
                    int(row["exercise_id"]),
                    answer,
                    max(0.0, min(1.0, float(score))),
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
                      AND stage IN (
                          'idle', 'waiting', 'scenario', 'practice', 'review', 'drill'
                      )
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

    def retry_failed_reminder(
        self, chat_id: int, expected_at: datetime, retry_at: datetime
    ) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE users SET reminder_next_at = ?, updated_at = ?
                WHERE chat_id = ? AND reminder_next_at = ?
                """,
                (
                    retry_at.isoformat(),
                    utc_now(),
                    chat_id,
                    expected_at.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def record_reminder_delivery(
        self, chat_id: int, outcome: str, mode: str
    ) -> None:
        self.event(
            chat_id,
            "reminder_delivery",
            {"outcome": outcome, "mode": mode},
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
