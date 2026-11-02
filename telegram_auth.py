"""telegram_auth.py - Official Telegram WebApp initData validation and user mapping.

Never trusts frontend-supplied Telegram IDs. HMAC-SHA256 over the data-check-string
with the bot token secret is required. Timestamp must be recent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl

from fastapi import HTTPException


# Max age of auth_date (seconds). Telegram docs recommend rejecting stale initData.
DEFAULT_MAX_AGE_SECONDS = 86400  # 24h


def _data_check_string(pairs: list[Tuple[str, str]]) -> str:
    # Exclude hash; sort keys lexicographically
    filtered = [(k, v) for k, v in pairs if k != "hash"]
    filtered.sort(key=lambda x: x[0])
    return "\n".join(f"{k}={v}" for k, v in filtered)


def validate_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> Dict[str, Any]:
    """Validate Telegram WebApp initData per official algorithm.

    Returns parsed fields including `user` dict and `auth_date` int.
    Raises HTTPException on any failure.
    """
    if not init_data or not isinstance(init_data, str):
        raise HTTPException(401, "Missing Telegram initData")
    if not bot_token:
        raise HTTPException(503, "Telegram bot is not configured")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    except Exception as exc:
        raise HTTPException(401, "Malformed Telegram initData") from exc

    data_map: Dict[str, str] = dict(pairs)
    received_hash = data_map.get("hash")
    if not received_hash:
        raise HTTPException(401, "Telegram initData missing hash")

    check_string = _data_check_string(pairs)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Invalid Telegram initData signature")

    # auth_date freshness
    try:
        auth_date = int(data_map.get("auth_date", "0"))
    except ValueError as exc:
        raise HTTPException(401, "Invalid auth_date") from exc
    now = int(time.time())
    if auth_date <= 0 or (now - auth_date) > max_age_seconds:
        raise HTTPException(401, "Telegram authentication expired")
    if auth_date > now + 60:
        # clock skew guard
        raise HTTPException(401, "Invalid auth_date")

    user_raw = data_map.get("user")
    user: Dict[str, Any] = {}
    if user_raw:
        try:
            user = json.loads(user_raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(401, "Invalid Telegram user payload") from exc
        if not isinstance(user, dict) or "id" not in user:
            raise HTTPException(401, "Telegram user payload incomplete")

    return {
        "user": user,
        "auth_date": auth_date,
        "query_id": data_map.get("query_id"),
        "start_param": data_map.get("start_param"),
        "chat_type": data_map.get("chat_type"),
        "chat_instance": data_map.get("chat_instance"),
        "raw": data_map,
    }


def telegram_photo_url(user: Dict[str, Any]) -> Optional[str]:
    """Best-effort photo URL from initData user object (photo_url field when present)."""
    url = user.get("photo_url")
    if isinstance(url, str) and url.startswith("https://"):
        return url[:2048]
    return None


def display_name(user: Dict[str, Any]) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    if name:
        return name[:80]
    un = (user.get("username") or "").strip()
    if un:
        return un[:80]
    return f"tg_{user.get('id', 'user')}"
