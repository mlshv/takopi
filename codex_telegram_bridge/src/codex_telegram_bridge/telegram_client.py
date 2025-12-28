from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

class TelegramClient:
    """
    Minimal Telegram Bot API client using standard library (no requests dependency).
    """

    def __init__(self, token: str, timeout_s: int = 120) -> None:
        if not token:
            raise ValueError("Telegram token is empty")
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout_s = timeout_s

    def _call(self, method: str, params: Dict[str, Any]) -> Any:
        url = f"{self._base}/{method}"
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTPError {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Telegram URLError: {e}") from e

        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload["result"]

    def get_updates(
        self,
        offset: Optional[int],
        timeout_s: int = 50,
        allowed_updates: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timeout": timeout_s}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = allowed_updates
        return self._call("getUpdates", params)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        disable_notification: Optional[bool] = False,
        entities: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if disable_notification is not None:
            params["disable_notification"] = disable_notification
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        if entities is not None:
            params["entities"] = entities
        return self._call("sendMessage", params)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if entities is not None:
            params["entities"] = entities
        return self._call("editMessageText", params)

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        res = self._call("deleteMessage", params)
        return bool(res)
