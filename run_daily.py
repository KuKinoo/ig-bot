"""Entry point of the scheduled workflow: build today's draft and send it to Telegram for approval."""
import logging

from config import settings
from services import telegram_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    settings.validate()
    telegram_bot.deliver_new_draft()
