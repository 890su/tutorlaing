from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_chat_ids(raw: str) -> frozenset[int] | None:
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if not values:
        return None
    try:
        return frozenset(int(value) for value in values)
    except ValueError as exc:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS must contain integer IDs") from exc


def _webhook_secret() -> str:
    direct = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    secret_file = os.environ.get("TELEGRAM_WEBHOOK_SECRET_FILE", "").strip()
    if direct and secret_file:
        raise ValueError(
            "Set TELEGRAM_WEBHOOK_SECRET or TELEGRAM_WEBHOOK_SECRET_FILE, not both"
        )
    if not secret_file:
        return direct
    try:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("Could not read TELEGRAM_WEBHOOK_SECRET_FILE") from exc


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_chat_ids: frozenset[int] | None
    data_dir: Path
    health_host: str
    health_port: int
    poll_timeout: int
    log_level: str
    telegram_webhook_url: str
    telegram_webhook_secret: str
    ai_provider: str = "none"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    ai_timeout: int = 45

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider == "gemini" and bool(self.gemini_api_key)

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        data_dir = Path(os.environ.get("DATA_DIR", "/data")).expanduser()
        webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
        webhook_secret = _webhook_secret()
        if bool(webhook_url) != bool(webhook_secret):
            raise ValueError(
                "TELEGRAM_WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET must be set together"
            )
        if webhook_url and not webhook_url.startswith("https://"):
            raise ValueError("TELEGRAM_WEBHOOK_URL must use https://")

        ai_provider = os.environ.get("AI_PROVIDER", "none").strip().lower()
        if ai_provider not in {"none", "gemini"}:
            raise ValueError("AI_PROVIDER must be 'none' or 'gemini'")
        gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if ai_provider == "gemini" and not gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when AI_PROVIDER=gemini")

        return cls(
            telegram_bot_token=token,
            allowed_chat_ids=_parse_chat_ids(
                os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
            ),
            data_dir=data_dir,
            health_host=os.environ.get("HEALTH_HOST", "0.0.0.0"),
            health_port=int(os.environ.get("HEALTH_PORT", "8080")),
            poll_timeout=max(5, min(50, int(os.environ.get("POLL_TIMEOUT", "25")))),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            telegram_webhook_url=webhook_url,
            telegram_webhook_secret=webhook_secret,
            ai_provider=ai_provider,
            gemini_api_key=gemini_api_key,
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip(),
            ai_timeout=max(10, min(120, int(os.environ.get("AI_TIMEOUT", "45")))),
        )
