from __future__ import annotations

import logging
import signal
import threading

from .app import TutorlaingBot
from .config import Settings
from .health import start_health_server
from .storage import Storage


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    storage = Storage(settings.data_dir / "tutorlaing.sqlite3")
    bot = TutorlaingBot(settings, storage)
    webhook_mode = bool(settings.telegram_webhook_url)
    health_server, _ = start_health_server(
        storage,
        settings.health_host,
        settings.health_port,
        webhook_handler=bot.handle_update if webhook_mode else None,
        webhook_secret=settings.telegram_webhook_secret,
    )
    stopped = threading.Event()

    def stop(*_: object) -> None:
        bot.running = False
        stopped.set()
        health_server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if webhook_mode:
            bot.configure_webhook()
            stopped.wait()
        else:
            bot.run_polling()
    finally:
        health_server.server_close()
        storage.close()


if __name__ == "__main__":
    main()
