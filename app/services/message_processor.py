from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beancount import loader

from ..models.telegram import CallbackQuery, Message, Update
from ..storage.database import Database
from datetime import date

from .beancount_service import BeancountService
from .fava_manager import FavaManager
from .llm import LLMResult, generate_accounting_entry
from .statement_extractor import StatementExtractor
from .telegram import TelegramService


@dataclass
class MessageProcessingResult:
    user_id: str
    chat_id: int | str
    ledger_path: str
    entries: list[str]
    summary: str | None
    raw_ai_response: dict[str, Any]
    status: str
    pending_entry_id: int | None = None


class MessageProcessor:
    def __init__(self, db: Database, fava_manager: FavaManager | None = None):
        self.db = db
        self.telegram = TelegramService()
        self.beancount = BeancountService.from_settings()
        self.statement_extractor = StatementExtractor()
        self.fava_manager = fava_manager
        self.logger = logging.getLogger(__name__)

    async def handle_update(self, update: Update) -> MessageProcessingResult | None:
        if update.callback_query is not None:
            return await self._handle_callback(update.callback_query)

        message = update.message
        if message is None:
            return None

        if message.document or (message.photo and len(message.photo) > 0):
            return await self._handle_statement_upload(message)

        if not message.text:
            return None

        text = message.text.strip()
        if not text:
            return None

        from_user = message.from_user
        user_id = str(from_user.id if from_user else message.chat.id)
        username = from_user.username if from_user else None
        chat_id = message.chat.id

        message_row_id = await self.db.log_message(
            user_id=user_id,
            chat_id=str(chat_id),
            text=text,
            username=username,
            response=None,
        )

        instruction_raw = await self.db.get_instruction(user_id)
        instruction = instruction_raw.strip() if instruction_raw and instruction_raw.strip() else None

        if (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.is_bot
        ):
            prompt_text = (message.reply_to_message.text or "").strip()
            if prompt_text.startswith("Reply to this message to edit your custom instruction"):
                await self._handle_instruction_reply_edit(
                    user_id=user_id,
                    new_instruction=text,
                    message_row_id=message_row_id,
                    chat_id=chat_id,
                )
                return None

        command_token = ""
        command_payload = text
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            command_token = parts[0]
            command_payload = parts[1] if len(parts) > 1 else ""

        base_command = command_token.lower().split("@", 1)[0] if command_token else ""

        if base_command == "/start":
            response_text = await self._handle_start_command(
                user_id=user_id,
                current_instruction=instruction,
                message_row_id=message_row_id,
            )
            await self.telegram.send_message(chat_id=chat_id, text=response_text)
            return None

        if base_command == "/instruction":
            response_text, reply_markup = await self._handle_instruction_command(
                user_id=user_id,
                current_instruction=instruction,
                payload=command_payload,
                message_row_id=message_row_id,
            )
            await self.telegram.send_message(chat_id=chat_id, text=response_text, reply_markup=reply_markup)
            return None

        if base_command == "/accounts":
            response_text = await self._handle_accounts_command(
                user_id=user_id,
                message_row_id=message_row_id,
            )
            await self.telegram.send_message(chat_id=chat_id, text=response_text)
            return None

        if not self._looks_like_transaction(text):
            friendly = (
                "I didn't detect any amounts or transaction details. "
                "Please describe a transaction with dates/amounts or attach a statement file/image."
            )
            await self.db.update_message_response(message_row_id, friendly)
            await self.telegram.send_message(chat_id=chat_id, text=friendly)
            return None

        prompt_message_id: int | None = None
        try:
            prompt_message_id = await self.telegram.send_message(
                chat_id=chat_id,
                text="Generating accounting entries, please wait...",
            )

            llm_result = await self._call_llm(text, user_id, instruction)

            pending_id = await self.db.create_pending_entry(
                message_row_id=message_row_id,
                user_id=user_id,
                chat_id=str(chat_id),
                entries=llm_result.entries,
                summary=llm_result.summary,
                original_text=text,
                prompt_message_id=prompt_message_id,
            )

            summary = llm_result.summary or "Please review the generated Beancount entries below."
            ledger_path = str(self.beancount.user_ledger_path(user_id))
            response_lines = [
                summary,
                "Generated entries:",
                *[entry.strip() for entry in llm_result.entries],
                "",
                "Use the buttons below to confirm whether to write them to the ledger.",
            ]
            response_text = "\n".join(response_lines)

            await self.db.update_message_response(message_row_id, response_text)
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Accept entry", "callback_data": f"accept:{pending_id}"},
                        {"text": "❌ Reject", "callback_data": f"reject:{pending_id}"},
                    ]
                ]
            }

            if prompt_message_id is not None:
                try:
                    await self._send_or_edit_chunked_message(
                        chat_id,
                        prompt_message_id,
                        response_text,
                        reply_markup=reply_markup,
                    )
                    await self.db.set_pending_message_id(pending_id, prompt_message_id)
                except Exception:  # noqa: BLE001
                    sent_message_id = await self._send_or_edit_chunked_message(
                        chat_id,
                        None,
                        response_text,
                        reply_markup=reply_markup,
                    )
                    if sent_message_id is not None:
                        await self.db.set_prompt_message_id(pending_id, sent_message_id)
                        await self.db.set_pending_message_id(pending_id, sent_message_id)
            else:
                sent_message_id = await self._send_or_edit_chunked_message(
                    chat_id,
                    None,
                    response_text,
                    reply_markup=reply_markup,
                )
                if sent_message_id is not None:
                    await self.db.set_prompt_message_id(pending_id, sent_message_id)
                    await self.db.set_pending_message_id(pending_id, sent_message_id)

            await self._refresh_fava()

            return MessageProcessingResult(
                user_id=user_id,
                chat_id=chat_id,
                ledger_path=ledger_path,
                entries=llm_result.entries,
                summary=llm_result.summary,
                raw_ai_response=llm_result.raw,
                status="pending",
                pending_entry_id=pending_id,
            )
        except Exception as exc:
            await self.db.update_message_response(message_row_id, f"ERROR: {exc}")
            if prompt_message_id is not None:
                try:
                    await self._send_or_edit_chunked_message(
                        chat_id,
                        prompt_message_id,
                        f"Failed to generate entry: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    await self.telegram.send_message(chat_id=chat_id, text=f"Failed to generate entry: {exc}")
            raise

    async def _handle_statement_upload(self, message: Message) -> MessageProcessingResult | None:
        caption = (message.caption or message.text or "").strip()
        note = caption or None
        from_user = message.from_user
        user_id = str(from_user.id if from_user else message.chat.id)
        username = from_user.username if from_user else None
        chat_id = message.chat.id
        text_for_log = caption or "[statement upload]"

        message_row_id = await self.db.log_message(
            user_id=user_id,
            chat_id=str(chat_id),
            text=text_for_log,
            username=username,
            response=None,
        )

        processing_message_id = await self.telegram.send_message(
            chat_id=chat_id,
            text="Extracting statement, please wait...",
        )

        local_path: Path | None = None
        try:
            local_path = await self._download_attachment_to_temp(message)
            statement = await asyncio.to_thread(
                self.statement_extractor.extract,
                user_id,
                local_path,
                note,
            )
            entries, new_count, skipped = await asyncio.to_thread(
                self.statement_extractor.generate_entries,
                statement,
                user_id,
                local_path,
            )

            if new_count == 0:
                response_text = (
                    "No new transactions detected in the uploaded statement. "
                    f"Skipped {skipped} duplicate or zero-amount entries."
                )
                await self.db.update_message_response(message_row_id, response_text)
                if processing_message_id is not None:
                    await self._send_or_edit_chunked_message(
                        chat_id,
                        processing_message_id,
                        response_text,
                    )
                else:
                    await self._send_or_edit_chunked_message(chat_id, None, response_text)
                return None

            summary_lines = [
                "Statement extraction ready for confirmation.",
                f"New transactions detected: {new_count}",
            ]
            if skipped:
                summary_lines.append(f"Skipped {skipped} entries already present in the ledger.")
            summary = "\n".join(summary_lines)
            statement_json = statement.model_dump_json(indent=2)
            entry_preview = "\n\n".join(entries)
            response_lines = [
                summary,
                "",
                "Generated entries (will be written on approval):",
                entry_preview,
                "",
                "Structured statement JSON:",
                statement_json,
            ]
            response_text = "\n".join(response_lines)

            pending_id = await self.db.create_pending_entry(
                message_row_id=message_row_id,
                user_id=user_id,
                chat_id=str(chat_id),
                entries=entries,
                summary=summary,
                original_text=text_for_log,
                prompt_message_id=processing_message_id,
            )

            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Accept entry", "callback_data": f"accept:{pending_id}"},
                        {"text": "❌ Reject", "callback_data": f"reject:{pending_id}"},
                    ]
                ]
            }

            if processing_message_id is not None:
                await self._send_or_edit_chunked_message(
                    chat_id,
                    processing_message_id,
                    response_text,
                    reply_markup=reply_markup,
                )
                await self.db.set_prompt_message_id(pending_id, processing_message_id)
                await self.db.set_pending_message_id(pending_id, processing_message_id)
            else:
                sent_message_id = await self._send_or_edit_chunked_message(
                    chat_id,
                    None,
                    response_text,
                    reply_markup=reply_markup,
                )
                if sent_message_id is not None:
                    await self.db.set_prompt_message_id(pending_id, sent_message_id)
                    await self.db.set_pending_message_id(pending_id, sent_message_id)

            await self.db.update_message_response(message_row_id, response_text)

            return MessageProcessingResult(
                user_id=user_id,
                chat_id=chat_id,
                ledger_path=str(self.beancount.user_ledger_path(user_id)),
                entries=entries,
                summary=summary,
                raw_ai_response=statement.model_dump(),
                status="pending",
                pending_entry_id=pending_id,
            )
        except Exception as exc:  # noqa: BLE001
            error_text = f"Failed to extract statement: {exc}"
            self.logger.exception("Failed to handle statement upload: %s", exc)
            await self.db.update_message_response(message_row_id, error_text)
            if processing_message_id is not None:
                try:
                    await self._send_or_edit_chunked_message(chat_id, processing_message_id, error_text)
                except Exception:  # noqa: BLE001
                    await self.telegram.send_message(chat_id=chat_id, text=error_text)
            else:
                await self.telegram.send_message(chat_id=chat_id, text=error_text)
            return None
        finally:
            if local_path:
                try:
                    local_path.unlink(missing_ok=True)
                    if local_path.parent.name.startswith("statement-"):
                        local_path.parent.rmdir()
                except OSError:
                    pass

    async def _download_attachment_to_temp(self, message: Message) -> Path:
        if message.document:
            file_id = message.document.file_id
            filename = message.document.file_name or Path(file_id).name
            suffix = Path(filename).suffix or Path(file_id).suffix or ""
        else:
            photos = message.photo or []
            if not photos:
                raise RuntimeError("No photo sizes available for statement upload")
            photo = max(photos, key=lambda p: p.file_size or 0)
            file_id = photo.file_id
            suffix = ".jpg"
            filename = f"{file_id}{suffix}"

        tmp_dir = Path(tempfile.mkdtemp(prefix="statement-"))
        destination = tmp_dir / filename
        return await self.telegram.download_file(file_id, destination=destination)

    async def _send_or_edit_chunked_message(
        self,
        chat_id: int | str,
        message_id: int | None,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        chunks = TelegramService._chunk_text(text)
        if not chunks:
            return message_id

        first_chunk, *rest = chunks
        result_message_id = message_id
        if message_id is not None:
            await self.telegram.edit_message_text(
                chat_id,
                message_id,
                first_chunk,
                reply_markup=reply_markup,
            )
        else:
            result_message_id = await self.telegram.send_message(
                chat_id=chat_id,
                text=first_chunk,
                reply_markup=reply_markup,
            )

        for chunk in rest:
            await self.telegram.send_message(chat_id=chat_id, text=chunk)

        return result_message_id

    async def _handle_instruction_command(
        self,
        *,
        user_id: str,
        current_instruction: str | None,
        payload: str,
        message_row_id: int,
    ) -> tuple[str, dict[str, Any] | None]:
        normalized_payload = payload.strip()
        reply_markup: dict[str, Any] | None = None
        if not normalized_payload:
            if current_instruction:
                response_text = (
                    "Reply to this message to edit your custom instruction:\n"
                    f"{current_instruction}\n\n"
                    "You can also send `/instruction edit` to get a copyable instruction template."
                )
            else:
                response_text = (
                    "No custom instruction is set yet.\n"
                    "Reply to this message with new content, or send `/instruction <instruction text>` to set it directly."
                )
            reply_markup = {
                "force_reply": True,
                "input_field_placeholder": "Enter a new custom instruction (send reset to clear)",
                "selective": True,
            }
        else:
            lowered = normalized_payload.lower()
            if lowered in {"reset", "clear"}:
                await self.db.clear_instruction(user_id)
                response_text = "Custom instruction cleared."
            elif lowered == "edit":
                if current_instruction:
                    response_text = (
                        "Copy the instruction below, edit it, then reply to this message with the update:\n"
                        f"```\n{current_instruction}\n```"
                    )
                    reply_markup = {
                        "force_reply": True,
                        "input_field_placeholder": "Paste and modify your custom instruction",
                        "selective": True,
                    }
                else:
                    response_text = "No custom instruction is set; reply to this message to add one."
                    reply_markup = {
                        "force_reply": True,
                        "input_field_placeholder": "Enter a new custom instruction",
                        "selective": True,
                    }
            else:
                await self.db.set_instruction(user_id, normalized_payload)
                response_text = "Custom instruction updated:\n" + normalized_payload

        await self.db.update_message_response(message_row_id, response_text)
        return response_text, reply_markup

    async def _handle_start_command(
        self,
        *,
        user_id: str,
        current_instruction: str | None,
        message_row_id: int,
    ) -> str:
        instruction_text = current_instruction or "No custom instruction is set yet."
        response_text = (
            "Welcome to the accounting bot!\n"
            "Send messages like \"I spent 13000 KZT on dinner using KaspiBank\" to generate Beancount entries automatically.\n\n"
            "Available commands:\n"
            "• /instruction — View or update your custom instruction\n"
            "• /instruction edit — Return the instruction text so you can copy and edit it\n"
            "• /instruction <instruction text> — Set a new instruction\n"
            "• /instruction reset — Clear the custom instruction\n"
            "• /accounts — View current ledger accounts and balances\n\n"
            f"Current instruction:\n{instruction_text}"
        )
        await self.db.update_message_response(message_row_id, response_text)
        return response_text

    async def _handle_instruction_reply_edit(
        self,
        *,
        user_id: str,
        new_instruction: str,
        message_row_id: int,
        chat_id: int,
    ) -> None:
        normalized = new_instruction.strip()
        if not normalized:
            response_text = "Instruction not updated: received empty content."
        elif normalized.lower() in {"reset", "clear"}:
            await self.db.clear_instruction(user_id)
            response_text = "Custom instruction cleared."
        else:
            await self.db.set_instruction(user_id, normalized)
            response_text = "Custom instruction updated:\n" + normalized

        await self.db.update_message_response(message_row_id, response_text)
        await self.telegram.send_message(chat_id=chat_id, text=response_text)

    async def _handle_accounts_command(
        self,
        *,
        user_id: str,
        message_row_id: int,
    ) -> str:
        try:
            lines, errors = await asyncio.to_thread(self.beancount.summarize_accounts, user_id)
        except Exception as exc:  # noqa: BLE001
            response_text = f"Failed to read ledger: {exc}"
            await self.db.update_message_response(message_row_id, response_text)
            return response_text

        if not lines:
            response_lines = ["No accounts found in the ledger yet; try recording a transaction first."]
        else:
            response_lines = ["Ledger accounts and balances:", *lines]

        if errors:
            response_lines.append("")
            response_lines.append("Parse warnings:")
            response_lines.extend(f"- {err}" for err in errors)

        response_text = "\n".join(response_lines)
        await self.db.update_message_response(message_row_id, response_text)
        return response_text

    async def _handle_callback(self, callback_query: CallbackQuery) -> MessageProcessingResult | None:
        data = (callback_query.data or "").strip()
        if not data:
            await self._safe_answer_callback(callback_query.id, text="Invalid action")
            return None

        if data.startswith("accept:"):
            pending_id = self._parse_pending_id(data)
            if pending_id is None:
                await self._safe_answer_callback(callback_query.id, text="Invalid request", show_alert=True)
                return None
            return await self._finalize_pending(callback_query, pending_id, accept=True)

        if data.startswith("reject:"):
            pending_id = self._parse_pending_id(data)
            if pending_id is None:
                await self._safe_answer_callback(callback_query.id, text="Invalid request", show_alert=True)
                return None
            return await self._finalize_pending(callback_query, pending_id, accept=False)

        if data.startswith("autofix:"):
            pending_id = self._parse_pending_id(data)
            if pending_id is None:
                await self._safe_answer_callback(callback_query.id, text="Invalid request", show_alert=True)
                return None
            return await self._autofix_pending(callback_query, pending_id)

        await self._safe_answer_callback(callback_query.id, text="Unknown action")
        return None

    @staticmethod
    def _looks_like_transaction(text: str) -> bool:
        return bool(re.search(r"\d", text))

    @staticmethod
    def _parse_pending_id(data: str) -> int | None:
        try:
            _, value = data.split(":", 1)
            return int(value)
        except (ValueError, AttributeError):
            return None

    async def _finalize_pending(
        self,
        callback_query: CallbackQuery,
        pending_id: int,
        *,
        accept: bool,
    ) -> MessageProcessingResult | None:
        record = await self.db.get_pending_entry(pending_id)
        if record is None:
            await self._safe_answer_callback(callback_query.id, text="Request not found", show_alert=True)
            self.logger.warning("Pending entry %s not found for callback", pending_id)
            return None

        status = record.get("status")
        if status != "pending":
            await self._safe_answer_callback(callback_query.id, text="Request already processed", show_alert=True)
            self.logger.info("Pending entry %s already processed (status=%s)", pending_id, status)
            return None

        callback_user_id = str(callback_query.from_user.id)
        if str(record.get("user_id")) != callback_user_id:
            await self._safe_answer_callback(callback_query.id, text="You are not allowed to act on this request", show_alert=True)
            self.logger.warning(
                "User %s attempted to act on entry %s owned by %s",
                callback_user_id,
                pending_id,
                record.get("user_id"),
            )
            return None

        await self._safe_answer_callback(callback_query.id, text="Processing...")

        fallback_chat = None
        if callback_query.message is not None:
            fallback_chat = callback_query.message.chat.id
        chat_id_normalized = self._normalize_chat_id(record.get("chat_id"))
        chat_id = fallback_chat if fallback_chat is not None else chat_id_normalized
        telegram_message_id = record.get("telegram_message_id")

        try:
            if accept:
                ledger_path_obj = self.beancount.user_ledger_path(record["user_id"])
                previous_content = ""
                if ledger_path_obj.exists():
                    previous_content = ledger_path_obj.read_text(encoding="utf-8")
                entries = record.get("entries", [])
                self.logger.info("Accepting pending entry %s with %d postings", pending_id, len(entries))
                ledger_path = await asyncio.to_thread(
                    self.beancount.append_entries,
                    record["user_id"],
                    entries,
                )
                validation_errors = await asyncio.to_thread(self._validate_ledger, ledger_path_obj)
                if validation_errors:
                    ledger_path_obj.write_text(previous_content, encoding="utf-8")
                    error_lines = [self._format_validation_error(err) for err in validation_errors[:5]]
                    error_summary = "\n".join(f"- {line}" for line in error_lines)
                    entry_preview = "\n".join(entry.strip() for entry in entries)
                    response_text = (
                        "❌ Entry failed: the generated entries did not pass Beancount validation. Please review and try again.\n\n"
                        "Error details:\n"
                        f"{error_summary}\n\n"
                        "Generated entry preview:\n"
                        f"{entry_preview}\n\n"
                        "Original message:\n"
                        f"{record['original_text']}"
                    )
                    error_context = (
                        f"Error details:\n{error_summary}\n\n"
                        f"Generated entry preview:\n{entry_preview}\n\n"
                        f"Original message:\n{record['original_text']}"
                    )
                    await self.db.update_pending_entry_status(pending_id, "error", None, error_context)
                    await self.db.update_message_response(record["message_row_id"], response_text)
                    error_markup = {
                        "inline_keyboard": [
                            [
                                {"text": "🔄 Auto-fix", "callback_data": f"autofix:{pending_id}"},
                                {"text": "❌ Reject", "callback_data": f"reject:{pending_id}"},
                            ]
                        ]
                    }
                    await self._update_callback_message(
                        chat_id,
                        telegram_message_id,
                        callback_query,
                        response_text,
                        reply_markup=error_markup,
                    )
                    return MessageProcessingResult(
                        user_id=str(record["user_id"]),
                        chat_id=chat_id,
                        ledger_path=str(ledger_path_obj),
                        entries=list(entries),
                        summary=response_text,
                        raw_ai_response={},
                        status="error",
                        pending_entry_id=pending_id,
                    )

                summary_text = record.get("summary")
                response_parts = ["✅ Entry accepted and written to the ledger."]
                if summary_text:
                    response_parts.append(summary_text)
                response_parts.append("Generated entries:")
                response_parts.extend(entry.strip() for entry in entries)
                response_text = "\n".join(response_parts)
                await self.db.update_pending_entry_status(pending_id, "accepted", str(ledger_path))
                await self.db.update_message_response(record["message_row_id"], response_text)
                await self._update_callback_message(chat_id, telegram_message_id, callback_query, response_text)
                await self._safe_answer_callback(callback_query.id, text="✅ Accepted")
                return MessageProcessingResult(
                    user_id=str(record["user_id"]),
                    chat_id=chat_id,
                    ledger_path=str(ledger_path),
                    entries=list(entries),
                    summary=record.get("summary"),
                    raw_ai_response={},
                    status="accepted",
                    pending_entry_id=pending_id,
                )

            summary_text = record.get("summary")
            response_lines = ["❌ Entry rejected. Please submit again or adjust the content."]
            if summary_text:
                response_lines.append(summary_text)
            response_text = "\n".join(response_lines)
            await self.db.update_pending_entry_status(pending_id, "rejected", None)
            await self.db.update_message_response(record["message_row_id"], response_text)
            await self._update_callback_message(chat_id, telegram_message_id, callback_query, response_text)
            await self._safe_answer_callback(callback_query.id, text="Rejected")
            self.logger.info("Rejected pending entry %s", pending_id)
            return MessageProcessingResult(
                user_id=str(record["user_id"]),
                chat_id=chat_id,
                ledger_path=str(self.beancount.user_ledger_path(str(record["user_id"]))),
                entries=[],
                summary=record.get("summary"),
                raw_ai_response={},
                status="rejected",
                pending_entry_id=pending_id,
            )
        except Exception as exc:  # noqa: BLE001
            error_text = f"Error while processing request: {exc}"
            self.logger.exception("Failed to finalize pending entry %s: %s", pending_id, exc)
            await self.db.update_pending_entry_status(pending_id, "error", None)
            await self.db.update_message_response(record["message_row_id"], error_text)
            await self._update_callback_message(chat_id, telegram_message_id, callback_query, error_text)
        finally:
            await self._refresh_fava()
        return None

    async def _update_callback_message(
        self,
        chat_id: int | str,
        telegram_message_id: int | None,
        callback_query: CallbackQuery,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        target_chat_id = chat_id
        if telegram_message_id is None and callback_query.message is not None:
            telegram_message_id = callback_query.message.message_id
            target_chat_id = callback_query.message.chat.id

        try:
            if telegram_message_id is None:
                await self.telegram.send_message(chat_id=target_chat_id, text=text, reply_markup=reply_markup)
                return

            if len(text) <= 4096:
                try:
                    await self.telegram.edit_message_text(
                        target_chat_id,
                        telegram_message_id,
                        text,
                        reply_markup=reply_markup,
                    )
                    return
                except Exception:  # noqa: BLE001
                    pass

            try:
                await self.telegram.edit_message_reply_markup(target_chat_id, telegram_message_id, reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            await self.telegram.send_message(chat_id=target_chat_id, text=text, reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to update callback message: %s", exc)

    async def _safe_answer_callback(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        try:
            await self.telegram.answer_callback_query(callback_query_id, text=text, show_alert=show_alert)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to answer callback query %s: %s", callback_query_id, exc)

    async def _refresh_fava(self) -> None:
        if self.fava_manager is None:
            return
        try:
            await self.fava_manager.refresh()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to refresh Fava process: %s", exc)

    @staticmethod
    def _validate_ledger(ledger_path: Path) -> list[str]:
        try:
            _, errors, _ = loader.load_file(str(ledger_path))
        except Exception as exc:
            return [str(exc)]
        return [str(err) for err in errors]

    @staticmethod
    def _format_validation_error(error_text: str) -> str:
        lineno = "?"
        message = error_text
        import re

        match = re.search(r"lineno': (\d+)", error_text)
        if match:
            lineno = match.group(1)

        message_match = re.search(r"message='([^']+)'", error_text)
        if message_match:
            message = message_match.group(1)

        return f"Line {lineno}: {message}"

    @staticmethod
    def _normalize_chat_id(chat_id: object) -> int | str:
        if isinstance(chat_id, int):
            return chat_id
        if isinstance(chat_id, str):
            try:
                return int(chat_id)
            except ValueError:
                return chat_id
        return str(chat_id)


    async def _autofix_pending(self, callback_query: CallbackQuery, pending_id: int) -> MessageProcessingResult | None:
        record = await self.db.get_pending_entry(pending_id)
        if record is None:
            await self._safe_answer_callback(callback_query.id, text="Request not found", show_alert=True)
            return None

        if record.get("status") != "error":
            await self._safe_answer_callback(callback_query.id, text="Request does not need to be fixed", show_alert=True)
            return None

        error_context = record.get("error_context")
        if not error_context:
            await self._safe_answer_callback(callback_query.id, text="Missing error details; unable to auto-fix", show_alert=True)
            return None

        await self._safe_answer_callback(callback_query.id, text="Attempting auto-fix...")

        instruction = await self.db.get_instruction(str(record.get("user_id")))
        extra_context = (
            "The previously generated entries failed Beancount validation. Use the details below to fix them:\n"
            f"{error_context}\n"
            "Regenerate entries that pass validation. Review every error carefully and provide new entries that resolve the issues."
        )

        try:
            llm_result = await self._call_llm(
                record.get("original_text", ""),
                record.get("user_id"),
                instruction,
                extra_context=extra_context,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Auto-fix failed for pending entry %s: %s", pending_id, exc)
            await self._update_callback_message(
                record.get("chat_id"),
                record.get("telegram_message_id"),
                callback_query,
                f"Auto-fix failed: {exc}",
            )
            return None

        entries_json = json.dumps(llm_result.entries, ensure_ascii=False)
        await self.db.connection.execute(
            """
            UPDATE pending_entries
            SET entries = ?,
                summary = ?,
                status = 'pending',
                ledger_path = NULL,
                error_context = NULL,
                processed_at = NULL
            WHERE id = ?
            """,
            (entries_json, llm_result.summary, pending_id),
        )
        await self.db.connection.commit()

        summary = llm_result.summary or "Auto-fix suggestions:"
        response_parts = ["🤖 Auto-fix suggestions (pending your confirmation).", summary, "Generated entries:"]
        response_parts.extend(entry.strip() for entry in llm_result.entries)
        response_text = "\n".join(part for part in response_parts if part)

        await self.db.update_message_response(record["message_row_id"], response_text)

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Accept entry", "callback_data": f"accept:{pending_id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{pending_id}"},
                ]
            ]
        }
        await self._update_callback_message(
            record.get("chat_id"),
            record.get("telegram_message_id"),
            callback_query,
            response_text,
            reply_markup=reply_markup,
        )

        return MessageProcessingResult(
            user_id=str(record.get("user_id")),
            chat_id=self._normalize_chat_id(record.get("chat_id")),
            ledger_path=str(self.beancount.user_ledger_path(str(record.get("user_id")))),
            entries=llm_result.entries,
            summary=llm_result.summary,
            raw_ai_response=llm_result.raw,
            status="pending",
            pending_entry_id=pending_id,
        )

    async def _call_llm(self, text: str, user_id, instruction: str | None, extra_context: str | None = None) -> LLMResult:
        try:
            account_lines, account_errors = await asyncio.to_thread(self.beancount.summarize_accounts, user_id)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to read ledger: {exc}") from exc
        else:
            ledger_empty = len(account_lines) == 0
            if account_lines:
                accounts_text = "\n".join(account_lines)
                account_section = (
                    "Existing ledger accounts and balances are listed below. Reuse them whenever possible to avoid duplicates:\n"
                    f"{accounts_text}"
                )
            else:
                account_section = (
                    "The ledger currently has no accounts. Initialize defaults such as operating currency (for example CNY, USD, or KZT) and the basic account structure (Assets, Liabilities, Income, Expenses, Equity),"
                    "and use option, commodity, and open directives to create opening entries when needed."
                )

            if account_errors:
                warnings_text = "\n".join(f"- {warning}" for warning in account_errors)
                account_section += f"\nNote: Loading the accounts produced the following warnings. Adjust if necessary:\n{warnings_text}"

        instruction_block = ""
        if instruction:
            instruction_block = f"The user's custom instruction is below. Follow it exactly:\n{instruction.strip()}\n\n"

        today_str = date.today().isoformat()
        prompt_parts = [
            instruction_block,
            "Turn the user's request below into Beancount-compliant transaction entries.",
            "If you need to create new accounts or adjust balances, add appropriate opening entries or balance adjustments.",
            f"\n{account_section}\n",
            "Only create new accounts when the request truly requires one that does not exist. Add an open directive dated at the start of the current year and follow the existing Beancount hierarchy.",
        ]
        if ledger_empty:
            prompt_parts.append(
                "The ledger is currently empty. Add the required option, commodity, and open directives to establish the default currency and base account structure before recording the user's transaction."
            )
        if extra_context:
            prompt_parts.append(f"Previous error or feedback:\n{extra_context}\n")
        prompt_parts.extend(
            [
                f"Today's date: {today_str}",
                f"User input: {text}"
            ]
        )
        prompt = "\n".join(part for part in prompt_parts if part)
        return await generate_accounting_entry(prompt)
