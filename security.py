"""security.py - Sessions, CSRF, rate limiting, Telegram WebApp validation, input sanitisation."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from config import settings

log = logging.getLogger("security")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    hops = settings.trust_proxy_hops
    if parts and hops > 0 and len(parts) >= hops:
        return parts[-hops][:64]
    return (request.client.host if request.client else "unknown")[:64]


# ---------------------------------------------------------------------------
# Telegram WebApp initData validation
# ---------------------------------------------------------------------------
def validate_telegram_webapp(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> Dict[str, Any]:
    """Validate Telegram Mini App initData signature (HMAC-SHA256).

    Returns the parsed user dict + auth_date when valid.
    Raises HTTPException(401) on failure.
    """
    if not init_data or not bot_token:
        raise HTTPException(401, "Missing Telegram auth data")
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception as exc:
        raise HTTPException(401, "Invalid Telegram auth data") from exc

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Missing hash in Telegram auth data")

    # Build data-check-string: sorted key=value joined by \n
    check_lines = [f"{k}={v}" for k, v in sorted(parsed.items())]
    data_check_string = "\n".join(check_lines)

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        raise HTTPException(401, "Invalid Telegram signature")

    auth_date = int(parsed.get("auth_date") or 0)
    if auth_date and (time.time() - auth_date) > max_age_seconds:
        raise HTTPException(401, "Telegram auth data expired")

    user_raw = parsed.get("user")
    user: Dict[str, Any] = {}
    if user_raw:
        try:
            user = json.loads(user_raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(401, "Invalid Telegram user payload") from exc

    return {
        "user": user,
        "auth_date": auth_date,
        "query_id": parsed.get("query_id"),
        "chat_type": parsed.get("chat_type"),
        "chat_instance": parsed.get("chat_instance"),
        "start_param": parsed.get("start_param"),
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
class SessionService:
    def __init__(self, db: Any) -> None:
        self.col = db["sessions"]

    async def create(self, user: Dict[str, Any], request: Request, kind: str = "full") -> Tuple[str, Dict[str, Any]]:
        is_admin = user.get("role") in ("admin", "support", "superadmin")
        token = new_token()
        now = utcnow()
        if is_admin:
            ttl = timedelta(minutes=settings.admin_session_ttl_minutes)
        else:
            ttl = timedelta(hours=settings.session_ttl_hours)
        doc = {
            "_id": hash_token(token),
            "user_id": user["_id"],
            "role": user.get("role", "customer"),
            "kind": kind,
            "csrf": new_token(24),
            "created_at": now,
            "last_seen": now,
            "expires_at": now + ttl,
            "idle_minutes": settings.admin_session_idle_minutes if is_admin else settings.session_idle_minutes,
            "ip": client_ip(request),
            "ua": (request.headers.get("user-agent") or "")[:200],
            "tg_id": user.get("tg_id"),
        }
        await self.col.insert_one(doc)
        return token, doc

    async def resolve(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token or len(token) > 200:
            return None
        sid = hash_token(token)
        doc = await self.col.find_one({"_id": sid})
        if not doc:
            return None
        now = utcnow()
        expires = doc["expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        last = doc["last_seen"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if expires <= now or last + timedelta(minutes=doc.get("idle_minutes", 60)) <= now:
            await self.col.delete_one({"_id": sid})
            return None
        if (now - last).total_seconds() > 60:
            await self.col.update_one({"_id": sid}, {"$set": {"last_seen": now}})
        return doc

    async def revoke(self, token: Optional[str]) -> None:
        if token:
            await self.col.delete_one({"_id": hash_token(token)})

    async def revoke_user(self, user_id: Any, keep_id: Optional[str] = None) -> int:
        q: Dict[str, Any] = {"user_id": user_id}
        if keep_id:
            q["_id"] = {"$ne": keep_id}
        res = await self.col.delete_many(q)
        return res.deleted_count


def check_csrf(request: Request, session: Dict[str, Any], via_cookie: bool) -> None:
    if request.method in SAFE_METHODS or not via_cookie:
        return
    origin = request.headers.get("origin")
    if origin:
        allowed = set(settings.effective_origins())
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
        if host:
            h = host.split(",")[0].strip()
            allowed.add(f"{proto}://{h}")
            allowed.add(f"https://{h}")
            allowed.add(f"http://{h}")
        if allowed and origin not in allowed:
            raise HTTPException(403, "Origin not allowed")
    sent = request.headers.get("x-csrf-token", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        raise HTTPException(403, "CSRF token missing or invalid")


# ---------------------------------------------------------------------------
# Rate limiting (Mongo / memory)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, db: Any = None) -> None:
        self._db = db
        self._mem: Dict[str, Tuple[int, float]] = {}

    @staticmethod
    def _bucket(key: str, window: int) -> Tuple[str, int]:
        now = int(time.time())
        return f"rl:{key}:{now // window}", window - (now % window)

    async def incr(self, key: str, window: int) -> Tuple[int, int]:
        bucket, ttl = self._bucket(key, window)
        try:
            if self._db is not None:
                doc = await self._db["rate_limits"].find_one_and_update(
                    {"_id": bucket},
                    {"$inc": {"n": 1}, "$setOnInsert": {"expires_at": utcnow() + timedelta(seconds=window + 5)}},
                    upsert=True,
                    return_document=True,
                )
                return int(doc["n"]), ttl
        except Exception as exc:
            log.warning("rate-limit write failed: %s", type(exc).__name__)
        n, exp = self._mem.get(bucket, (0, 0))
        if exp <= time.time():
            n, exp = 0, time.time() + window + 5
        self._mem[bucket] = (n + 1, exp)
        return n + 1, ttl

    async def enforce(self, scope: str, identity: str, limit: int, window: int) -> None:
        n, ttl = await self.incr(f"{scope}:{identity}", window)
        if n > limit:
            raise HTTPException(429, "Too many requests. Please try again later.", headers={"Retry-After": str(ttl)})


# ---------------------------------------------------------------------------
# Input sanitisation
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]*>")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_OBJ_ID_RE = re.compile(r"^[a-f0-9]{24}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}$")


def clean_text(value: Any, max_len: int = 500, multiline: bool = False, label: str = "value") -> str:
    if not isinstance(value, str):
        raise HTTPException(422, f"{label} must be text")
    value = unicodedata.normalize("NFC", value)
    value = _CTRL_RE.sub("", value)
    if not multiline:
        value = re.sub(r"\s+", " ", value)
    value = _TAG_RE.sub("", value).replace("<", "").replace(">", "")
    value = re.sub(r"(?i)\bjavascript\s*:", "", value).strip()
    if len(value) > max_len:
        raise HTTPException(422, f"{label} is too long (max {max_len})")
    return value


def clean_email(value: Any) -> str:
    v = clean_text(value, 254, label="email").lower()
    if not EMAIL_RE.match(v):
        raise HTTPException(422, "Invalid email address")
    return v


def object_id_str(value: Any, label: str = "id") -> str:
    if not isinstance(value, str) or not _OBJ_ID_RE.match(value):
        raise HTTPException(422, f"Invalid {label}")
    return value


def reject_operators(obj: Any, depth: int = 0) -> Any:
    if depth > 6:
        raise HTTPException(422, "Payload nested too deeply")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str) or k.startswith("$") or "." in k or "\x00" in k:
                raise HTTPException(422, "Illegal key in payload")
            reject_operators(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        if len(obj) > 200:
            raise HTTPException(422, "List too long")
        for v in obj:
            reject_operators(v, depth + 1)
    return obj


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
def build_csp(nonce: str) -> str:
    img_hosts = " ".join(f"https://{h}" for h in settings.image_host_allowlist)
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}' https://telegram.org",
            f"style-src 'self' 'nonce-{nonce}' 'unsafe-inline'",
            f"img-src 'self' data: blob: {img_hosts} https://*.telegram.org".strip(),
            "font-src 'self'",
            "connect-src 'self' https://api.telegram.org",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'self' https://web.telegram.org https://telegram.org",
            "form-action 'self'",
        ]
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.csp_nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        h = response.headers
        h.setdefault("Content-Security-Policy", build_csp(request.state.csp_nonce))
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")  # Telegram Mini App needs framing
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.is_production:
            h.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        return response
