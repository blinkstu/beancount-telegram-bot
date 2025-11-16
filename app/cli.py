from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv


def load_env() -> None:
    load_dotenv()


def load_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def main() -> None:
    load_env()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = load_bool(os.getenv("RELOAD"), True)

    uvicorn.run("app.main:app", host=host, port=port, reload=reload, log_level="debug")
