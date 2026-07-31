import json
import tempfile
import unittest
import urllib.request
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


if __name__ == "__main__":
    unittest.main()
