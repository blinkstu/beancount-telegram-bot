from __future__ import annotations

from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI
from httpx import HTTPStatusError
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .routes import router
from .services.fava_manager import FavaManager
from .services.telegram import TelegramService
from .storage.database import Database

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database(settings.sqlite_path)
    await db.initialize()
    telegram = TelegramService()
    try:
        await telegram.set_my_commands(
            [
                {"command": "start", "description": "Show bot overview and current instruction"},
                {"command": "instruction", "description": "View or update your custom instruction"},
                {"command": "accounts", "description": "View ledger accounts and balances"},
            ]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to set Telegram commands: %s", exc)

    webhook_url = settings.telegram_webhook_url
    if webhook_url:
        if webhook_url.startswith("http://"):
            logger.warning("Telegram requires HTTPS webhook endpoints; skipping setWebhook for %s", webhook_url)
        else:
            try:
                await telegram.set_webhook(
                    webhook_url,
                    allowed_updates=["message", "callback_query"],
                )
                logger.info("Telegram webhook set to %s", webhook_url)
            except HTTPStatusError as exc:
                response_text = exc.response.text if exc.response is not None else "no response text"
                status = exc.response.status_code if exc.response else "unknown"
                logger.error("Failed to set Telegram webhook (%s): %s", status, response_text)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to set Telegram webhook: %s", exc)
    else:
        logger.warning("TELEGRAM_WEBHOOK_URL not configured; webhook not set.")
    app.state.db = db
    fava_manager = FavaManager(settings.beancount_root, host="0.0.0.0", port=5001)
    await fava_manager.refresh()
    app.state.fava_manager = fava_manager
    try:
        yield
    finally:
        await fava_manager.stop()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Beancount Telegram Bot", lifespan=lifespan)
    settings = get_settings()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_key,
        https_only=bool(settings.telegram_webhook_url),
        same_site="lax",
    )
    app.include_router(router)
    return app


app = create_app()
