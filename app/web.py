"""The shop around the gateway: accounts, sessions and the cabinet's JSON API.

The landing page and the cabinet are one static document in /static that talks
to these endpoints. Accounts are login + password (no e-mail, no confirmations)
and the session is an HttpOnly cookie, so a stolen script cannot read it.
"""

import hmac
import math
import os
import re
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import db, limits
from .config import DEFAULT_MODEL, available_models, catalog
from .errors import GatewayError

COOKIE = "fg_session"
ADMIN_COOKIE = "eqqsoai_admin"
ADMIN_SESSION_TTL = 60 * 60 * 8
LOGIN_RE = re.compile(r"^[\w.\-@]{3,64}$", re.UNICODE)
LOGIN_RPM = 20


def welcome_amount() -> float:
    """Trial balance for a new account. WELCOME_TKN=0 turns it off."""
    raw = os.environ.get("WELCOME_TKN")
    if raw is None or raw == "":
        return 10000.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 10000.0

router = APIRouter()


def _base_url(request: Request) -> str:
    host = request.headers.get("host") or "localhost:8000"
    return f"{request.url.scheme}://{host}/v1"


def _set_cookie(response: JSONResponse, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=db.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise GatewayError(400, "Тело запроса должно быть JSON", code="invalid_json")
    if not isinstance(data, dict):
        raise GatewayError(400, "Тело запроса должно быть объектом", code="invalid_json")
    return data


def current_user(request: Request):
    """The logged-in account, from the cookie or the X-Session-Token header.

    The header fallback matters inside Telegram: its web view can drop cookies
    when the app is opened from an iframe.
    """
    token = request.headers.get("x-session-token") or request.cookies.get(COOKIE, "")
    return db.user_for_session(token)


def _require_user(request: Request):
    user = current_user(request)
    if not user:
        raise GatewayError(401, "Нужно войти в кабинет", code="not_authenticated")
    if user["blocked"]:
        raise GatewayError(403, "Аккаунт заблокирован, доступ к API закрыт", code="account_blocked")
    return user


def _login_or_400(raw) -> str:
    login = (raw or "").strip() if isinstance(raw, str) else ""
    if not LOGIN_RE.match(login):
        raise GatewayError(400, "Логин должен содержать от 3 до 64 символов: буквы, цифры, . - _", param="login")
    return login


def _password_or_400(raw) -> str:
    password = raw if isinstance(raw, str) else ""
    if not 8 <= len(password) <= 256:
        raise GatewayError(400, "Пароль должен содержать от 8 до 256 символов", param="password")
    return password


def _account_payload(request: Request, user) -> dict:
    keys = [
        {
            "id": row["id"],
            "key": row["key"],
            "label": row["label"],
            "created_at": row["created_at"],
        }
        for row in db.keys_of(user["id"])
        if not row["revoked"]
    ]
    return {
        "login": user["login"],
        "balance": round(db.balance_of(user["id"]), 4),
        "blocked": bool(user["blocked"]),
        "base_url": _base_url(request),
        "default_model": DEFAULT_MODEL,
        "keys": keys,
        "stats": db.stats(user["id"]),
    }


@router.get("/api/session")
async def session(request: Request):
    user = current_user(request)
    return {
        "authenticated": bool(user),
        "login": user["login"] if user else None,
        "base_url": _base_url(request),
        "default_model": DEFAULT_MODEL,
    }


@router.get("/api/models")
async def models():
    usable = available_models()
    return {"models": [{**m, "available": m["id"] in usable} for m in catalog()]}


@router.post("/api/register")
async def register(request: Request):
    limits.hit(f"auth:{request.client.host if request.client else '?'}", LOGIN_RPM, "Слишком много попыток входа.")

    data = await _body(request)
    login = _login_or_400(data.get("login"))
    password = _password_or_400(data.get("password"))

    if db.find_user(login):
        raise GatewayError(409, "Такой логин уже зарегистрирован", param="login")

    user_id = db.create_user(login, password)
    db.new_key(user_id, "основной")
    trial = welcome_amount()
    if trial > 0:
        db.top_up(user_id, trial)
    token = db.create_session(user_id)

    response = JSONResponse({"ok": True, "token": token})
    _set_cookie(response, request, token)
    return response


@router.post("/api/login")
async def login(request: Request):
    limits.hit(f"auth:{request.client.host if request.client else '?'}", LOGIN_RPM, "Слишком много попыток входа.")

    data = await _body(request)
    login = (data.get("login") or "").strip() if isinstance(data.get("login"), str) else ""
    password = data.get("password") if isinstance(data.get("password"), str) else ""

    user = db.find_user(login) if login else None
    if not user or not db.check_password(user, password):
        raise GatewayError(401, "Неверный логин или пароль", code="bad_credentials")

    token = db.create_session(user["id"])
    response = JSONResponse({"ok": True, "token": token})
    _set_cookie(response, request, token)
    return response


@router.post("/api/logout")
async def logout(request: Request):
    db.drop_session(request.headers.get("x-session-token") or request.cookies.get(COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/api/account")
async def account(request: Request):
    return _account_payload(request, _require_user(request))


@router.post("/api/keys")
async def create_key(request: Request):
    user = _require_user(request)
    data = await _body(request)
    label = (data.get("label") or "").strip()[:40] if isinstance(data.get("label"), str) else ""

    existing = [row for row in db.keys_of(user["id"]) if not row["revoked"]]
    if len(existing) >= 5:
        raise GatewayError(400, "До 5 активных ключей на аккаунт", code="too_many_keys")

    key = db.new_key(user["id"], label or "ключ")
    return {"key": key, "account": _account_payload(request, user)}


@router.post("/api/keys/{key_id}/revoke")
async def revoke_key(request: Request, key_id: int):
    user = _require_user(request)
    with db.db() as conn:
        conn.execute(
            "UPDATE api_keys SET revoked = 1 WHERE id = ? AND user_id = ?",
            (key_id, user["id"]),
        )
    return {"ok": True, "account": _account_payload(request, user)}


@router.post("/api/redeem")
async def redeem(request: Request):
    user = _require_user(request)
    client_ip = request.client.host if request.client else "unknown"
    limits.hit(f"redeem:{client_ip}", 10, "Слишком много попыток активации кода.")
    data = await _body(request)
    code = data.get("code")
    if not isinstance(code, str) or not code.strip():
        raise GatewayError(400, "Введите код доступа", param="code")

    amount = db.redeem(code, user["id"])
    if amount is None:
        raise GatewayError(400, "Код недействителен или уже использован", code="bad_code")

    return {"amount": amount, "account": _account_payload(request, user)}



# --------------------------------------------------------------------------
# owner-only admin panel
# --------------------------------------------------------------------------


def _admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "")


def _admin_session_token(request: Request) -> str:
    return request.headers.get("x-admin-session") or request.cookies.get(ADMIN_COOKIE, "")


def _require_admin(request: Request) -> str:
    if len(_admin_password()) < 16:
        raise GatewayError(503, "Админ-панель отключена: задайте ADMIN_PASSWORD длиной не менее 16 символов", code="admin_not_configured")
    token = _admin_session_token(request)
    if not db.admin_session_valid(token):
        raise GatewayError(401, "Войдите в админ-панель", code="admin_not_authenticated")
    return token


def _set_admin_cookie(response: JSONResponse, request: Request, token: str) -> None:
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=ADMIN_SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/api/admin",
    )


@router.get("/api/admin/session")
async def admin_session(request: Request):
    authenticated = len(_admin_password()) >= 16 and db.admin_session_valid(_admin_session_token(request))
    return {"authenticated": authenticated}


@router.post("/api/admin/login")
async def admin_login(request: Request):
    configured_password = _admin_password()
    if len(configured_password) < 16:
        raise GatewayError(503, "Админ-панель отключена: задайте ADMIN_PASSWORD длиной не менее 16 символов", code="admin_not_configured")

    client_ip = request.client.host if request.client else "unknown"
    limits.hit(f"admin-login:{client_ip}", 5, "Слишком много попыток входа в админ-панель.")
    data = await _body(request)
    password = data.get("password")
    if not isinstance(password, str) or not hmac.compare_digest(
        password.encode("utf-8"), configured_password.encode("utf-8")
    ):
        raise GatewayError(401, "Неверный пароль администратора", code="bad_admin_password")

    token = secrets.token_urlsafe(32)
    db.create_admin_session(token, time.time() + ADMIN_SESSION_TTL)
    response = JSONResponse({"ok": True, "token": token})
    _set_admin_cookie(response, request, token)
    return response


@router.post("/api/admin/logout")
async def admin_logout(request: Request):
    db.drop_admin_session(_admin_session_token(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(ADMIN_COOKIE, path="/api/admin", samesite="strict")
    return response


@router.get("/api/admin/codes")
async def admin_codes(request: Request, limit: int = 50):
    _require_admin(request)
    return {"codes": db.recent_codes(limit)}


@router.post("/api/admin/codes")
async def create_admin_codes(request: Request):
    _require_admin(request)
    data = await _body(request)
    amount = data.get("amount")
    count = data.get("count", 1)
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise GatewayError(400, "Сумма TKN должна быть положительным числом", param="amount")
    try:
        amount = float(amount)
    except (OverflowError, ValueError):
        raise GatewayError(400, "Сумма TKN должна быть положительным числом", param="amount")
    if not math.isfinite(amount) or amount <= 0:
        raise GatewayError(400, "Сумма TKN должна быть положительным числом", param="amount")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise GatewayError(400, "Количество кодов должно быть от 1 до 100", param="count")

    codes = [db.make_code(amount) for _ in range(count)]
    return {"codes": [{"code": code, "amount": amount} for code in codes]}
