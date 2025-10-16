from __future__ import annotations

import json
from pathlib import Path

import aiosqlite


class Database:
    def __init__(self, path: Path):
        self._path = path
        self._connection: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database connection is not initialized.")
        return self._connection

    async def initialize(self) -> None:
        self._connection = await aiosqlite.connect(self._path)
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                username TEXT,
                chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                response TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS instructions (
                user_id TEXT PRIMARY KEY,
                instruction TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_row_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                entries TEXT NOT NULL,
                summary TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                telegram_message_id INTEGER,
                ledger_path TEXT,
                original_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                FOREIGN KEY(message_row_id) REFERENCES messages(id)
            )
            """
        )
        await self.connection.commit()

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def log_message(
        self,
        user_id: str,
        chat_id: str,
        text: str,
        username: str | None = None,
        response: str | None = None,
    ) -> int:
        cursor = await self.connection.execute(
            """
            INSERT INTO messages (user_id, username, chat_id, text, response)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, username, chat_id, text, response),
        )
        await self.connection.commit()
        return int(cursor.lastrowid)

    async def update_message_response(self, message_id: int, response: str) -> None:
        await self.connection.execute(
            "UPDATE messages SET response = ? WHERE id = ?",
            (response, message_id),
        )
        await self.connection.commit()

    async def get_instruction(self, user_id: str) -> str | None:
        cursor = await self.connection.execute(
            "SELECT instruction FROM instructions WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            return row[0]
        return None

    async def set_instruction(self, user_id: str, instruction: str) -> None:
        await self.connection.execute(
            """
            INSERT INTO instructions (user_id, instruction, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                instruction = excluded.instruction,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, instruction),
        )
        await self.connection.commit()

    async def clear_instruction(self, user_id: str) -> None:
        await self.connection.execute(
            "DELETE FROM instructions WHERE user_id = ?",
            (user_id,),
        )
        await self.connection.commit()

    async def create_pending_entry(
        self,
        *,
        message_row_id: int,
        user_id: str,
        chat_id: str,
        entries: list[str],
        summary: str | None,
        original_text: str,
        prompt_message_id: int | None = None,
        error_context: str | None = None,
    ) -> int:
        cursor = await self.connection.execute(
            """
            INSERT INTO pending_entries (
                message_row_id,
                user_id,
                chat_id,
                entries,
                summary,
                original_text,
                prompt_message_id,
                error_context
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_row_id,
                user_id,
                chat_id,
                json.dumps(entries, ensure_ascii=False),
                summary,
                original_text,
                prompt_message_id,
                error_context,
            ),
        )
        await self.connection.commit()
        return int(cursor.lastrowid)

    async def set_pending_message_id(self, pending_id: int, telegram_message_id: int) -> None:
        await self.connection.execute(
            """
            UPDATE pending_entries
            SET telegram_message_id = ?
            WHERE id = ?
            """,
            (telegram_message_id, pending_id),
        )
        await self.connection.commit()

    async def get_pending_entry(self, pending_id: int) -> dict[str, object] | None:
        cursor = await self.connection.execute(
            """
            SELECT
                id,
                message_row_id,
                user_id,
                chat_id,
                entries,
                summary,
                status,
                telegram_message_id,
                ledger_path,
                original_text,
                prompt_message_id,
                error_context
            FROM pending_entries
            WHERE id = ?
            """,
            (pending_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None

        (
            record_id,
            message_row_id,
            user_id,
            chat_id,
            entries_json,
            summary,
            status,
            telegram_message_id,
            ledger_path,
            original_text,
            prompt_message_id,
            error_context,
        ) = row

        try:
            entries = json.loads(entries_json)
        except json.JSONDecodeError:
            entries = []

        return {
            "id": int(record_id),
            "message_row_id": int(message_row_id),
            "user_id": str(user_id),
            "chat_id": str(chat_id),
            "entries": entries if isinstance(entries, list) else [],
            "summary": summary,
            "status": status,
            "telegram_message_id": telegram_message_id,
            "ledger_path": ledger_path,
            "original_text": original_text,
            "prompt_message_id": prompt_message_id,
            "error_context": error_context,
        }

    async def update_pending_entry_status(
        self,
        pending_id: int,
        status: str,
        ledger_path: str | None = None,
        error_context: str | None = None,
    ) -> None:
        await self.connection.execute(
            """
            UPDATE pending_entries
            SET status = ?,
                ledger_path = COALESCE(?, ledger_path),
                error_context = COALESCE(?, error_context),
                processed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, ledger_path, error_context, pending_id),
        )
        await self.connection.commit()

    async def set_prompt_message_id(self, pending_id: int, prompt_message_id: int) -> None:
        await self.connection.execute(
            """
            UPDATE pending_entries
            SET prompt_message_id = ?
            WHERE id = ?
            """,
            (prompt_message_id, pending_id),
        )
        await self.connection.commit()
