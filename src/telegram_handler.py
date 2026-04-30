from __future__ import annotations

import logging
import requests

logger = logging.getLogger(__name__)


class TelegramHandler:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        if not self.token or not self.chat_id:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    def command_handlers(self) -> list[str]:
        return ["/status", "/pause", "/resume", "/risk", "/positions", "/orders"]
