"""Who is calling the API.

Two kinds of key are accepted on /v1:

    sk-... in api_keys    a customer's key — billed against their balance
    sk-... in API_KEYS    the operator's key — unmetered, for your own use

Operator keys come from keys.json (local) and the API_KEYS env var (deploys
whose filesystem is wiped). They live here because they are not accounts.
"""

import json
import os
import time

from fastapi import Request

from . import db
from .errors import GatewayError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def keys_file() -> str:
    return os.environ.get("KEYS_FILE") or os.path.join(ROOT, "keys.json")


def _read_file() -> dict:
    path = keys_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # A corrupt keys.json must not take the gateway down: API_KEYS still
        # works, and the CLI rewrites the file on the next key.
        return {}
    return data if isinstance(data, dict) else {}


def load_keys() -> dict:
    keys = dict(_read_file())
    for raw in os.environ.get("API_KEYS", "").replace(",", " ").split():
        if raw not in keys:
            keys[raw] = {"name": "env", "created": "-"}
    return keys


def save_key(key: str, name: str) -> None:
    """Write to keys.json only, so env-provided keys are never persisted."""
    keys = _read_file()
    keys[key] = {"name": name, "created": time.strftime("%Y-%m-%d")}
    with open(keys_file(), "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2, ensure_ascii=False)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise GatewayError(
            401,
            "Missing Authorization header. Send it as 'Authorization: Bearer sk-...'",
            code="invalid_api_key",
        )
    key = header[len("Bearer "):].strip()
    if not key.startswith("sk-"):
        raise GatewayError(401, "Invalid key format: gateway keys start with sk-", code="invalid_api_key")
    return key


def resolve_api_key(request: Request) -> dict:
    """Return the caller: an operator, or an account with a balance to spend."""
    key = _bearer(request)

    if key in load_keys():
        return {"key": key, "user_id": None, "login": None, "operator": True}

    user = db.user_for_key(key)
    if not user:
        raise GatewayError(401, "Unknown API key", code="invalid_api_key")
    if user["blocked"]:
        raise GatewayError(403, "Account is blocked, API access is closed", code="account_blocked")
    if float(user["balance"]) <= 0:
        raise GatewayError(
            402,
            "Balance is empty. Redeem an access code in the cabinet to keep using the API.",
            type="insufficient_quota",
            code="insufficient_balance",
        )

    return {"key": key, "user_id": user["id"], "login": user["login"], "operator": False}
