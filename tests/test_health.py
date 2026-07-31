import json
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from tutorlaing.health import start_health_server
from tutorlaing.storage import Storage


class HealthTests(unittest.TestCase):
    def test_health_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "health.sqlite3")
            server, _ = start_health_server(storage, "127.0.0.1", 0)
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as response:
                    payload = json.loads(response.read())
                    status = response.status
                self.assertEqual(200, status)
                self.assertEqual("ok", payload["status"])
            finally:
                server.shutdown()
                server.server_close()
                storage.close()

    def test_webhook_requires_secret_and_dispatches_update(self) -> None:
        received = []
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "webhook.sqlite3")
            server, _ = start_health_server(
                storage,
                "127.0.0.1",
                0,
                webhook_handler=received.append,
                webhook_secret="test-secret",
            )
            try:
                port = server.server_address[1]
                url = f"http://127.0.0.1:{port}/telegram/webhook"
                unauthorized = urllib.request.Request(
                    url,
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(unauthorized, timeout=2)
                self.assertEqual(403, context.exception.code)

                request = urllib.request.Request(
                    url,
                    data=b'{"update_id": 1}',
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Telegram-Bot-Api-Secret-Token": "test-secret",
                    },
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(200, response.status)
                self.assertEqual([{"update_id": 1}], received)
            finally:
                server.shutdown()
                server.server_close()
                storage.close()


if __name__ == "__main__":
    unittest.main()
