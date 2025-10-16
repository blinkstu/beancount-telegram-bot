from __future__ import annotations

import hashlib
import hmac
import logging
import time
from html import escape

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .config import get_settings
from .models.telegram import Update
from .services.message_processor import MessageProcessor

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    settings = get_settings()
    bot_username = settings.telegram_login_bot_username
    auth_url = settings.telegram_login_auth_url
    request_access = settings.telegram_login_request_access or ""

    session_user = request.session.get("telegram_user")
    if session_user:
        user_id = str(session_user.get("id")) if session_user.get("id") is not None else None
        display_name = escape(session_user.get("name") or session_user.get("username") or user_id or "User")
        target_path = f"/{user_id}/income_statement/" if user_id else "/reports/income-statement"

        html = f"""
        <html>
            <head>
                <meta charset=\"utf-8\" />
                <title>Beancount Telegram Bot</title>
            </head>
            <body>
                <h1>Welcome, {display_name}</h1>
                <p>Click the button below to open the Fava reports:</p>
                <p><a href=\"{escape(target_path)}\" style=\"display:inline-block;padding:0.6rem 1.2rem;background:#4CAF50;color:#fff;text-decoration:none;border-radius:4px;\">Jump to Fava</a></p>
                <p><a href=\"/logout\">Log out</a></p>
            </body>
        </html>
        """
        return HTMLResponse(content=html)

    if not bot_username or not auth_url:
        html = """
        <html>
            <head><title>Beancount Telegram Bot</title></head>
            <body>
                <h1>Beancount Telegram Bot</h1>
                <p>The Telegram login button is not configured. Please set <code>TELEGRAM_LOGIN_BOT_USERNAME</code> and <code>TELEGRAM_LOGIN_AUTH_URL</code>.</p>
            </body>
        </html>
        """
        return HTMLResponse(content=html)

    request_access_attr = (
        f' data-request-access="{escape(request_access)}"'
        if request_access
        else ""
    )

    html = f"""
    <html>
        <head>
            <meta charset="utf-8" />
            <title>Beancount Telegram Bot</title>
        </head>
        <body>
            <h1>Beancount Telegram Bot</h1>
            <p>Sign in with your Telegram account:</p>
            <script async src="https://telegram.org/js/telegram-widget.js?22" data-telegram-login="{escape(bot_username)}" data-size="large" data-auth-url="{escape(auth_url)}"{request_access_attr} data-userpic="false"></script>
        </body>
    </html>
    """
    return HTMLResponse(content=html)


def _verify_telegram_auth(payload: dict[str, str], bot_token: str, max_age: int = 86400) -> bool:
    hash_value = payload.pop("hash", None)
    if not hash_value:
        return False

    auth_date = payload.get("auth_date")
    try:
        auth_ts = int(auth_date)
    except (TypeError, ValueError):
        return False

    if time.time() - auth_ts > max_age:
        return False

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_hash, hash_value)


@router.get("/auth/telegram")
async def telegram_auth(request: Request) -> RedirectResponse:
    settings = get_settings()
    bot_username = settings.telegram_login_bot_username
    auth_url = settings.telegram_login_auth_url
    if not bot_username or not auth_url:
        raise HTTPException(status_code=400, detail="Telegram Login is not configured.")

    payload = dict(request.query_params)
    if not payload:
        raise HTTPException(status_code=400, detail="Missing Telegram login payload.")

    data_for_check = payload.copy()
    if not _verify_telegram_auth(data_for_check, settings.telegram_token):
        raise HTTPException(status_code=400, detail="Invalid Telegram login payload.")

    user_id = str(payload.get("id"))
    session_data = {
        "id": user_id,
        "username": payload.get("username"),
        "name": payload.get("first_name") or payload.get("last_name"),
        "photo_url": payload.get("photo_url"),
    }
    request.session["telegram_user"] = session_data
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@router.post("/telegram/webhook", status_code=status.HTTP_200_OK)
async def telegram_webhook(update: Update, request: Request) -> dict[str, str]:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    fava_manager = getattr(request.app.state, "fava_manager", None)

    processor = MessageProcessor(db, fava_manager=fava_manager)
    try:
        result = await processor.handle_update(update)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process Telegram update: %s", exc)
        return {"status": "error", "detail": str(exc)}

    if result is None:
        return {"status": "ignored"}

    response: dict[str, str] = {"status": result.status, "user_id": result.user_id}
    if result.ledger_path:
        response["ledger_path"] = result.ledger_path
    if result.pending_entry_id is not None:
        response["pending_entry_id"] = str(result.pending_entry_id)
    return response


