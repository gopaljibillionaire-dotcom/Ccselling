"""security.py - Authentication, session, CSRF, MFA, rate limiting, input validation and headers.

Design notes
------------
* Passwords: Argon2id (argon2-cffi default type), transparent rehash on login.
* Sessions: opaque random tokens; only a SHA-256 digest is stored server-side, so a database
  leak does not leak live sessions. Tokens travel in an HttpOnly cookie (customers, with CSRF
  double-protection) or an Authorization: Bearer header (admin console, immune to CSRF).
* MFA: RFC 6238 TOTP via pyotp; secrets are encrypted at rest (Fernet keyed from APP_SECRET);
  used time-steps are remembered to stop code replay.
* Rate limiting: fixed-window counters in a *shared* store (Redis if REDIS_URL, else MongoDB,
  else in-process memory for tests/dev only) so limits hold across Heroku dynos.
* NoSQL injection: all user input is validated into scalars/allow-listed fields; operator keys
  ("$..." / dotted names) are rejected before anything touches a query.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from config import settings

log = logging.getLogger("security")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)  # Argon2id
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: Optional[str], password: str) -> Tuple[bool, bool]:
    """Return (valid, needs_rehash). Always performs one hash verification (timing equalisation)."""
    try:
        _hasher.verify(stored_hash or _DUMMY_HASH, password)
        ok = stored_hash is not None
        return ok, ok and _hasher.check_needs_rehash(stored_hash)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False


def validate_password_strength(password: str) -> None:
    if not isinstance(password, str) or not (10 <= len(password) <= 128):
        raise HTTPException(422, "Password must be 10-128 characters long")
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w\s]"))
    if classes < 3 or password.lower() in {"password123", "1234567890", "qwertyuiop"}:
        raise HTTPException(422, "Password must mix at least three of: lowercase, uppercase, digits, symbols")


# ---------------------------------------------------------------------------
# Tokens and secret-at-rest encryption
# ---------------------------------------------------------------------------
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fernet() -> Fernet:
    secret = settings.app_secret or "dev-only-insecure-secret-change-me"
    key = base64.urlsafe_b64encode(hashlib.sha256(("fernet:" + secret).encode()).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> Optional[str]:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


# ---------------------------------------------------------------------------
# TOTP MFA
# ---------------------------------------------------------------------------
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=settings.brand_name)


def verify_totp(secret: str, code: str, last_step: int = 0) -> Optional[int]:
    """Return the accepted time-step, or None. Rejects replays of an already-used step."""
    if not (isinstance(code, str) and re.fullmatch(r"\d{6}", code)):
        return None
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in (-1, 0, 1):
        step = now // 30 + offset
        if step <= last_step:
            continue
        if hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


# ---------------------------------------------------------------------------
# Client identification
# ---------------------------------------------------------------------------
def client_ip(request: Request) -> str:
    """Resolve the client IP behind N trusted proxies (Heroku router appends the real address)."""
    xff = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    hops = settings.trust_proxy_hops
    if parts and hops > 0 and len(parts) >= hops:
        return parts[-hops][:64]
    return (request.client.host if request.client else "unknown")[:64]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
class SessionService:
    """Server-side sessions stored in MongoDB (collection: sessions, TTL-indexed on expires_at)."""

    def __init__(self, db: Any) -> None:
        self.col = db["sessions"]

    async def create(self, user: Dict[str, Any], request: Request, kind: str = "full") -> Tuple[str, Dict[str, Any]]:
        """Create a fresh session (always a new token => rotation on every authentication)."""
        is_admin = user.get("role") in ("admin", "staff", "superadmin")
        token = new_token()
        now = utcnow()
        if kind == "mfa_pending":
            ttl = timedelta(minutes=5)
        elif is_admin:
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
        }
        await self.col.insert_one(doc)
        return token, doc

    async def resolve(self, token: Optional[str], allow_pending: bool = False) -> Optional[Dict[str, Any]]:
        if not token or len(token) > 200:
            return None
        sid = hash_token(token)
        doc = await self.col.find_one({"_id": sid})
        if not doc:
            return None
        now = utcnow()
        expires = doc["expires_at"].replace(tzinfo=timezone.utc) if doc["expires_at"].tzinfo is None else doc["expires_at"]
        last = doc["last_seen"].replace(tzinfo=timezone.utc) if doc["last_seen"].tzinfo is None else doc["last_seen"]
        if expires <= now or last + timedelta(minutes=doc.get("idle_minutes", 60)) <= now:
            await self.col.delete_one({"_id": sid})
            return None
        if doc["kind"] != "full" and not allow_pending:
            return None
        if (now - last).total_seconds() > 60:
            await self.col.update_one({"_id": sid}, {"$set": {"last_seen": now}})
        return doc

    async def revoke(self, token: Optional[str]) -> None:
        if token:
            await self.col.delete_one({"_id": hash_token(token)})

    async def revoke_id(self, session_id: str, user_id: Any) -> int:
        res = await self.col.delete_one({"_id": session_id, "user_id": user_id})
        return res.deleted_count

    async def revoke_user(self, user_id: Any, keep_id: Optional[str] = None) -> int:
        q: Dict[str, Any] = {"user_id": user_id}
        if keep_id:
            q["_id"] = {"$ne": keep_id}
        res = await self.col.delete_many(q)
        return res.deleted_count


def check_csrf(request: Request, session: Dict[str, Any], via_cookie: bool) -> None:
    """Cookie-authenticated unsafe requests need a matching X-CSRF-Token and an allowed Origin."""
    if request.method in SAFE_METHODS or not via_cookie:
        return
    origin = request.headers.get("origin")
    if origin:
        allowed = set(settings.effective_origins())
        # Also accept the request host itself (covers reverse-proxy / missing ALLOWED_ORIGINS).
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
        if host:
            allowed.add(f"{proto}://{host.split(',')[0].strip()}")
            allowed.add(f"https://{host.split(',')[0].strip()}")
            allowed.add(f"http://{host.split(',')[0].strip()}")
        if not allowed or origin not in allowed:
            # Last resort: same-origin check via Referer / Host when allow-list is empty.
            if allowed and origin not in allowed:
                raise HTTPException(403, "Origin not allowed")
    sent = request.headers.get("x-csrf-token", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        raise HTTPException(403, "CSRF token missing or invalid")


# ---------------------------------------------------------------------------
# Rate limiting (shared store)
# ---------------------------------------------------------------------------
class RateLimiter:
    """Fixed-window counters. Backend order: Redis (REDIS_URL) -> MongoDB -> in-memory (dev/test)."""

    def __init__(self, db: Any = None) -> None:
        self._db = db
        self._redis = None
        self._mem: Dict[str, Tuple[int, float]] = {}
        if settings.redis_url:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(settings.redis_url, socket_timeout=2, decode_responses=True)
            except Exception as exc:  # pragma: no cover
                log.warning("Redis unavailable (%s); falling back to MongoDB limiter", type(exc).__name__)

    @staticmethod
    def _bucket(key: str, window: int) -> Tuple[str, int]:
        now = int(time.time())
        return f"rl:{key}:{now // window}", window - (now % window)

    async def get(self, key: str, window: int) -> Tuple[int, int]:
        bucket, ttl = self._bucket(key, window)
        try:
            if self._redis is not None:
                return int(await self._redis.get(bucket) or 0), ttl
            if self._db is not None:
                doc = await self._db["rate_limits"].find_one({"_id": bucket})
                return (doc or {}).get("n", 0), ttl
        except Exception as exc:
            log.warning("rate-limit read failed: %s", type(exc).__name__)
        n, exp = self._mem.get(bucket, (0, 0))
        return (n if exp > time.time() else 0), ttl

    async def incr(self, key: str, window: int) -> Tuple[int, int]:
        bucket, ttl = self._bucket(key, window)
        try:
            if self._redis is not None:
                n = await self._redis.incr(bucket)
                if n == 1:
                    await self._redis.expire(bucket, window + 5)
                return int(n), ttl
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
        if len(self._mem) > 50000:
            self._mem = {k: v for k, v in self._mem.items() if v[1] > time.time()}
        return n + 1, ttl

    async def clear(self, key: str, window: int) -> None:
        bucket, _ = self._bucket(key, window)
        try:
            if self._redis is not None:
                await self._redis.delete(bucket)
            elif self._db is not None:
                await self._db["rate_limits"].delete_one({"_id": bucket})
        except Exception:
            pass
        self._mem.pop(bucket, None)

    async def enforce(self, scope: str, identity: str, limit: int, window: int) -> None:
        """Count one request; raise 429 + Retry-After when the window budget is exhausted."""
        n, ttl = await self.incr(f"{scope}:{identity}", window)
        if n > limit:
            raise HTTPException(429, "Too many requests. Please try again later.", headers={"Retry-After": str(ttl)})

    async def guard_failures(self, scope: str, identity: str, limit: int, window: int) -> None:
        """Block (without counting) when failures already reached the limit - used for logins."""
        n, ttl = await self.get(f"{scope}:{identity}", window)
        if n >= limit:
            raise HTTPException(429, "Too many failed attempts. Please try again later.", headers={"Retry-After": str(ttl)})


# ---------------------------------------------------------------------------
# Input validation / NoSQL-injection prevention
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]*>")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_OBJ_ID_RE = re.compile(r"^[a-f0-9]{24}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}$")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


def clean_text(value: Any, max_len: int = 500, multiline: bool = False, label: str = "value") -> str:
    """Normalise text and strip any markup. Output is still escaped/textContent-rendered client side."""
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


def assert_safe_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise HTTPException(422, "Invalid field key")
    return key


def reject_operators(obj: Any, depth: int = 0) -> Any:
    """Recursively reject Mongo operator / dotted keys anywhere in user-supplied JSON."""
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


def scalar(value: Any, label: str = "value") -> Any:
    """Allow only plain scalars where a query value is expected (blocks {"$ne": ...} style attacks)."""
    if isinstance(value, (dict, list, tuple, set)) or value is None:
        raise HTTPException(422, f"{label} must be a simple value")
    return value


def object_id_str(value: Any, label: str = "id") -> str:
    if not isinstance(value, str) or not _OBJ_ID_RE.match(value):
        raise HTTPException(422, f"Invalid {label}")
    return value


def pick_sort(requested: str, allowed: Dict[str, Any], default: str) -> Any:
    """Map a client sort token to a server-defined sort spec (never pass client text to Mongo)."""
    return allowed.get(requested, allowed[default])


def pick_fields(data: Dict[str, Any], allowed: Iterable[str]) -> Dict[str, Any]:
    allowed_set = set(allowed)
    return {k: v for k, v in data.items() if k in allowed_set}


def validate_https_url(url: Any, label: str = "URL", max_len: int = 2048) -> str:
    if not isinstance(url, str) or len(url) > max_len:
        raise HTTPException(422, f"{label} is invalid")
    p = urlparse(url.strip())
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise HTTPException(422, f"{label} must be a plain https:// URL")
    return url.strip()


def validate_image_url(url: Any, label: str = "Image URL") -> str:
    clean = validate_https_url(url, label)
    p = urlparse(clean)
    host = (p.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in settings.image_host_allowlist):
        raise HTTPException(422, f"{label} host is not on the trusted image host allow-list")
    if not p.path.lower().endswith(IMAGE_EXTS):
        raise HTTPException(422, f"{label} must point to an image file ({', '.join(IMAGE_EXTS)})")
    return clean


def sniff_image_type(data: bytes) -> Optional[str]:
    """Magic-number sniffing so uploaded HTML/executables are never accepted as images."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------------------------------------------------------------------------
