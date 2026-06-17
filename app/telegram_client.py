from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import requests


class TelegramClient:
    def __init__(self, bot_token: Optional[str] = None) -> None:
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or "8494126901:AAE0fbTFsQosqG1YpoGjx9SkIM41PzB64RQ"
        self.base = f"https://api.telegram.org/bot{self.bot_token}"
        self.file_base = f"https://api.telegram.org/file/bot{self.bot_token}"

    def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if offset is not None:
            params["offset"] = int(offset)
        if timeout:
            params["timeout"] = int(timeout)
        resp = requests.get(f"{self.base}/getUpdates", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_file(self, file_id: str) -> Dict[str, Any]:
        resp = requests.get(f"{self.base}/getFile", params={"file_id": file_id}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def build_file_url(self, file_path: str) -> str:
        return f"{self.file_base}/{file_path}"

    def send_animation(self, chat_id_or_username: str | int, animation: str, caption: Optional[str] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "chat_id": chat_id_or_username,
            "animation": animation,
        }
        if caption:
            data["caption"] = caption
        resp = requests.post(f"{self.base}/sendAnimation", data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()
