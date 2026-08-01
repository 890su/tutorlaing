import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tutorlaing.config import Settings


class ConfigTests(unittest.TestCase):
    def test_webhook_secret_can_be_loaded_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_path = Path(temp_dir) / "secret"
            secret_path.write_text("safe-test-secret\n", encoding="utf-8")
            environment = {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_WEBHOOK_URL": "https://example.com/telegram/webhook",
                "TELEGRAM_WEBHOOK_SECRET_FILE": str(secret_path),
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env()
            self.assertEqual("safe-test-secret", settings.telegram_webhook_secret)

    def test_partial_webhook_configuration_is_rejected(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_WEBHOOK_URL": "https://example.com/telegram/webhook",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_gemini_provider_requires_key(self) -> None:
        environment = {"TELEGRAM_BOT_TOKEN": "test-token", "AI_PROVIDER": "gemini"}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_gemini_provider_is_enabled_with_key(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "AI_PROVIDER": "gemini",
            "GEMINI_API_KEY": "test-key",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.ai_enabled)
        self.assertEqual("gemini-2.5-pro", settings.gemini_model)


if __name__ == "__main__":
    unittest.main()