# Webhook signature
# ---------------------------------------------------------------------------
def verify_hmac_sha512(raw_body: bytes, header_value: Optional[str], key: str) -> bool:
    """OxaPay: HMAC-SHA512(raw POST body, MERCHANT_API_KEY) hex digest in the 'HMAC' header."""
    if not header_value or not key:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header_value.strip().lower())


# ---------------------------------------------------------------------------
# Security headers / CSP
# ---------------------------------------------------------------------------
def build_csp(nonce: str) -> str:
    img_hosts = " ".join(f"https://{h}" for h in settings.image_host_allowlist)
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            f"style-src 'self' 'nonce-{nonce}'",
            "style-src-attr 'unsafe-inline'",  # DOM-built inline style attributes only; scripts stay nonce-locked
            f"img-src 'self' data: blob: {img_hosts}".strip(),
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
        ]
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds CSP (per-request nonce), HSTS, and other hardening headers; enforces HTTPS in production."""

    DOC_PATHS = ("/docs", "/redoc", "/openapi.json")

    async def dispatch(self, request: Request, call_next):
        if settings.is_production and request.headers.get("x-forwarded-proto", "https") == "http":
            from starlette.responses import RedirectResponse

            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        request.state.csp_nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        h = response.headers
        if not request.url.path.startswith(self.DOC_PATHS):
            h.setdefault("Content-Security-Policy", build_csp(request.state.csp_nonce))
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if settings.is_production:
            h.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        return response


# ---------------------------------------------------------------------------
# Unit tests (run: pytest security.py -q)
# ---------------------------------------------------------------------------
def test_password_roundtrip() -> None:
    h = hash_password("Correct-Horse-9!")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "Correct-Horse-9!")[0] is True
    assert verify_password(h, "wrong")[0] is False
    assert verify_password(None, "anything")[0] is False


def test_clean_text_strips_markup() -> None:
    assert clean_text("<script>alert(1)</script>Hello") == "alert(1)Hello"
    assert "<" not in clean_text("a <img src=x onerror=1> b")
    assert "javascript:" not in clean_text("javascript:alert(1)").lower()


def test_reject_operators() -> None:
    for bad in ({"$ne": 1}, {"a": {"$gt": ""}}, {"a.b": 1}, [{"$where": "1"}]):
        try:
            reject_operators(bad)
        except HTTPException as e:
            assert e.status_code == 422
        else:
            raise AssertionError(f"accepted {bad}")
    assert reject_operators({"ok": [1, 2, {"x": "y"}]})


def test_scalar_blocks_objects() -> None:
    for bad in ({"$ne": ""}, ["a"], None):
        try:
            scalar(bad)
        except HTTPException:
            continue
        raise AssertionError("accepted non-scalar")


def test_image_url_validation() -> None:
    assert validate_image_url("https://i.ibb.co/abc/pic.jpg")
    for bad in ("http://i.ibb.co/a.jpg", "https://evil.com/a.jpg", "https://i.ibb.co/a.html", "javascript:alert(1)",
                "https://user:pw@i.ibb.co/a.jpg"):
        try:
            validate_image_url(bad)
        except HTTPException:
            continue
        raise AssertionError(f"accepted {bad}")


def test_hmac_verification() -> None:
    body, key = b'{"track_id":"1"}', "k"
    sig = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    assert verify_hmac_sha512(body, sig, key)
    assert not verify_hmac_sha512(body + b" ", sig, key)
    assert not verify_hmac_sha512(body, None, key)


def test_totp_replay_protection() -> None:
    secret = new_totp_secret()
    code = pyotp.TOTP(secret).now()
    step = verify_totp(secret, code)
    assert step
    assert verify_totp(secret, code, last_step=step) is None


def test_rate_limiter_memory() -> None:
    async def run() -> None:
        rl = RateLimiter(None)
        for _ in range(3):
            await rl.enforce("t", "ip", 3, 60)
        try:
            await rl.enforce("t", "ip", 3, 60)
        except HTTPException as e:
            assert e.status_code == 429 and "Retry-After" in e.headers
        else:
            raise AssertionError("limit not enforced")

    asyncio.run(run())


def test_sniff_image() -> None:
    assert sniff_image_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert sniff_image_type(b"<html><script>") is None
