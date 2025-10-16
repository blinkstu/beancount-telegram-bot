from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Chat(BaseModel):
    id: int
    type: str
    username: str | None = None
    title: str | None = None


class User(BaseModel):
    id: int
    is_bot: bool
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None


class Message(BaseModel):
    message_id: int = Field(alias="message_id")
    date: int
    chat: Chat
    from_user: User | None = Field(default=None, alias="from")
    text: str | None = None
    reply_to_message: Optional["Message"] = Field(default=None, alias="reply_to_message")


class CallbackQuery(BaseModel):
    id: str
    from_user: User = Field(alias="from")
    message: Message | None = None
    data: str | None = None


class Update(BaseModel):
    update_id: int = Field(alias="update_id")
    message: Message | None = None
    callback_query: CallbackQuery | None = None


Message.model_rebuild()
