from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


load_dotenv()
BASE_DIR = Path(__file__).resolve().parents[1]


def _default_data_directory() -> Path:
    return BASE_DIR / "data"


def _default_sqlite_path() -> Path:
    return BASE_DIR / "data/messages.db"


def _default_beancount_root() -> Path:
    return BASE_DIR / "data/beancount"


class Settings(BaseModel):
    telegram_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    ai_provider: str = Field("deepseek", alias="AI_PROVIDER")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_api_url: str = Field(
        "https://api.deepseek.com/v1/chat/completions", alias="DEEPSEEK_API_URL"
    )
    deepseek_model: str = Field("deepseek-chat", alias="DEEPSEEK_MODEL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_api_base: str | None = Field(default=None, alias="OPENAI_API_BASE")
    openai_model: str = Field("gpt-4.1-mini", alias="OPENAI_MODEL")
    telegram_webhook_url: str | None = Field(default=None, alias="TELEGRAM_WEBHOOK_URL")
    telegram_login_bot_username: str | None = Field(default=None, alias="TELEGRAM_LOGIN_BOT_USERNAME")
    telegram_login_auth_url: str | None = Field(default=None, alias="TELEGRAM_LOGIN_AUTH_URL")
    telegram_login_request_access: str | None = Field(default=None, alias="TELEGRAM_LOGIN_REQUEST_ACCESS")
    session_secret_key: str = Field("change-me", alias="SESSION_SECRET_KEY")
    data_directory: Path = Field(default_factory=_default_data_directory, alias="DATA_DIRECTORY")
    sqlite_path: Path = Field(default_factory=_default_sqlite_path, alias="SQLITE_PATH")
    beancount_root: Path = Field(default_factory=_default_beancount_root, alias="BEANCOUNT_ROOT")

    class Config:
        populate_by_name = True


@lru_cache
def get_settings() -> Settings:
    kwargs: dict[str, object] = {}
    for field_name, field_info in Settings.model_fields.items():
        alias = field_info.alias or field_name
        if alias in os.environ:
            kwargs[field_name] = os.environ[alias]

    settings = Settings(**kwargs)
    settings.data_directory = settings.data_directory.expanduser().resolve()
    settings.beancount_root = settings.beancount_root.expanduser().resolve()
    settings.sqlite_path = settings.sqlite_path.expanduser().resolve()
    settings.data_directory.mkdir(parents=True, exist_ok=True)
    settings.beancount_root.mkdir(parents=True, exist_ok=True)
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
