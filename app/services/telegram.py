from __future__ import annotations

from typing import Any

import httpx

from ..config import get_settings


class TelegramService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = f"https://api.telegram.org/bot{self.settings.telegram_token}"

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        message_id: int | None = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for index, chunk in enumerate(self._chunk_text(text)):
                payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
                if reply_markup and index == 0:
                    payload["reply_markup"] = reply_markup
                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json=payload,
                )
                if response.status_code >= 400:
                    detail = response.text
                    raise httpx.HTTPStatusError(
                        f"Telegram sendMessage failed ({response.status_code}): {detail}",
                        request=response.request,
                        response=response,
                    )
                data = response.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram sendMessage returned error: {data}")
                if message_id is None:
                    result = data.get("result") or {}
                    message_id = result.get("message_id")
        return message_id

    @staticmethod
    def _chunk_text(text: str, limit: int = 4096) -> list[str]:
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        buffer = text
        while buffer:
            if len(buffer) <= limit:
                chunks.append(buffer)
                break
            split_index = buffer.rfind("\n", 0, limit)
            if split_index == -1 or split_index == 0:
                split_index = limit
            chunk = buffer[:split_index].rstrip()
            chunks.append(chunk)
            buffer = buffer[split_index:].lstrip()
        return chunks

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/setMyCommands",
                json={"commands": commands},
            )
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok", False):
            raise RuntimeError(f"Failed to set bot commands: {payload}")

    async def set_webhook(self, url: str, allowed_updates: list[str] | None = None) -> None:
        payload: dict[str, Any] = {"url": url}
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/setWebhook",
                json=payload,
            )
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok", False):
            raise RuntimeError(f"Failed to set webhook: {payload}")

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        async with httpx.AsyncClient(timeout=timeout + 5) as client:
            response = await client.get(f"{self.base_url}/getUpdates", params=params)
            response.raise_for_status()
            payload = response.json()

        if not payload.get("ok", False):
            raise RuntimeError(f"Telegram getUpdates returned error: {payload}")

        results = payload.get("result", [])
        if not isinstance(results, list):
            raise RuntimeError("Telegram getUpdates result is not a list.")
        return results

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:200]

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self.base_url}/answerCallbackQuery",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        if not data.get("ok", False):
            raise RuntimeError(f"Failed to answer callback query: {data}")

    async def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/editMessageText",
                json=payload,
            )
            if response.status_code >= 400:
                detail = response.text
                raise httpx.HTTPStatusError(
                    f"Telegram editMessageText failed ({response.status_code}): {detail}",
                    request=response.request,
                    response=response,
                )
            data = response.json()
        if not data.get("ok", False):
            raise RuntimeError(f"Failed to edit message text: {data}")

    async def edit_message_reply_markup(
        self,
        chat_id: int | str,
        message_id: int,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self.base_url}/editMessageReplyMarkup",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        if not data.get("ok", False):
            raise RuntimeError(f"Failed to edit message reply markup: {data}")