@router.get("/reports/fava", response_class=Response)
async def proxy_fava_root(request: Request) -> Response:
    session_user = request.session.get("telegram_user")
    if not session_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = str(session_user.get("id"))
    target_path = f"{user_id}/"
    return await _proxy_fava_path(request, target_path, enforce_user=user_id)


@router.get("/reports/income-statement", response_class=Response)
async def income_statement(request: Request) -> Response:
    session_user = request.session.get("telegram_user")
    if not session_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_id = str(session_user.get("id"))
    target_path = f"{user_id}/income_statement/"
    return await _proxy_fava_path(request, target_path, enforce_user=user_id)


@router.get("/static/{asset_path:path}", response_class=Response)
async def proxy_fava_static(asset_path: str) -> Response:
    fava_url = f"http://127.0.0.1:5001/static/{asset_path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            fava_response = await client.get(fava_url)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to proxy Fava static asset %s: %s", asset_path, exc)
        raise HTTPException(status_code=502, detail="Unable to load asset from Fava") from exc

    headers = {
        key: value
        for key, value in fava_response.headers.items()
        if key.lower() in {"content-type", "cache-control", "etag"}
    }

    return Response(content=fava_response.content, status_code=fava_response.status_code, headers=headers)


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    response_class=Response,
)
async def proxy_fava_catch_all(full_path: str, request: Request) -> Response:
    if not full_path:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

    session_user = request.session.get("telegram_user")
    if not session_user:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

    user_id = str(session_user.get("id"))
    stripped = full_path.strip("/")
    leading_segment = stripped.split("/", 1)[0] if stripped else ""
    if leading_segment != user_id:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

    return await _proxy_fava_path(request, stripped, enforce_user=user_id)


async def _proxy_fava_path(request: Request, target_path: str, *, enforce_user: str | None = None) -> Response:
    settings = get_settings()
    if not getattr(settings, "fava_proxy_enabled", True):
        raise HTTPException(status_code=404, detail="Fava proxy disabled")

    stripped = target_path.strip("/")
    trailing_slash = target_path.endswith("/")

    if enforce_user is not None:
        leading_segment = stripped.split("/", 1)[0] if stripped else ""
        if stripped and leading_segment != enforce_user:
            raise HTTPException(status_code=403, detail="Forbidden")
        if not stripped:
            stripped = enforce_user
            trailing_slash = True
        elif leading_segment == enforce_user and stripped.count("/") == 0:
            trailing_slash = True

    normalized_path = stripped
    if trailing_slash and stripped and not stripped.endswith("/"):
        normalized_path = stripped + "/"

    base_url = "http://127.0.0.1:5001"
    fava_url = f"{base_url}/{normalized_path}" if normalized_path else base_url

    base_url = "http://127.0.0.1:5001"
    fava_url = f"{base_url}/{normalized_path}" if normalized_path else base_url

    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
    }

    body: bytes | None = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            fava_response = await client.request(
                request.method,
                fava_url,
                params=request.query_params,
                content=body,
                headers=request_headers,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to proxy Fava request %s: %s", fava_url, exc)
        raise HTTPException(status_code=502, detail="Unable to connect to Fava") from exc

    headers = {
        key: value
        for key, value in fava_response.headers.items()
        if key.lower()
        in {"content-type", "cache-control", "etag", "last-modified", "set-cookie", "location"}
    }

    return Response(content=fava_response.content, status_code=fava_response.status_code, headers=headers)
