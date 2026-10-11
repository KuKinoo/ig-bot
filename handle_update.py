"""Entry point of the Telegram workflow: process one update forwarded by the Cloudflare Worker."""
import json
import logging
import os

from config import settings
from services import telegram_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    settings.validate()
    raw = os.environ.get("TELEGRAM_UPDATE", "").strip()
    if not raw:
        raise SystemExit("TELEGRAM_UPDATE is empty - nothing to do.")
    telegram_bot.process_update(json.loads(raw))
