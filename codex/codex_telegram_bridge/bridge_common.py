from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

TELEGRAM_HARD_LIMIT = 4096
DEFAULT_CHUNK_LEN = 3500  # leave room for formatting / safety


def _now_unix() -> int:
    return int(time.time())


def chunk_text(text: str, limit: int = DEFAULT_CHUNK_LEN) -> List[str]:
    """
    Telegram hard limit is 4096 chars. Chunk at newlines when possible.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]

    out: List[str] = []
    buf: List[str] = []
    size = 0

    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            # flush current buffer
            if buf:
                out.append("".join(buf))
                buf, size = [], 0
            # hard-split this long line
            for i in range(0, len(line), limit):
                out.append(line[i : i + limit])
            continue

        if size + len(line) > limit:
            out.append("".join(buf))
            buf, size = [line], len(line)
        else:
            buf.append(line)
            size += len(line)

    if buf:
        out.append("".join(buf))
    return out


class TelegramClient:
    """
    Minimal Telegram Bot API client using standard library (no requests dependency).

    Env:
      TELEGRAM_BOT_TOKEN
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
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        if len(text) > TELEGRAM_HARD_LIMIT:
            raise ValueError("send_message received too-long text; chunk it first")
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        return self._call("sendMessage", params)

    def send_message_chunked(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
        chunk_len: int = DEFAULT_CHUNK_LEN,
    ) -> List[Dict[str, Any]]:
        sent: List[Dict[str, Any]] = []
        chunks = chunk_text(text, limit=chunk_len)
        for i, c in enumerate(chunks):
            msg = self.send_message(
                chat_id=chat_id,
                text=c,
                reply_to_message_id=(reply_to_message_id if i == 0 else None),
                disable_notification=disable_notification,
            )
            sent.append(msg)
        return sent

    def send_chat_action(self, chat_id: int, action: str = "typing") -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "action": action,
        }
        return self._call("sendChatAction", params)


@dataclass(frozen=True)
class Route:
    route_type: str  # "exec" | "mcp" | "tmux"
    route_id: str    # session_id / conversationId / tmux target
    meta: Dict[str, Any]


class RouteStore:
    """
    Stores mapping: (chat_id, bot_message_id) -> route
    so Telegram replies can be routed.
    """

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS routes (
              chat_id INTEGER NOT NULL,
              bot_message_id INTEGER NOT NULL,
              route_type TEXT NOT NULL,
              route_id TEXT NOT NULL,
              meta_json TEXT,
              created_at INTEGER NOT NULL,
              PRIMARY KEY (chat_id, bot_message_id)
            );
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_routes_route_id ON routes(route_id);"
        )
        self._conn.commit()

    def link(
        self,
        chat_id: int,
        bot_message_id: int,
        route_type: str,
        route_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO routes(chat_id, bot_message_id, route_type, route_id, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (chat_id, bot_message_id, route_type, route_id, meta_json, _now_unix()),
        )
        self._conn.commit()

    def resolve(self, chat_id: int, bot_message_id: int) -> Optional[Route]:
        cur = self._conn.execute(
            """
            SELECT route_type, route_id, meta_json
            FROM routes
            WHERE chat_id = ? AND bot_message_id = ?
            """,
            (chat_id, bot_message_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        route_type, route_id, meta_json = row
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except json.JSONDecodeError:
            meta = {}
        return Route(route_type=route_type, route_id=route_id, meta=meta)

    def close(self) -> None:
        self._conn.close()


def parse_allowed_chat_ids(env_value: str) -> Optional[set[int]]:
    """
    Parse ALLOWED_CHAT_IDS="123,456"
    """
    v = (env_value or "").strip()
    if not v:
        return None
    out: set[int] = set()
    for part in v.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out
