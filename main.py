"""main.py - FastAPI backend for the marketplace (single-module, section-organised).

Sections: 1 Setup/DB  2 Helpers  3 Auth deps  4 Models  5 Catalog helpers  6 Inventory
7 Wallet/Rewards  8 Orders/Payments  9 Public+Customer routes  10 Admin routes  11 App/CLI/tests

Backend is the source of truth for prices, stock, permissions, payment status and rewards.
"""
from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import io
import json
import logging
import re
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import httpx
from bson import ObjectId
from bson.decimal128 import Decimal128
from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

import security as sec
from config import ConfigError, settings
from security import utcnow

log = logging.getLogger("market")
BASE_DIR = Path(__file__).resolve().parent
ROLE_LEVEL = {"customer": 0, "support": 1, "admin": 2, "superadmin": 3}
STAFF_ROLES = {"support", "admin", "superadmin"}


# =============================================================================
# 1. SETUP / DATABASE
# =============================================================================
class AppState:
    client: Any = None
    db: Any = None
    sessions: Optional[sec.SessionService] = None
    limiter: sec.RateLimiter = sec.RateLimiter(None)
    http: Optional[httpx.AsyncClient] = None
    sweeper: Optional[asyncio.Task] = None


S = AppState()


def db() -> Any:
    if S.db is None:
        raise HTTPException(503, "Database is not configured. Set MONGODB_URI (MongoDB Atlas connection string).")
    return S.db


async def ensure_indexes(d: Any) -> None:
    """Idempotent index creation (also run on Heroku release phase)."""
    ix = lambda col, keys, **kw: d[col].create_index(keys, **kw)  # noqa: E731
    A, D = ASCENDING, DESCENDING
    await ix("users", [("email", A)], unique=True)
    await ix("users", [("referral_code", A)], unique=True, sparse=True)
    await ix("users", [("reset.token_hash", A)], sparse=True)
    await ix("users", [("created_at", D)])
    await ix("invitations", [("email", A)])
    await ix("invitations", [("code_hash", A)], unique=True, sparse=True)
    await ix("sessions", [("expires_at", A)], expireAfterSeconds=0)
    await ix("sessions", [("user_id", A)])
    await ix("rate_limits", [("expires_at", A)], expireAfterSeconds=0)
    await ix("products", [("slug", A)], unique=True)
    await ix("products", [("name", "text"), ("short_description", "text"), ("search_blob", "text")],
             weights={"name": 10, "short_description": 3, "search_blob": 1}, name="product_text")
    await ix("products", [("status", A), ("country", A), ("category_id", A), ("created_at", D)])
    await ix("products", [("status", A), ("sold_total", D)])
    await ix("products", [("status", A), ("final_price_minor", A)])
    await ix("products", [("name_lc", A)])
    await ix("categories", [("slug", A)], unique=True)
    await ix("categories", [("parent_id", A), ("order", A)])
    await ix("countries", [("code", A)], unique=True)
    await ix("product_schemas", [("category_id", A)])
    await ix("inventory_movements", [("product_id", A), ("created_at", D)])
    await ix("inventory_movements", [("order_id", A), ("sku", A), ("type", A)], unique=True,
             partialFilterExpression={"order_id": {"$type": "objectId"}})
    await ix("carts", [("user_id", A)], unique=True)
    await ix("orders", [("number", A)], unique=True)
    await ix("orders", [("user_id", A), ("created_at", D)])
    await ix("orders", [("user_id", A), ("idempotency_key", A)], unique=True,
             partialFilterExpression={"idempotency_key": {"$type": "string"}})
    await ix("orders", [("status", A), ("reservation_expires_at", A)])
    await ix("payment_records", [("track_id", A)], unique=True, sparse=True)
    await ix("payment_records", [("reference", A)])
    await ix("payment_records", [("status", A), ("created_at", D)])
    await ix("webhook_events", [("event_key", A)], unique=True)
    await ix("webhook_events", [("received_at", D)])
    await ix("wallet_accounts", [("user_id", A)], unique=True)
    await ix("wallet_ledger", [("idempotency_key", A)], unique=True)
    await ix("wallet_ledger", [("user_id", A), ("created_at", D)])
    await ix("memberships", [("key", A)], unique=True, sparse=True)
    await ix("reward_ledger", [("user_id", A), ("ref", A), ("type", A)], unique=True)
    await ix("reward_ledger", [("user_id", A), ("created_at", D)])
    await ix("referrals", [("referee_id", A)], unique=True)
    await ix("referrals", [("referrer_id", A)])
    await ix("reviews", [("product_id", A), ("user_id", A)], unique=True)
    await ix("support_tickets", [("user_id", A), ("created_at", D)])
    await ix("audit_logs", [("created_at", D)])
    await ix("audit_logs", [("category", A), ("created_at", D)])
    await ix("notifications", [("user_id", A), ("created_at", D)])
    await ix("newsletter", [("email", A)], unique=True)


DEFAULT_TIERS = [
    {"key": "basic", "name": "Basic", "threshold_minor": 0, "discount_bps": 0, "reward_multiplier_bps": 10000,
     "benefits": ["Order tracking", "Wishlist & saved cart"], "order": 0, "active": True},
    {"key": "bronze", "name": "Bronze", "threshold_minor": 20000, "discount_bps": 100, "reward_multiplier_bps": 11000,
     "benefits": ["1% member discount", "1.1x reward points"], "order": 1, "active": True},
    {"key": "silver", "name": "Silver", "threshold_minor": 75000, "discount_bps": 250, "reward_multiplier_bps": 12500,
     "benefits": ["2.5% member discount", "1.25x reward points", "Priority support"], "order": 2, "active": True},
    {"key": "gold", "name": "Gold", "threshold_minor": 200000, "discount_bps": 400, "reward_multiplier_bps": 15000,
     "benefits": ["4% member discount", "1.5x reward points"], "order": 3, "active": True},
    {"key": "vip", "name": "VIP", "threshold_minor": 500000, "discount_bps": 600, "reward_multiplier_bps": 20000,
     "benefits": ["6% member discount", "2x reward points", "Dedicated concierge"], "order": 4, "active": True},
]
DEFAULT_COUNTRIES = {"US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia", "DE": "Germany",
                     "FR": "France", "IN": "India", "JP": "Japan", "BR": "Brazil", "AE": "United Arab Emirates",
                     "SG": "Singapore", "TR": "Türkiye", "ES": "Spain", "IT": "Italy", "NL": "Netherlands"}
DEFAULT_SITE = {"_id": "site", "brand_name": settings.brand_name, "tagline": "Premium goods from around the world",
                "hero_title": "Discover the world, delivered.", "hero_subtitle": "Curated products by country, with member rewards.",
                "support_url": "", "support_email": "", "invite_only": settings.invite_only, "tax_rate_bps": 0,
                "points_per_unit": 1, "terms_url": ""}


async def seed_defaults(d: Any) -> None:
    """Seed configuration (not products): membership tiers, country list and site settings."""
    if await d["memberships"].count_documents({"kind": "tier"}) == 0:
        for t in DEFAULT_TIERS:
            await d["memberships"].update_one({"key": t["key"]}, {"$setOnInsert": {**t, "kind": "tier"}}, upsert=True)
    if await d["countries"].count_documents({}) == 0:
        for i, (code, name) in enumerate(DEFAULT_COUNTRIES.items()):
            await d["countries"].update_one({"code": code}, {"$setOnInsert": {"code": code, "name": name, "region": "", "enabled": True, "order": i}}, upsert=True)
    await d["site_settings"].update_one({"_id": "site"}, {"$setOnInsert": DEFAULT_SITE}, upsert=True)


# =============================================================================
# 2. HELPERS
# =============================================================================
def oid(value: Any, label: str = "id") -> ObjectId:
    return ObjectId(sec.object_id_str(value, label))


def money_to_minor(value: Any, label: str = "amount", allow_zero: bool = True) -> int:
    """Parse a decimal string/number into integer minor units (2 dp). Never uses float math."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise HTTPException(422, f"{label} must be a valid decimal number")
    if not d.is_finite() or d < 0 or d > Decimal("9999999.99") or d != d.quantize(Decimal("0.01")):
        raise HTTPException(422, f"{label} must be between 0 and 9,999,999.99 with at most 2 decimals")
    minor = int(d * 100)
    if minor == 0 and not allow_zero:
        raise HTTPException(422, f"{label} must be greater than zero")
    return minor


def minor_to_str(minor: int) -> str:
    return f"{Decimal(int(minor)) / 100:.2f}"


def apply_discount(price_minor: int, pct: int) -> int:
    return (price_minor * (100 - pct) + 50) // 100


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"
    return f"{s}-{secrets.token_hex(3)}"


def iso(dt: Any) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


async def audit(request: Optional[Request], action: str, actor: Any = None, target: Any = None,
                meta: Optional[dict] = None, category: str = "admin", level: str = "info") -> None:
    """Append-only audit trail. Failures to write must never break the request."""
    try:
        await db()["audit_logs"].insert_one({
            "action": action, "actor_id": actor, "target": str(target) if target is not None else None,
            "meta": meta or {}, "category": category, "level": level, "created_at": utcnow(),
            "ip": sec.client_ip(request) if request else None,
        })
    except Exception as exc:  # pragma: no cover
        log.error("audit write failed: %s", type(exc).__name__)


async def sensitive(request: Request, name: str, identity: Optional[str] = None) -> None:
    await S.limiter.enforce(f"sens:{name}", identity or sec.client_ip(request), settings.rl_sensitive_max, settings.rl_sensitive_window)


_cache: Dict[str, Any] = {}


async def cached(key: str, loader, ttl: int = 20):
    hit = _cache.get(key)
    if hit and hit[0] > asyncio.get_event_loop().time():
        return hit[1]
    val = await loader()
    _cache[key] = (asyncio.get_event_loop().time() + ttl, val)
    return val


def invalidate_cache() -> None:
    _cache.clear()


async def get_site() -> dict:
    return await cached("site", lambda: db()["site_settings"].find_one({"_id": "site"}), 10) or DEFAULT_SITE


async def get_tiers() -> List[dict]:
    async def load():
        return [t async for t in db()["memberships"].find({"kind": "tier", "active": True}).sort("threshold_minor", 1)]
    return await cached("tiers", load, 10)


def tier_for(user: dict, tiers: List[dict]) -> dict:
    if user.get("membership_override"):
        for t in tiers:
            if t["key"] == user["membership_override"]:
                return t
    spend = user.get("lifetime_spend_minor", 0)
    chosen = tiers[0] if tiers else {"key": "basic", "name": "Basic", "threshold_minor": 0, "discount_bps": 0, "reward_multiplier_bps": 10000, "benefits": []}
    for t in tiers:
        if spend >= t["threshold_minor"]:
            chosen = t
    return chosen


# =============================================================================
# 3. AUTH DEPENDENCIES
# =============================================================================
async def get_session(request: Request) -> Optional[dict]:
    if getattr(request.state, "session_loaded", False):
        return request.state.session
    token, via_cookie = None, False
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
    else:
        token, via_cookie = request.cookies.get(settings.cookie_name), True
    sess = await S.sessions.resolve(token) if (token and S.sessions) else None
    request.state.session_loaded, request.state.session = True, sess
    request.state.via_cookie, request.state.token = via_cookie, token
    return sess


EXEMPT_LIMIT = {"/api/health", "/api/payments/plisio/webhook"}


async def api_rate_limit(request: Request) -> None:
    """Global limiter: 100 req / 15 min per authenticated user, or per IP when anonymous."""
    if request.url.path in EXEMPT_LIMIT or not request.url.path.startswith("/api/"):
        return
    sess = await get_session(request) if S.sessions else None
    ident = f"u:{sess['user_id']}" if sess else f"ip:{sec.client_ip(request)}"
    await S.limiter.enforce("api", ident, settings.rl_api_max, settings.rl_api_window)


async def current_user(request: Request) -> dict:
    sess = await get_session(request)
    if not sess:
        raise HTTPException(401, "Authentication required")
    sec.check_csrf(request, sess, request.state.via_cookie)
    user = await db()["users"].find_one({"_id": sess["user_id"]})
    if not user or user.get("status") != "active":
        raise HTTPException(401, "Authentication required")
    request.state.user = user
    return user


async def optional_user(request: Request) -> Optional[dict]:
    sess = await get_session(request)
    if not sess:
        return None
    return await db()["users"].find_one({"_id": sess["user_id"], "status": "active"})


def require_role(minimum: str):
    async def dep(request: Request, user: dict = Depends(current_user)) -> dict:
        if ROLE_LEVEL.get(user.get("role", "customer"), 0) < ROLE_LEVEL[minimum]:
            await audit(request, "authz.denied", user["_id"], meta={"path": request.url.path}, category="security", level="warn")
            raise HTTPException(403, "Insufficient permissions")
        return user
    return dep


staff = require_role("support")
admin_user = require_role("admin")
superadmin_user = require_role("superadmin")


# =============================================================================
# 4. MODELS (extra="forbid" blocks mass-assignment of fields like role/balance)
# =============================================================================
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterIn(Strict):
    email: str
    password: str
    name: str = Field(min_length=1, max_length=80)
    invite_code: Optional[str] = Field(default=None, max_length=100)
    referral_code: Optional[str] = Field(default=None, max_length=20)
    accept_terms: bool


class LoginIn(Strict):
    email: str
    password: str = Field(max_length=128)


class CodeIn(Strict):
    code: str = Field(max_length=10)


class PasswordChangeIn(Strict):
    current_password: str = Field(max_length=128)
    new_password: str


class ResetConfirmIn(Strict):
    token: str = Field(max_length=100)
    new_password: str


class ProfileIn(Strict):
    name: Optional[str] = Field(default=None, max_length=80)
    country: Optional[str] = Field(default=None, max_length=2)
    theme: Optional[Literal["light", "dark"]] = None
    notifications: Optional[Dict[str, bool]] = None


class CartItemIn(Strict):
    product_id: str
    sku: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,40}$")
    qty: int = Field(ge=0, le=20)


class CheckoutIn(Strict):
    payment_method: Literal["plisio", "wallet"] = "plisio"


class DepositIn(Strict):
    amount: str


class ReviewIn(Strict):
    rating: int = Field(ge=1, le=5)
    title: str = Field(max_length=100)
    body: str = Field(max_length=2000)


class TicketIn(Strict):
    subject: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=3, max_length=3000)
    order_number: Optional[str] = Field(default=None, max_length=20)


class MessageIn(Strict):
    message: str = Field(min_length=1, max_length=3000)


class ApplyIn(Strict):
    email: str
    name: str = Field(max_length=80)
    message: str = Field(default="", max_length=500)


class NewsletterIn(Strict):
    email: str
    consent: bool


class ImageIn(Strict):
    url: str
    alt: str = Field(default="", max_length=140)


class VariantIn(Strict):
    sku: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,40}$")
    label: str = Field(min_length=1, max_length=60)
    price: Optional[str] = None
    stock: int = Field(default=0, ge=0, le=1_000_000)


class ProductIn(Strict):
    name: str = Field(min_length=2, max_length=140)
    short_description: str = Field(default="", max_length=240)
    description: str = Field(default="", max_length=8000)
    price: str
    discount_percent: int = Field(default=0, ge=0, le=90)
    country: str = Field(min_length=2, max_length=2)
    category_id: str
    subcategory: str = Field(default="", max_length=60)
    images: List[ImageIn] = Field(default_factory=list, max_length=12)
    variants: List[VariantIn] = Field(default_factory=list, max_length=40)
    stock: int = Field(default=0, ge=0, le=1_000_000)
    low_stock_threshold: int = Field(default=5, ge=0, le=100000)
    fields: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list, max_length=15)
    featured: bool = False
    status: Literal["draft", "published"] = "draft"
    delivery_info: str = Field(default="", max_length=500)
    refund_policy: str = Field(default="", max_length=1000)


class StatusIn(Strict):
    status: Literal["draft", "published", "archived"]


class CategoryIn(Strict):
    name: str = Field(min_length=2, max_length=60)
    parent_id: Optional[str] = None
    description: str = Field(default="", max_length=300)
    image_url: Optional[str] = None
    enabled: bool = True
    featured: bool = False
    order: int = Field(default=0, ge=0, le=10000)


class ReorderIn(Strict):
    ids: List[str] = Field(max_length=500)


class CountryIn(Strict):
    code: str = Field(pattern=r"^[A-Za-z]{2}$")
    name: str = Field(min_length=2, max_length=60)
    region: str = Field(default="", max_length=60)
    enabled: bool = True
    order: int = Field(default=0, ge=0, le=10000)


FIELD_TYPES = ["text", "textarea", "number", "decimal", "country", "region", "dropdown", "multiselect",
               "boolean", "date", "url", "image_url", "stock", "variant", "metadata"]


class FieldDef(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    label: str = Field(min_length=1, max_length=60)
    type: Literal["text", "textarea", "number", "decimal", "country", "region", "dropdown", "multiselect",
                  "boolean", "date", "url", "image_url", "stock", "variant", "metadata"]
    placeholder: str = Field(default="", max_length=100)
    help_text: str = Field(default="", max_length=200)
    required: bool = False
    min: Optional[float] = None
    max: Optional[float] = None
    options: List[str] = Field(default_factory=list, max_length=60)
    default: Optional[Any] = None
    public: bool = True
    searchable: bool = False
    filterable: bool = False
    sortable: bool = False
    position: int = Field(default=0, ge=0, le=1000)
    show_on_card: bool = False


class SchemaIn(Strict):
    category_id: Optional[str] = None
    name: str = Field(default="Template", max_length=60)
    fields: List[FieldDef] = Field(default_factory=list, max_length=60)
    active: bool = True


class AdjustIn(Strict):
    product_id: str
    sku: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,40}$")
    delta: Optional[int] = Field(default=None, ge=-1_000_000, le=1_000_000)
    set_to: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    reason: str = Field(min_length=3, max_length=200)


class ImportRow(Strict):
    product_id: str
    sku: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,40}$")
    stock: int = Field(ge=0, le=1_000_000)


class ImportIn(Strict):
    rows: List[ImportRow] = Field(max_length=2000)
    reason: str = Field(min_length=3, max_length=200)


class ThresholdIn(Strict):
    low_stock_threshold: int = Field(ge=0, le=100000)


class TierIn(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,19}$")
    name: str = Field(min_length=1, max_length=30)
    threshold_minor: int = Field(ge=0, le=10**10)
    discount_bps: int = Field(ge=0, le=5000)
    reward_multiplier_bps: int = Field(ge=0, le=100000)
    benefits: List[str] = Field(default_factory=list, max_length=10)
    order: int = Field(default=0, ge=0, le=100)
    active: bool = True


class SiteIn(Strict):
    brand_name: Optional[str] = Field(default=None, max_length=40)
    tagline: Optional[str] = Field(default=None, max_length=120)
    hero_title: Optional[str] = Field(default=None, max_length=100)
    hero_subtitle: Optional[str] = Field(default=None, max_length=200)
    support_url: Optional[str] = None
    support_email: Optional[str] = None
    terms_url: Optional[str] = None
    invite_only: Optional[bool] = None
    tax_rate_bps: Optional[int] = Field(default=None, ge=0, le=5000)
    points_per_unit: Optional[int] = Field(default=None, ge=0, le=100)


class RefundIn(Strict):
    reason: str = Field(min_length=3, max_length=300)
    restock: bool = False


class FulfillIn(Strict):
    fulfillment_status: Literal["unfulfilled", "processing", "shipped", "delivered"]


class WalletAdjustIn(Strict):
    amount_minor: int = Field(ge=-10**9, le=10**9)
    reason: str = Field(min_length=3, max_length=200)


class UserStatusIn(Strict):
    status: Literal["active", "suspended"]
    reason: str = Field(min_length=3, max_length=200)


class TierOverrideIn(Strict):
    tier_key: Optional[str] = None


class InviteIn(Strict):
    email: str


class TicketStatusIn(Strict):
    status: Literal["open", "pending", "closed"]


# =============================================================================
# 5. CATALOG HELPERS
# =============================================================================
def validate_field_value(fd: dict, raw: Any, country_codes: set) -> Any:
    """Validate one dynamic field value against its definition. Returns a normalised, JSON/BSON-safe value."""
    label, t = fd["label"], fd["type"]
    if raw is None or raw == "" or raw == []:
        if fd.get("required"):
            raise HTTPException(422, f"{label} is required")
        return None
    sec.scalar(raw, label) if t not in ("multiselect", "metadata") else None
    lo, hi = fd.get("min"), fd.get("max")
    if t in ("text", "variant", "region"):
        v = sec.clean_text(raw, int(hi) if hi else 200, label=label)
        if lo and len(v) < lo:
            raise HTTPException(422, f"{label} is too short")
        return v
    if t == "textarea":
        return sec.clean_text(raw, int(hi) if hi else 5000, multiline=True, label=label)
    if t in ("number", "stock"):
        if isinstance(raw, bool):
            raise HTTPException(422, f"{label} must be a number")
        try:
            d = Decimal(str(raw))
        except InvalidOperation:
            raise HTTPException(422, f"{label} must be a number")
        if not d.is_finite() or (t == "stock" and (d != d.to_integral_value() or d < 0)):
            raise HTTPException(422, f"{label} must be a valid {'whole non-negative ' if t == 'stock' else ''}number")
        if (lo is not None and d < Decimal(str(lo))) or (hi is not None and d > Decimal(str(hi))):
            raise HTTPException(422, f"{label} must be between {lo} and {hi}")
        return int(d) if d == d.to_integral_value() else float(d)
    if t == "decimal":
        minor = money_to_minor(raw, label)
        if (lo is not None and minor < lo * 100) or (hi is not None and minor > hi * 100):
            raise HTTPException(422, f"{label} is out of range")
        return Decimal128(minor_to_str(minor))
    if t == "country":
        v = str(raw).upper()
        if v not in country_codes:
            raise HTTPException(422, f"{label} is not a supported country")
        return v
    if t == "dropdown":
        if raw not in fd.get("options", []):
            raise HTTPException(422, f"{label} must be one of the configured options")
        return raw
    if t == "multiselect":
        if not isinstance(raw, list) or any(x not in fd.get("options", []) for x in raw):
            raise HTTPException(422, f"{label} contains invalid options")
        return list(dict.fromkeys(raw))
    if t == "boolean":
        if not isinstance(raw, bool):
            raise HTTPException(422, f"{label} must be true or false")
        return raw
    if t == "date":
        try:
            return datetime.fromisoformat(str(raw)).date().isoformat()
        except ValueError:
            raise HTTPException(422, f"{label} must be an ISO date (YYYY-MM-DD)")
    if t == "url":
        return sec.validate_https_url(raw, label)
    if t == "image_url":
        return sec.validate_image_url(raw, label)
    if t == "metadata":
        if not isinstance(raw, dict) or len(raw) > 20:
            raise HTTPException(422, f"{label} must be a small key/value object")
        sec.reject_operators(raw)
        return {sec.assert_safe_key(k): sec.clean_text(v, 200, label=label) for k, v in raw.items()}
    raise HTTPException(422, f"Unsupported field type {t}")


async def schema_fields_for(category_id: Optional[ObjectId]) -> Dict[str, dict]:
    """Merged global + category schema fields (category overrides global by key)."""
    async def load():
        return [s async for s in db()["product_schemas"].find({"active": True})]
    schemas = await cached("schemas", load, 10)
    merged: Dict[str, dict] = {}
    for s in sorted(schemas, key=lambda s: 0 if s.get("category_id") is None else 1):
        if s.get("category_id") is None or s.get("category_id") == category_id:
            for f in s.get("fields", []):
                merged[f["key"]] = f
    return merged


async def catalog_ctx() -> dict:
    async def load():
        countries = {c["code"]: c async for c in db()["countries"].find({})}
        cats = {c["_id"]: c async for c in db()["categories"].find({})}
        return {"countries": countries, "categories": cats}
    return await cached("ctx", load, 20)


def stock_state(p: dict) -> str:
    avail = sum(v.get("available", 0) for v in p.get("variants", []))
    if avail <= 0:
        return "out_of_stock"
    if avail <= p.get("low_stock_threshold", 0):
        return "low_stock"
    return "in_stock"


def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal128):
        return str(v.to_decimal())
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    return v


async def public_product(p: dict, detail: bool = False, ctx: Optional[dict] = None) -> dict:
    """Public projection: no raw stock counts, reserved/sold figures, or internal fields."""
    ctx = ctx or await catalog_ctx()
    c = ctx["countries"].get(p.get("country"), {})
    cat = ctx["categories"].get(p.get("category_id"), {})
    pct = p.get("discount_percent", 0)
    out = {
        "id": str(p["_id"]), "slug": p["slug"], "name": p["name"], "short_description": p.get("short_description", ""),
        "country": {"code": p.get("country"), "name": c.get("name", p.get("country"))},
        "category": {"id": str(p.get("category_id")), "name": cat.get("name", ""), "slug": cat.get("slug", "")},
        "subcategory": p.get("subcategory", ""), "currency": settings.default_currency,
        "price_minor": p["price_minor"], "final_price_minor": p["final_price_minor"], "discount_percent": pct,
        "images": [{"url": i["url"], "alt": i.get("alt", "")} for i in p.get("images", [])],
        "availability": stock_state(p), "in_stock": stock_state(p) != "out_of_stock", "featured": p.get("featured", False),
        "rating_avg": round(p["rating_sum"] / p["rating_count"], 2) if p.get("rating_count") else None,
        "rating_count": p.get("rating_count", 0),
    }
    sf = await schema_fields_for(p.get("category_id"))
    pub_fields = []
    for key, fd in sorted(sf.items(), key=lambda kv: kv[1].get("position", 0)):
        val = p.get("fields", {}).get(key)
        if fd.get("public") and val is not None and (detail or fd.get("show_on_card")):
            pub_fields.append({"key": key, "label": fd["label"], "type": fd["type"], "value": jsonable(val), "card": fd.get("show_on_card", False)})
    out["fields"] = pub_fields
    out["variants"] = [{"sku": v["sku"], "label": v["label"], "in_stock": v.get("available", 0) > 0,
                        "final_price_minor": apply_discount(v.get("price_minor") or p["price_minor"], pct)} for v in p.get("variants", [])]
    if detail:
        site = await get_site()
        out.update({"description": p.get("description", ""), "delivery_info": p.get("delivery_info", ""),
                    "refund_policy": p.get("refund_policy", ""), "tax_rate_bps": site.get("tax_rate_bps", 0),
                    "created_at": iso(p.get("created_at"))})
    return out


async def build_product_doc(body: ProductIn, existing: Optional[dict] = None) -> dict:
    """Validate a ProductIn against live country/category/schema data and build the DB document."""
    ctx = await catalog_ctx()
    code = body.country.upper()
    if code not in ctx["countries"]:
        raise HTTPException(422, "Unknown country")
    cat_id = oid(body.category_id, "category_id")
    if cat_id not in ctx["categories"]:
        raise HTTPException(422, "Unknown category")
    price_minor = money_to_minor(body.price, "Price", allow_zero=False)
    sf = await schema_fields_for(cat_id)
    codes = set(ctx["countries"].keys())
    unknown = set(body.fields) - set(sf)
    if unknown:
        raise HTTPException(422, f"Unknown fields: {', '.join(sorted(sec.clean_text(k, 40) for k in unknown))}")
    sec.reject_operators(body.fields)
    fields: Dict[str, Any] = {}
    for key, fd in sf.items():
        val = validate_field_value(fd, body.fields.get(key, fd.get("default")), codes)
        if val is not None:
            fields[key] = val
    variants_in = body.variants or [VariantIn(sku="DEFAULT", label="Standard", stock=body.stock)]
    if len({v.sku for v in variants_in}) != len(variants_in):
        raise HTTPException(422, "Duplicate variant SKUs")
    old = {v["sku"]: v for v in (existing or {}).get("variants", [])}
    variants = []
    for v in variants_in:
        vp = money_to_minor(v.price, "Variant price", allow_zero=False) if v.price else None
        base = old.get(v.sku)
        if base:  # keep inventory counters; stock only changes through the inventory endpoints
            variants.append({**base, "label": sec.clean_text(v.label, 60), "price_minor": vp})
        else:
            variants.append({"sku": v.sku, "label": sec.clean_text(v.label, 60), "price_minor": vp, "stock": v.stock,
                             "available": v.stock, "reserved": 0, "sold": 0})
    for sku, v in old.items():
        if sku not in {x["sku"] for x in variants}:
            if v.get("reserved", 0) > 0:
                raise HTTPException(409, f"Variant {sku} has reserved stock and cannot be removed yet")
    images = [{"url": sec.validate_image_url(i.url), "alt": sec.clean_text(i.alt, 140)} for i in body.images]
    name = sec.clean_text(body.name, 140, label="Name")
    blob_parts = [name, body.short_description] + [sec.clean_text(t, 30) for t in body.tags]
    blob_parts += [str(jsonable(fields[k])) for k, fd in sf.items() if fd.get("searchable") and k in fields]
    doc = {
        "name": name, "name_lc": name.lower(), "short_description": sec.clean_text(body.short_description, 240),
        "description": sec.clean_text(body.description, 8000, multiline=True), "price_minor": price_minor,
        "discount_percent": body.discount_percent, "final_price_minor": apply_discount(price_minor, body.discount_percent),
        "country": code, "category_id": cat_id, "subcategory": sec.clean_text(body.subcategory, 60), "images": images,
        "variants": variants, "low_stock_threshold": body.low_stock_threshold, "fields": fields,
        "tags": [sec.clean_text(t, 30) for t in body.tags], "featured": body.featured, "status": body.status,
        "delivery_info": sec.clean_text(body.delivery_info, 500), "refund_policy": sec.clean_text(body.refund_policy, 1000),
        "search_blob": " ".join(blob_parts), "updated_at": utcnow(),
    }
    return doc


def admin_product(p: dict) -> dict:
    d = jsonable({k: v for k, v in p.items() if k != "search_blob"})
    d["id"] = d.pop("_id")
    d["price"] = minor_to_str(p["price_minor"])
    d["availability"] = stock_state(p)
    return d


PRODUCT_SORTS = {"newest": [("created_at", DESCENDING)], "price_asc": [("final_price_minor", ASCENDING)],
                 "price_desc": [("final_price_minor", DESCENDING)], "popularity": [("sold_total", DESCENDING), ("created_at", DESCENDING)]}


# =============================================================================
# 6. INVENTORY (atomic conditional updates; invariant available + reserved == stock)
# =============================================================================
async def log_movement(product_id: ObjectId, sku: str, kind: str, d_stock: int, d_avail: int, d_res: int,
                       reason: str = "", actor: Any = None, order_id: Optional[ObjectId] = None) -> None:
    doc = {"product_id": product_id, "sku": sku, "type": kind, "delta_stock": d_stock, "delta_available": d_avail,
           "delta_reserved": d_res, "reason": reason, "actor_id": actor, "created_at": utcnow()}
    if order_id:
        doc["order_id"] = order_id
    try:
        await db()["inventory_movements"].insert_one(doc)
    except DuplicateKeyError:
        pass


async def adjust_stock(pid: ObjectId, sku: str, delta: int, reason: str, actor: Any, kind: str = "adjust") -> None:
    if delta == 0:
        raise HTTPException(422, "Adjustment must be non-zero")
    elem: Dict[str, Any] = {"sku": sku}
    if delta < 0:
        elem["available"] = {"$gte": -delta}
    res = await db()["products"].update_one({"_id": pid, "variants": {"$elemMatch": elem}},
                                            {"$inc": {"variants.$.stock": delta, "variants.$.available": delta}, "$set": {"updated_at": utcnow()}})
    if res.modified_count == 0:
        raise HTTPException(409, "SKU not found or insufficient unreserved stock for this decrease")
    await log_movement(pid, sku, kind, delta, delta, 0, reason, actor)


async def reserve_lines(lines: List[dict], order_id: ObjectId) -> None:
    done: List[dict] = []
    for ln in lines:
        res = await db()["products"].update_one(
            {"_id": ln["product_id"], "status": "published", "variants": {"$elemMatch": {"sku": ln["sku"], "available": {"$gte": ln["qty"]}}}},
            {"$inc": {"variants.$.available": -ln["qty"], "variants.$.reserved": ln["qty"]}})
        if res.modified_count == 0:
            await release_lines(done, order_id, log_it=False)
            raise HTTPException(409, f"Insufficient stock for {ln['name']}")
        done.append(ln)
        await log_movement(ln["product_id"], ln["sku"], "reserve", 0, -ln["qty"], ln["qty"], "checkout", None, order_id)


async def release_lines(lines: List[dict], order_id: ObjectId, log_it: bool = True) -> None:
    for ln in lines:
        res = await db()["products"].update_one(
            {"_id": ln["product_id"], "variants": {"$elemMatch": {"sku": ln["sku"], "reserved": {"$gte": ln["qty"]}}}},
            {"$inc": {"variants.$.available": ln["qty"], "variants.$.reserved": -ln["qty"]}})
        if res.modified_count and log_it:
            await log_movement(ln["product_id"], ln["sku"], "release", 0, ln["qty"], -ln["qty"], "reservation released", None, order_id)


async def commit_sale(order: dict) -> None:
    for it in order["items"]:
        res = await db()["products"].update_one(
            {"_id": it["product_id"], "variants": {"$elemMatch": {"sku": it["sku"], "reserved": {"$gte": it["qty"]}}}},
            {"$inc": {"variants.$.reserved": -it["qty"], "variants.$.stock": -it["qty"], "variants.$.sold": it["qty"], "sold_total": it["qty"]}})
        if res.modified_count:
            await log_movement(it["product_id"], it["sku"], "sale", -it["qty"], 0, -it["qty"], "payment verified", None, order["_id"])
        else:
            await audit(None, "inventory.commit_anomaly", target=order["number"], meta={"sku": it["sku"]}, category="security", level="error")


# =============================================================================
# 7. WALLET & REWARDS
# =============================================================================
async def wallet_post(user_id: ObjectId, delta_minor: int, kind: str, idem_key: str, ref: str = "", actor: Any = None, note: str = "") -> dict:
    """Idempotent ledger posting. Ledger row is inserted first (unique idempotency key), then balance moves atomically."""
    await db()["wallet_accounts"].update_one({"user_id": user_id}, {"$setOnInsert": {"user_id": user_id, "balance_minor": 0, "currency": settings.default_currency, "created_at": utcnow()}}, upsert=True)
    entry = {"user_id": user_id, "type": kind, "amount_minor": delta_minor, "ref": ref, "idempotency_key": idem_key,
             "status": "pending", "actor_id": actor, "note": note, "created_at": utcnow()}
    try:
        ins = await db()["wallet_ledger"].insert_one(entry)
    except DuplicateKeyError:
        existing = await db()["wallet_ledger"].find_one({"idempotency_key": idem_key})
        return {"duplicate": True, "entry": existing}
    cond: Dict[str, Any] = {"user_id": user_id}
    if delta_minor < 0:
        cond["balance_minor"] = {"$gte": -delta_minor}
    acct = await db()["wallet_accounts"].find_one_and_update(cond, {"$inc": {"balance_minor": delta_minor}}, return_document=ReturnDocument.AFTER)
    if not acct:
        await db()["wallet_ledger"].update_one({"_id": ins.inserted_id}, {"$set": {"status": "failed"}})
        raise HTTPException(402, "Insufficient wallet balance")
    await db()["wallet_ledger"].update_one({"_id": ins.inserted_id}, {"$set": {"status": "posted", "balance_after_minor": acct["balance_minor"]}})
    return {"duplicate": False, "balance_minor": acct["balance_minor"]}


async def award_points(user: dict, order: dict) -> None:
    site = await get_site()
    tiers = await get_tiers()
    tier = tier_for(user, tiers)
    pts = (order["total_minor"] * tier.get("reward_multiplier_bps", 10000) * site.get("points_per_unit", 1)) // (100 * 10000)
    if pts <= 0:
        return
    try:
        await db()["reward_ledger"].insert_one({"user_id": user["_id"], "ref": str(order["_id"]), "type": "earn", "points": pts, "order_number": order["number"], "created_at": utcnow()})
    except DuplicateKeyError:
        return  # webhook retry: reward already granted
    await db()["users"].update_one({"_id": user["_id"]}, {"$inc": {"points_balance": pts}})
    await db()["orders"].update_one({"_id": order["_id"]}, {"$set": {"points_earned": pts}})


async def notify(user_id: ObjectId, title: str, body: str) -> None:
    await db()["notifications"].insert_one({"user_id": user_id, "title": title, "body": body, "read": False, "created_at": utcnow()})


# =============================================================================
# 8. ORDERS & PAYMENTS (Plisio)
# =============================================================================
ORDER_TRANSITIONS = {"created": {"pending", "failed", "cancelled", "expired", "paid"}, "pending": {"paid", "failed", "expired", "cancelled"},
                     "paid": {"refunded"}, "failed": set(), "expired": set(), "cancelled": set(), "refunded": set()}


def can_transition(old: str, new: str) -> bool:
    return new in ORDER_TRANSITIONS.get(old, set())


async def transition(col: str, doc_id: Any, new: str, note: str = "", actor: Any = None, extra: Optional[dict] = None) -> Optional[dict]:
    """Atomic state change; only succeeds from states that may legally move to `new`."""
    from_states = [s for s, targets in ORDER_TRANSITIONS.items() if new in targets]
    return await db()[col].find_one_and_update(
        {"_id": doc_id, "status": {"$in": from_states}},
        {"$set": {"status": new, "updated_at": utcnow(), **(extra or {})}, "$push": {"history": {"status": new, "at": utcnow(), "note": note, "actor": actor}}},
        return_document=ReturnDocument.AFTER)


async def price_cart(user: dict, items: List[dict], strict: bool = False) -> dict:
    """Server-side pricing from current DB data. Client-supplied prices are never read."""
    if not items:
        return {"lines": [], "subtotal_minor": 0, "member_discount_minor": 0, "tax_minor": 0, "total_minor": 0, "currency": settings.default_currency}
    ids = [oid(i["product_id"]) for i in items]
    prods = {p["_id"]: p async for p in db()["products"].find({"_id": {"$in": ids}})}
    lines, problems = [], []
    for it in items:
        p = prods.get(ObjectId(it["product_id"]))
        v = next((x for x in (p or {}).get("variants", []) if x["sku"] == it["sku"]), None)
        if not p or not v or p.get("status") != "published":
            problems.append(f"{(p or {}).get('name', 'An item')} is no longer available")
            continue
        unit = apply_discount(v.get("price_minor") or p["price_minor"], p.get("discount_percent", 0))
        ok = v.get("available", 0) >= it["qty"]
        if not ok:
            problems.append(f"Only part of the requested quantity of {p['name']} is available")
        lines.append({"product_id": p["_id"], "slug": p["slug"], "sku": v["sku"], "name": p["name"], "variant_label": v["label"], "qty": it["qty"],
                      "unit_price_minor": unit, "list_price_minor": v.get("price_minor") or p["price_minor"], "discount_percent": p.get("discount_percent", 0),
                      "image": (p.get("images") or [{}])[0].get("url"), "line_total_minor": unit * it["qty"], "available": ok})
    if strict and problems:
        raise HTTPException(409, "; ".join(problems))
    site, tiers = await get_site(), await get_tiers()
    tier = tier_for(user, tiers)
    subtotal = sum(l["line_total_minor"] for l in lines)
    member = (subtotal * tier.get("discount_bps", 0) + 5000) // 10000
    tax = ((subtotal - member) * site.get("tax_rate_bps", 0) + 5000) // 10000
    return {"lines": lines, "subtotal_minor": subtotal, "member_discount_minor": member, "tax_minor": tax,
            "total_minor": subtotal - member + tax, "currency": settings.default_currency, "tier": tier["key"], "problems": problems}


def public_order(o: dict, pay: Optional[dict] = None) -> dict:
    d = {"number": o["number"], "status": o["status"], "fulfillment_status": o.get("fulfillment_status", "unfulfilled"),
         "items": [{k: jsonable(v) for k, v in it.items() if k != "product_id"} | {"product_id": str(it["product_id"])} for it in o["items"]],
         "subtotal_minor": o["subtotal_minor"], "member_discount_minor": o["member_discount_minor"], "tax_minor": o["tax_minor"],
         "total_minor": o["total_minor"], "currency": o["currency"], "payment_method": o["payment_method"],
         "created_at": iso(o["created_at"]), "paid_at": iso(o.get("paid_at")), "points_earned": o.get("points_earned", 0),
         "refund_total_minor": o.get("refund_total_minor", 0), "history": [{"status": h["status"], "at": iso(h["at"])} for h in o.get("history", [])]}
    if pay and o["status"] in ("created", "pending") and pay.get("payment_url"):
        d["payment_url"] = pay["payment_url"]
    return d


async def plisio_request(method: str, path: str, params: Optional[dict] = None) -> dict:
    """Call the Plisio API. Auth: 'api_key' query parameter."""
    if not settings.plisio_enabled:
        raise HTTPException(503, "Payments are not configured. Set PLISIO_API_KEY.")
    assert S.http is not None
    url = f"{settings.plisio_base_url}{path}"
    headers = {"Content-Type": "application/json"}
    
    # Plisio API uses GET for invoices with query params, POST for others
    payload = params or {}
    payload["api_key"] = settings.plisio_api_key
    
    try:
        if method == "GET":
            r = await S.http.get(url, params=payload, headers=headers)
        else:
            r = await S.http.post(url, json=payload, headers=headers)
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.error("Plisio request failed: %s", type(exc).__name__)
        raise HTTPException(502, "Payment provider is temporarily unavailable. Please try again.")
    if r.status_code != 200 or data.get("status") == "error":
        log.error("Plisio error status=%s body=%s", r.status_code, json.dumps(data)[:300])
        raise HTTPException(502, "Payment provider rejected the request. Please contact support if this persists.")
    return data.get("data") or {}


async def create_invoice(user: dict, reference: str, amount_minor: int, purpose: str, order_id: Optional[ObjectId], description: str) -> dict:
    await S.limiter.enforce("pay", str(user["_id"]), settings.rl_payment_max, settings.rl_payment_window)
    
    # Plisio expects amount in standard units, not minor units
    amount = float(Decimal(amount_minor) / 100)
    
    data = await plisio_request("GET", "/shops", {
        "amount": amount,
        "currency": settings.default_currency,
        "order_name": description[:100],
        "order_number": reference,
        "email": user["email"],
        "callback_url": f"{settings.public_app_url}/api/payments/plisio/webhook",
        "success_url": f"{settings.public_app_url}/?order={reference}",
        "fail_url": f"{settings.public_app_url}/?order={reference}&status=failed"
    })
    
    url = data.get("invoice_url", "")
    host = (sec.urlparse(url).hostname or "") if url else ""
    if not data.get("txn_id") or sec.urlparse(url).scheme != "https" or not (host == "plisio.net" or host.endswith(".plisio.net")):
        raise HTTPException(502, "Payment provider returned an unexpected response")
        
    pay = {"provider": "plisio", "purpose": purpose, "reference": reference, "order_id": order_id, "user_id": user["_id"], "track_id": str(data["txn_id"]),
           "payment_url": url, "expected_minor": amount_minor, "currency": settings.default_currency, "status": "pending", "verified_minor": None,
           "verification": None, "sandbox": settings.plisio_sandbox, "created_at": utcnow(), "updated_at": utcnow(),
           "history": [{"status": "pending", "at": utcnow(), "note": "invoice created", "actor": None}]}
    await db()["payment_records"].insert_one(pay)
    return pay


async def provider_state(track_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", track_id):
        raise HTTPException(502, "Invalid provider reference")
    return await plisio_request("GET", f"/operations/{track_id}")


def map_provider_status(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in ("completed", "confirmed"):
        return "paid"
    if s in ("expired", "cancelled", "error", "failed"):
        return "failed"
    return "pending"  # new / waiting / pending


def verify_amounts(pay: dict, data: dict) -> Optional[str]:
    """Return None when provider data is consistent with our record, else a reason string."""
    try:
        paid = Decimal(str(data.get("amount")))
    except (InvalidOperation, TypeError):
        return "amount missing or malformed"
    if paid != Decimal(pay["expected_minor"]) / 100:
        return f"amount mismatch (expected {minor_to_str(pay['expected_minor'])}, provider {paid})"
    if str(data.get("currency", "")).upper() != pay["currency"].upper():
        return "currency mismatch"
    if str(data.get("order_number", "")) != pay["reference"]:
        return "order reference mismatch"
    return None


async def apply_provider_state(pay: dict, data: dict, source: str) -> str:
    """Idempotently apply an independently verified provider state to payment, order and wallet."""
    new = map_provider_status(data.get("status", ""))
    mismatch = verify_amounts(pay, data)
    verification = {"checked_at": utcnow(), "provider_status": data.get("status"), "ok": mismatch is None, "reason": mismatch, "source": source}
    await db()["payment_records"].update_one({"_id": pay["_id"]}, {"$set": {"verification": verification, "updated_at": utcnow()}})
    if mismatch:
        await audit(None, "payment.inconsistent", target=pay["reference"], meta={"reason": mismatch, "source": source}, category="security", level="error")
        return "inconsistent"
    if new == "pending":
        return "pending"
    claimed = await transition("payment_records", pay["_id"], new, note=source, extra={"verified_minor": pay["expected_minor"]} if new == "paid" else None)
    if new == "paid":
        if claimed is None and (await db()["payment_records"].find_one({"_id": pay["_id"]}))["status"] != "paid":
            return "ignored"
        await db()["payment_records"].update_one({"_id": pay["_id"]}, {"$set": {"paid_at": utcnow()}})
        if pay["purpose"] == "wallet_deposit":
            posted = await wallet_post(pay["user_id"], pay["expected_minor"], "deposit", f"dep:{pay['track_id']}", ref=pay["reference"])
            if not posted["duplicate"]:
                await notify(pay["user_id"], "Wallet funded", f"{minor_to_str(pay['expected_minor'])} {pay['currency']} was added to your wallet.")
        else:
            await settle_order(pay)
        return "paid"
    if pay.get("order_id"):
        order = await transition("orders", pay["order_id"], new, note=f"payment {new}")
        if order:
            await release_once(order)
    return new


async def release_once(order: dict) -> None:
    claimed = await db()["orders"].find_one_and_update({"_id": order["_id"], "reservation_released": {"$ne": True}, "inventory_committed": {"$ne": True}},
                                                       {"$set": {"reservation_released": True}})
    if claimed:
        await release_lines(order["items"], order["_id"])


async def settle_order(pay: dict) -> None:
    order = await transition("orders", pay["order_id"], "paid", note="payment verified", extra={"paid_at": utcnow()})
    if order is None:
        order = await db()["orders"].find_one({"_id": pay["order_id"], "status": "paid"})
        if order is None:  # order already expired/cancelled: keep the money as store credit, flag for review
            await wallet_post(pay["user_id"], pay["expected_minor"], "deposit", f"latepay:{pay['track_id']}", ref=pay["reference"], note="payment arrived after order expired")
            await audit(None, "payment.late_after_expiry", target=pay["reference"], category="security", level="warn")
            return
    await complete_paid_steps(order)


async def claim_flag(order_id: ObjectId, flag: str) -> Optional[dict]:
    return await db()["orders"].find_one_and_update({"_id": order_id, flag: {"$ne": True}}, {"$set": {flag: True}}, return_document=ReturnDocument.AFTER)


async def complete_paid_steps(order: dict) -> None:
    """Every step is claim-guarded, so webhook retries / reconciliation can re-run this safely."""
    if await claim_flag(order["_id"], "inventory_committed"):
        await commit_sale(order)
    if await claim_flag(order["_id"], "rewards_issued"):
        user = await db()["users"].find_one({"_id": order["user_id"]})
        if user:
            await award_points(user, order)
            await db()["users"].update_one({"_id": user["_id"]}, {"$inc": {"lifetime_spend_minor": order["total_minor"]}})
            ref = await db()["referrals"].find_one_and_update({"referee_id": user["_id"], "status": "pending"}, {"$set": {"status": "rewarded", "rewarded_at": utcnow()}})
            if ref:
                try:
                    await db()["reward_ledger"].insert_one({"user_id": ref["referrer_id"], "ref": str(user["_id"]), "type": "referral", "points": settings.referral_reward_points, "created_at": utcnow()})
                    await db()["users"].update_one({"_id": ref["referrer_id"]}, {"$inc": {"points_balance": settings.referral_reward_points}})
                except DuplicateKeyError:
                    pass
            await notify(order["user_id"], f"Order {order['number']} confirmed", "Your payment was verified. Thank you!")


async def sweep_once() -> None:
    """Reconciliation + reservation expiry. Safe to run concurrently on many dynos."""
    now = utcnow()
    async for order in db()["orders"].find({"status": {"$in": ["created", "pending"]}, "reservation_expires_at": {"$lt": now}}).limit(50):
        pay = await db()["payment_records"].find_one({"order_id": order["_id"]})
        if pay and pay.get("track_id") and settings.plisio_enabled:
            try:
                data = await provider_state(pay["track_id"])
            except HTTPException:
                continue  # provider outage: leave reservation, retry next sweep
            if await apply_provider_state(pay, data, "sweeper") in ("paid", "inconsistent"):
                continue
        fresh = await transition("orders", order["_id"], "expired", note="reservation expired")
        if fresh:
            if pay:
                await transition("payment_records", pay["_id"], "expired", note="reservation expired")
            await release_once(fresh)


async def sweeper_loop() -> None:
    while True:
        try:
            await sweep_once()
        except Exception as exc:
            log.error("sweeper error: %s", type(exc).__name__)
        await asyncio.sleep(max(10, settings.reconcile_interval_seconds))


# =============================================================================
# 9. PUBLIC + CUSTOMER ROUTES
# =============================================================================
api = APIRouter(prefix="/api", dependencies=[Depends(api_rate_limit)])


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(settings.cookie_name, token, max_age=settings.session_ttl_hours * 3600, httponly=True,
                        secure=settings.cookie_secure, samesite=settings.cookie_samesite, path="/")


def me_payload(user: dict, tiers: List[dict]) -> dict:
    tier = tier_for(user, tiers)
    return {"id": str(user["_id"]), "email": user["email"], "name": user.get("name", ""), "role": user.get("role", "customer"),
            "country": user.get("country"), "theme": user.get("theme"), "notifications": user.get("notifications", {}),
            "tier": {"key": tier["key"], "name": tier["name"]}, "points_balance": user.get("points_balance", 0),
            "referral_code": user.get("referral_code"), "wishlist": [str(x) for x in user.get("wishlist", [])], "created_at": iso(user.get("created_at"))}


@api.get("/health")
async def health() -> dict:
    ok = False
    if S.db is not None:
        try:
            await S.client.admin.command("ping")
            ok = True
        except Exception:
            ok = False
    return {"status": "ok" if ok else "degraded", "database": "up" if ok else "down", "payments_configured": settings.plisio_enabled,
            "uploads_configured": settings.imgbb_enabled, "environment": settings.app_env}


@api.get("/config")
async def public_config() -> dict:
    site = await get_site()
    tiers = await get_tiers()
    return {"brand_name": site.get("brand_name"), "tagline": site.get("tagline"), "hero_title": site.get("hero_title"), "hero_subtitle": site.get("hero_subtitle"),
            "support_url": site.get("support_url"), "support_email": site.get("support_email"), "terms_url": site.get("terms_url"),
            "invite_only": site.get("invite_only", False), "currency": settings.default_currency, "payments_enabled": settings.plisio_enabled,
            "tax_rate_bps": site.get("tax_rate_bps", 0),
            "tiers": [{"key": t["key"], "name": t["name"], "threshold_minor": t["threshold_minor"], "discount_bps": t["discount_bps"],
                       "reward_multiplier_bps": t["reward_multiplier_bps"], "benefits": t.get("benefits", [])} for t in tiers]}


# ---- auth -------------------------------------------------------------------
@api.post("/auth/register", status_code=201)
async def register(body: RegisterIn, request: Request, response: Response) -> dict:
    await sensitive(request, "register")
    if not body.accept_terms:
        raise HTTPException(422, "You must accept the terms to register")
    email = sec.clean_email(body.email)
    sec.validate_password_strength(body.password)
    site = await get_site()
    if site.get("invite_only", False):
        if not body.invite_code:
            raise HTTPException(403, "Registration is by invitation only")
        inv = await db()["invitations"].find_one_and_update(
            {"email": email, "code_hash": sec.hash_token(body.invite_code), "status": "approved", "expires_at": {"$gt": utcnow()}},
            {"$set": {"status": "used", "used_at": utcnow()}})
        if not inv:
            raise HTTPException(403, "Invalid or expired invitation")
    user = {"email": email, "name": sec.clean_text(body.name, 80), "password_hash": sec.hash_password(body.password), "role": "customer", "status": "active",
            "created_at": utcnow(), "lifetime_spend_minor": 0, "points_balance": 0, "referral_code": secrets.token_hex(4).upper(), "wishlist": [],
            "notifications": {"orders": True, "promotions": False}, "theme": None, "country": None}
    try:
        res = await db()["users"].insert_one(user)
    except DuplicateKeyError:
        raise HTTPException(409, "Unable to create an account with these details")
    user["_id"] = res.inserted_id
    if body.referral_code:
        referrer = await db()["users"].find_one({"referral_code": sec.scalar(body.referral_code.upper()), "status": "active"})
        if referrer:
            await db()["referrals"].update_one({"referee_id": user["_id"]}, {"$setOnInsert": {"referrer_id": referrer["_id"], "referee_id": user["_id"], "status": "pending", "created_at": utcnow()}}, upsert=True)
    token, sess = await S.sessions.create(user, request)
    set_session_cookie(response, token)
    await audit(request, "auth.register", user["_id"], category="security")
    return {"user": me_payload(user, await get_tiers()), "csrf": sess["csrf"]}


@api.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    ip, email = sec.client_ip(request), sec.clean_email(body.email) if sec.EMAIL_RE.match(body.email or "") else "invalid"
    ident = f"{email}|{ip}"
    await S.limiter.guard_failures("login", ident, settings.rl_login_max, settings.rl_login_window)
    await S.limiter.guard_failures("login_ip", ip, settings.rl_login_max * 3, settings.rl_login_window)
    user = await db()["users"].find_one({"email": email}) if email != "invalid" else None
    ok, rehash = sec.verify_password(user.get("password_hash") if user else None, body.password)
    if not ok or user.get("status") != "active":
        await S.limiter.incr(f"login:{ident}", settings.rl_login_window)
        await S.limiter.incr(f"login_ip:{ip}", settings.rl_login_window)
        await audit(request, "auth.login_failed", user["_id"] if user else None, meta={"email_hash": sec.hash_token(email)[:12]}, category="security", level="warn")
        raise HTTPException(401, "Invalid email or password")
    await S.limiter.clear(f"login:{ident}", settings.rl_login_window)
    if rehash:
        await db()["users"].update_one({"_id": user["_id"]}, {"$set": {"password_hash": sec.hash_password(body.password)}})
    # rotate: any pre-existing session cookie is revoked
    await S.sessions.revoke(request.cookies.get(settings.cookie_name))
    if user.get("role") in STAFF_ROLES:
        if settings.admin_require_mfa:
            token, _ = await S.sessions.create(user, request, kind="mfa_pending")
            enrolled = bool(user.get("mfa", {}).get("enabled"))
            await audit(request, "auth.admin_password_ok", user["_id"], category="security")
            return {"mfa_required": True, "mfa_enrolled": enrolled, "mfa_token": token}
        token, sess = await S.sessions.create(user, request)
        return {"token": token, "csrf": sess["csrf"], "user": me_payload(user, await get_tiers()), "expires_at": iso(sess["expires_at"])}
    token, sess = await S.sessions.create(user, request)
    set_session_cookie(response, token)
    await audit(request, "auth.login", user["_id"], category="security")
    return {"user": me_payload(user, await get_tiers()), "csrf": sess["csrf"]}


async def pending_session(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
    sess = await S.sessions.resolve(token, allow_pending=True) if token else None
    if not sess or sess["kind"] != "mfa_pending":
        raise HTTPException(401, "MFA session expired. Please sign in again.")
    return sess


@api.post("/auth/mfa/setup")
async def mfa_setup(request: Request, sess: dict = Depends(pending_session)) -> dict:
    user = await db()["users"].find_one({"_id": sess["user_id"]})
    if user.get("mfa", {}).get("enabled"):
        raise HTTPException(409, "MFA is already enrolled for this account")
    secret = sec.new_totp_secret()
    await db()["users"].update_one({"_id": user["_id"]}, {"$set": {"mfa.pending_secret": sec.encrypt_secret(secret)}})
    return {"secret": secret, "otpauth_uri": sec.totp_uri(secret, user["email"])}


@api.post("/auth/mfa/verify")
async def mfa_verify(body: CodeIn, request: Request, sess: dict = Depends(pending_session)) -> dict:
    await S.limiter.enforce("mfa", str(sess["user_id"]), 8, 300)
    user = await db()["users"].find_one({"_id": sess["user_id"]})
    mfa = user.get("mfa", {})
    enc = mfa.get("secret") if mfa.get("enabled") else mfa.get("pending_secret")
    secret = sec.decrypt_secret(enc) if enc else None
    step = sec.verify_totp(secret, body.code, mfa.get("last_step", 0)) if secret else None
    if not step:
        await audit(request, "auth.mfa_failed", user["_id"], category="security", level="warn")
        raise HTTPException(401, "Invalid authentication code")
    upd = {"mfa.last_step": step, "mfa.enabled": True, "mfa.secret": enc}
    await db()["users"].update_one({"_id": user["_id"]}, {"$set": upd, "$unset": {"mfa.pending_secret": ""}})
    auth = request.headers.get("authorization", "")
    await S.sessions.revoke(auth[7:].strip())
    token, full = await S.sessions.create(user, request)
    await audit(request, "auth.admin_login", user["_id"], category="security")
    return {"token": token, "csrf": full["csrf"], "user": me_payload(user, await get_tiers()), "expires_at": iso(full["expires_at"])}


@api.post("/auth/logout")
async def logout(request: Request, response: Response, user: dict = Depends(current_user)) -> dict:
    await S.sessions.revoke(request.state.token)
    response.delete_cookie(settings.cookie_name, path="/")
    await audit(request, "auth.logout", user["_id"], category="security")
    return {"ok": True}


@api.get("/auth/me")
async def me(request: Request, user: dict = Depends(current_user)) -> dict:
    sess = request.state.session
    return {"user": me_payload(user, await get_tiers()), "csrf": sess["csrf"], "session_expires_at": iso(sess["expires_at"])}


@api.post("/auth/password-reset/confirm")
async def reset_confirm(body: ResetConfirmIn, request: Request) -> dict:
    await S.limiter.enforce("reset", sec.client_ip(request), settings.rl_reset_max, settings.rl_reset_window)
    sec.validate_password_strength(body.new_password)
    user = await db()["users"].find_one_and_update({"reset.token_hash": sec.hash_token(body.token), "reset.expires_at": {"$gt": utcnow()}},
                                                   {"$set": {"password_hash": sec.hash_password(body.new_password)}, "$unset": {"reset": ""}})
    if not user:
        raise HTTPException(400, "Reset link is invalid or has expired")
    await S.sessions.revoke_user(user["_id"])
    await audit(request, "auth.password_reset", user["_id"], category="security")
    return {"ok": True}


@api.post("/invitations/apply", status_code=202)
async def apply_invitation(body: ApplyIn, request: Request) -> dict:
    await sensitive(request, "apply")
    email = sec.clean_email(body.email)
    await db()["invitations"].update_one({"email": email, "status": {"$in": ["pending", "rejected"]}},
                                         {"$set": {"name": sec.clean_text(body.name, 80), "message": sec.clean_text(body.message, 500), "status": "pending", "updated_at": utcnow()},
                                          "$setOnInsert": {"email": email, "created_at": utcnow()}}, upsert=True)
    return {"ok": True, "message": "Application received. You will hear from us if approved."}


# ---- account ----------------------------------------------------------------
@api.patch("/account/profile")
async def update_profile(body: ProfileIn, user: dict = Depends(current_user)) -> dict:
    upd: Dict[str, Any] = {}
    if body.name is not None:
        upd["name"] = sec.clean_text(body.name, 80)
    if body.country is not None:
        if body.country and body.country.upper() not in (await catalog_ctx())["countries"]:
            raise HTTPException(422, "Unknown country")
        upd["country"] = body.country.upper() or None
    if body.theme is not None:
        upd["theme"] = body.theme
    if body.notifications is not None:
        upd["notifications"] = {k: v for k, v in body.notifications.items() if k in ("orders", "promotions", "support")}
    if upd:
        await db()["users"].update_one({"_id": user["_id"]}, {"$set": upd})
    return me_payload({**user, **upd}, await get_tiers())


@api.post("/account/password")
async def change_password(body: PasswordChangeIn, request: Request, user: dict = Depends(current_user)) -> dict:
    await sensitive(request, "pwchange", str(user["_id"]))
    if not sec.verify_password(user["password_hash"], body.current_password)[0]:
        raise HTTPException(403, "Current password is incorrect")
    sec.validate_password_strength(body.new_password)
    await db()["users"].update_one({"_id": user["_id"]}, {"$set": {"password_hash": sec.hash_password(body.new_password)}})
    await S.sessions.revoke_user(user["_id"], keep_id=request.state.session["_id"])
    await audit(request, "account.password_changed", user["_id"], category="security")
    return {"ok": True}


@api.get("/account/sessions")
async def list_sessions(request: Request, user: dict = Depends(current_user)) -> dict:
    cur = db()["sessions"].find({"user_id": user["_id"], "kind": "full"}).sort("last_seen", -1)
    return {"sessions": [{"id": s["_id"], "ip": s.get("ip"), "ua": s.get("ua"), "created_at": iso(s["created_at"]), "last_seen": iso(s["last_seen"]),
                          "current": s["_id"] == request.state.session["_id"]} async for s in cur]}


@api.delete("/account/sessions/{session_id}")
async def revoke_session(session_id: str, user: dict = Depends(current_user)) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", session_id):
        raise HTTPException(422, "Invalid session id")
    return {"revoked": await S.sessions.revoke_id(session_id, user["_id"])}


@api.post("/account/sessions/revoke-others")
async def revoke_others(request: Request, user: dict = Depends(current_user)) -> dict:
    return {"revoked": await S.sessions.revoke_user(user["_id"], keep_id=request.state.session["_id"])}


# ---- catalog ----------------------------------------------------------------
@api.get("/countries")
async def list_countries() -> dict:
    ctx = await catalog_ctx()
    cs = sorted([c for c in ctx["countries"].values() if c.get("enabled", True)], key=lambda c: (c.get("order", 0), c["name"]))
    return {"countries": [{"code": c["code"], "name": c["name"], "region": c.get("region", "")} for c in cs]}


@api.get("/categories")
async def list_categories() -> dict:
    ctx = await catalog_ctx()
    cats = sorted([c for c in ctx["categories"].values() if c.get("enabled", True)], key=lambda c: (c.get("order", 0), c["name"]))
    return {"categories": [{"id": str(c["_id"]), "name": c["name"], "slug": c["slug"], "parent_id": str(c["parent_id"]) if c.get("parent_id") else None,
                            "description": c.get("description", ""), "image_url": c.get("image_url"), "featured": c.get("featured", False)} for c in cats]}


@api.get("/schemas/public")
async def public_filters(category_id: Optional[str] = None) -> dict:
    """Filterable/sortable dynamic fields for the storefront filter panel."""
    sf = await schema_fields_for(oid(category_id) if category_id else None)
    return {"filters": [{"key": k, "label": f["label"], "type": f["type"], "options": f.get("options", [])} for k, f in sf.items() if f.get("filterable") and f.get("public")]}


@api.get("/products")
async def list_products(request: Request, q: str = "", country: str = "", category: str = "", subcategory: str = "",
                        min_price: Optional[str] = None, max_price: Optional[str] = None, availability: str = "",
                        sort: str = "relevance", page: int = Query(1, ge=1, le=200), limit: int = Query(12, ge=1, le=48)) -> dict:
    flt: Dict[str, Any] = {"status": "published"}
    q = sec.clean_text(q, settings.search_max_query_length, label="Search")
    if q:
        flt["$text"] = {"$search": q}
    if country:
        flt["country"] = sec.scalar(country).upper()[:2]
    if category:
        cat_oid = oid(category, "category")
        child_ids = [c["_id"] for c in (await catalog_ctx())["categories"].values() if c.get("parent_id") == cat_oid]
        flt["category_id"] = {"$in": [cat_oid] + child_ids}
    if subcategory:
        flt["subcategory"] = sec.clean_text(subcategory, 60)
    price: Dict[str, int] = {}
    if min_price:
        price["$gte"] = money_to_minor(min_price, "min_price")
    if max_price:
        price["$lte"] = money_to_minor(max_price, "max_price")
    if price:
        flt["final_price_minor"] = price
    if availability == "in_stock":
        flt["$expr"] = {"$gt": [{"$sum": "$variants.available"}, 0]}
    elif availability == "out_of_stock":
        flt["$expr"] = {"$lte": [{"$sum": "$variants.available"}, 0]}
    sf = await schema_fields_for(oid(category) if category else None)
    for key, val in request.query_params.items():  # dynamic filters arrive as f_<key>=value
        if key.startswith("f_"):
            fk = key[2:]
            if fk in sf and sf[fk].get("filterable") and sf[fk].get("public"):
                flt[f"fields.{fk}"] = sec.clean_text(sec.scalar(val), 100)
    proj, sort_spec = None, None
    if sort == "relevance" and q:
        proj, sort_spec = {"score": {"$meta": "textScore"}}, [("score", {"$meta": "textScore"})]
    else:
        sort_spec = sec.pick_sort(sort, PRODUCT_SORTS, "newest")
    cur = db()["products"].find(flt, proj).sort(sort_spec).skip((page - 1) * limit).limit(limit)
    items = [p async for p in cur]
    total = await db()["products"].count_documents(flt)
    ctx = await catalog_ctx()
    return {"items": [await public_product(p, ctx=ctx) for p in items], "page": page, "limit": limit, "total": total, "has_more": page * limit < total}


@api.get("/products/suggest")
async def suggest(q: str = Query("", max_length=80)) -> dict:
    q = sec.clean_text(q, 80).lower()
    if len(q) < 2:
        return {"suggestions": []}
    cur = db()["products"].find({"status": "published", "name_lc": {"$regex": "^" + re.escape(q)}}, {"name": 1, "slug": 1}).limit(8)
    return {"suggestions": [{"name": p["name"], "slug": p["slug"]} async for p in cur]}


@api.get("/products/by-ids")
async def products_by_ids(ids: str = Query("", max_length=600)) -> dict:
    oids = [oid(i) for i in ids.split(",") if i][:24]
    ctx = await catalog_ctx()
    items = [await public_product(p, ctx=ctx) async for p in db()["products"].find({"_id": {"$in": oids}, "status": "published"})]
    return {"items": items}


@api.get("/home")
async def home(country: str = "") -> dict:
    ctx = await catalog_ctx()

    async def section(flt: dict, sort: list, n: int = 8) -> list:
        return [await public_product(p, ctx=ctx) async for p in db()["products"].find({"status": "published", **flt}).sort(sort).limit(n)]
    out = {
        "new_arrivals": await section({}, PRODUCT_SORTS["newest"]),
        "best_sellers": await section({"sold_total": {"$gt": 0}}, PRODUCT_SORTS["popularity"]),
        "recommended": await section({"featured": True}, PRODUCT_SORTS["newest"]),
        "offers": await section({"discount_percent": {"$gt": 0}}, [("discount_percent", DESCENDING)]),
    }
    if country:
        out["country_collection"] = await section({"country": sec.scalar(country).upper()[:2]}, PRODUCT_SORTS["newest"])
    return out


@api.get("/products/{ref}")
async def product_detail(ref: str) -> dict:
    flt: Dict[str, Any] = {"status": "published"}
    flt.update({"_id": oid(ref)} if re.fullmatch(r"[a-f0-9]{24}", ref) else {"slug": sec.clean_text(ref, 80)})
    p = await db()["products"].find_one(flt)
    if not p:
        raise HTTPException(404, "Product not found")
    ctx = await catalog_ctx()
    related = [await public_product(r, ctx=ctx) async for r in db()["products"].find({"status": "published", "category_id": p["category_id"], "_id": {"$ne": p["_id"]}}).limit(6)]
    return {"product": await public_product(p, detail=True, ctx=ctx), "related": related}


@api.get("/products/{pid}/reviews")
async def product_reviews(pid: str, page: int = Query(1, ge=1, le=100)) -> dict:
    cur = db()["reviews"].find({"product_id": oid(pid), "status": "published"}).sort("created_at", -1).skip((page - 1) * 10).limit(10)
    return {"reviews": [{"rating": r["rating"], "title": r["title"], "body": r["body"], "author": r["author"], "created_at": iso(r["created_at"]), "verified_purchase": True} async for r in cur]}


@api.post("/products/{pid}/reviews", status_code=201)
async def add_review(pid: str, body: ReviewIn, request: Request, user: dict = Depends(current_user)) -> dict:
    await sensitive(request, "review", str(user["_id"]))
    p_id = oid(pid)
    if not await db()["orders"].find_one({"user_id": user["_id"], "status": "paid", "items.product_id": p_id}):
        raise HTTPException(403, "Only customers who purchased this product can review it")
    try:
        await db()["reviews"].insert_one({"product_id": p_id, "user_id": user["_id"], "rating": body.rating, "title": sec.clean_text(body.title, 100),
                                          "body": sec.clean_text(body.body, 2000, multiline=True), "author": (user.get("name") or "Customer").split(" ")[0][:30],
                                          "status": "published", "created_at": utcnow()})
    except DuplicateKeyError:
        raise HTTPException(409, "You have already reviewed this product")
    await db()["products"].update_one({"_id": p_id}, {"$inc": {"rating_sum": body.rating, "rating_count": 1}})
    return {"ok": True}


@api.post("/newsletter", status_code=202)
async def newsletter(body: NewsletterIn, request: Request) -> dict:
    await sensitive(request, "newsletter")
    if not body.consent:
        raise HTTPException(422, "Consent is required to subscribe")
    await db()["newsletter"].update_one({"email": sec.clean_email(body.email)}, {"$setOnInsert": {"consented_at": utcnow(), "ip_hash": sec.hash_token(sec.client_ip(request))[:16]}}, upsert=True)
    return {"ok": True}


# ---- wishlist / cart ----------------------------------------------------------
@api.put("/wishlist/{pid}")
async def wishlist_add(pid: str, user: dict = Depends(current_user)) -> dict:
    await db()["users"].update_one({"_id": user["_id"]}, {"$addToSet": {"wishlist": oid(pid)}})
    return {"ok": True}


@api.delete("/wishlist/{pid}")
async def wishlist_remove(pid: str, user: dict = Depends(current_user)) -> dict:
    await db()["users"].update_one({"_id": user["_id"]}, {"$pull": {"wishlist": oid(pid)}})
    return {"ok": True}


async def cart_view(user: dict) -> dict:
    cart = await db()["carts"].find_one({"user_id": user["_id"]}) or {"items": []}
    priced = await price_cart(user, cart["items"])
    return {"items": [{**{k: jsonable(v) for k, v in l.items()}, "product_id": str(l["product_id"]), "unit_price": minor_to_str(l["unit_price_minor"])} for l in priced["lines"]],
            **{k: priced[k] for k in ("subtotal_minor", "member_discount_minor", "tax_minor", "total_minor", "currency")}, "problems": priced.get("problems", [])}


@api.get("/cart")
async def get_cart(user: dict = Depends(current_user)) -> dict:
    return await cart_view(user)


@api.put("/cart/items")
async def set_cart_item(body: CartItemIn, user: dict = Depends(current_user)) -> dict:
    pid = oid(body.product_id)
    cart = await db()["carts"].find_one({"user_id": user["_id"]}) or {"items": []}
    items = [i for i in cart["items"] if not (i["product_id"] == str(pid) and i["sku"] == body.sku)]
    if body.qty > 0:
        p = await db()["products"].find_one({"_id": pid, "status": "published", "variants.sku": body.sku})
        if not p:
            raise HTTPException(404, "Product or variant not found")
        if len(items) >= 40:
            raise HTTPException(422, "Cart is full")
        items.append({"product_id": str(pid), "sku": body.sku, "qty": body.qty})
    await db()["carts"].update_one({"user_id": user["_id"]}, {"$set": {"items": items, "updated_at": utcnow()}}, upsert=True)
    return await cart_view(user)


@api.delete("/cart")
async def clear_cart(user: dict = Depends(current_user)) -> dict:
    await db()["carts"].update_one({"user_id": user["_id"]}, {"$set": {"items": []}}, upsert=True)
    return {"ok": True}


# ---- checkout / orders --------------------------------------------------------
@api.post("/checkout", status_code=201)
async def checkout(body: CheckoutIn, request: Request, idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=80),
                   user: dict = Depends(current_user)) -> dict:
    existing = await db()["orders"].find_one({"user_id": user["_id"], "idempotency_key": idempotency_key})
    if existing:
        pay = await db()["payment_records"].find_one({"order_id": existing["_id"]})
        return {"order": public_order(existing, pay)}
    cart = await db()["carts"].find_one({"user_id": user["_id"]}) or {"items": []}
    if not cart["items"]:
        raise HTTPException(422, "Your cart is empty")
    if body.payment_method == "plisio" and not settings.plisio_enabled:
        raise HTTPException(503, "Crypto payments are not configured on this server")
    priced = await price_cart(user, cart["items"], strict=True)
    if priced["total_minor"] <= 0:
        raise HTTPException(422, "Order total must be greater than zero")
    number = "ORD-" + secrets.token_hex(5).upper()
    now = utcnow()
    order = {"_id": ObjectId(), "number": number, "user_id": user["_id"], "items": priced["lines"], "subtotal_minor": priced["subtotal_minor"],
             "member_discount_minor": priced["member_discount_minor"], "tax_minor": priced["tax_minor"], "total_minor": priced["total_minor"],
             "currency": priced["currency"], "status": "created", "fulfillment_status": "unfulfilled", "payment_method": body.payment_method,
             "idempotency_key": idempotency_key, "tier_at_purchase": priced["tier"], "created_at": now, "updated_at": now,
             "reservation_expires_at": now + timedelta(minutes=settings.checkout_reservation_minutes),
             "history": [{"status": "created", "at": now, "note": "order created", "actor": None}]}
    for l in order["items"]:
        l.pop("available", None)
    try:
        await db()["orders"].insert_one(order)
    except DuplicateKeyError:
        raise HTTPException(409, "Duplicate checkout request")
    try:
        await reserve_lines(order["items"], order["_id"])
    except HTTPException:
        await transition("orders", order["_id"], "failed", note="stock unavailable")
        raise
    pay = None
    try:
        if body.payment_method == "wallet":
            await wallet_post(user["_id"], -order["total_minor"], "purchase", f"buy:{number}", ref=number)
            pay = {"provider": "wallet", "purpose": "order", "reference": number, "order_id": order["_id"], "user_id": user["_id"], "track_id": f"wallet-{number}",
                   "expected_minor": order["total_minor"], "currency": order["currency"], "status": "paid", "verified_minor": order["total_minor"], "created_at": now, "updated_at": now, "paid_at": now, "history": []}
            await db()["payment_records"].insert_one(pay)
            await settle_order(pay)
        else:
            pay = await create_invoice(user, number, order["total_minor"], "order", order["_id"], f"Order {number}")
            await transition("orders", order["_id"], "pending", note="awaiting payment")
    except HTTPException:
        fresh = await transition("orders", order["_id"], "failed", note="payment initiation failed")
        if fresh:
            await release_once(fresh)
        raise
    await db()["carts"].update_one({"user_id": user["_id"]}, {"$set": {"items": []}})
    await audit(request, "order.created", user["_id"], target=number, category="order")
    return {"order": public_order(await db()["orders"].find_one({"_id": order["_id"]}), pay)}


@api.get("/orders")
async def my_orders(page: int = Query(1, ge=1, le=100), user: dict = Depends(current_user)) -> dict:
    cur = db()["orders"].find({"user_id": user["_id"]}).sort("created_at", -1).skip((page - 1) * 10).limit(10)
    return {"orders": [public_order(o) async for o in cur]}


@api.get("/orders/{number}")
async def my_order(number: str, user: dict = Depends(current_user)) -> dict:
    o = await db()["orders"].find_one({"number": sec.clean_text(number, 20).upper(), "user_id": user["_id"]})
    if not o:
        raise HTTPException(404, "Order not found")
    return {"order": public_order(o, await db()["payment_records"].find_one({"order_id": o["_id"]}))}


@api.post("/orders/{number}/cancel")
async def cancel_order(number: str, request: Request, user: dict = Depends(current_user)) -> dict:
    o = await db()["orders"].find_one({"number": sec.clean_text(number, 20).upper(), "user_id": user["_id"]})
    if not o:
        raise HTTPException(404, "Order not found")
    if o["status"] not in ("created", "pending"):
        raise HTTPException(409, "Only unpaid orders can be cancelled")
    pay = await db()["payment_records"].find_one({"order_id": o["_id"]})
    if pay and pay.get("track_id") and settings.plisio_enabled:  # never cancel if the provider already saw money
        try:
            if await apply_provider_state(pay, await provider_state(pay["track_id"]), "cancel-check") == "paid":
                raise HTTPException(409, "Payment was already received for this order")
        except HTTPException as exc:
            if exc.status_code == 409:
                raise
            raise HTTPException(503, "Could not confirm payment status with the provider; try again shortly")
    fresh = await transition("orders", o["_id"], "cancelled", note="cancelled by customer", actor=user["_id"])
    if not fresh:
        raise HTTPException(409, "Order can no longer be cancelled")
    if pay:
        await transition("payment_records", pay["_id"], "cancelled", note="order cancelled")
    await release_once(fresh)
    return {"order": public_order(fresh)}


# ---- payments -----------------------------------------------------------------
@api.post("/payments/plisio/webhook")
async def plisio_webhook(request: Request) -> Response:
    """Plisio callback: verification is done via independent server-to-server call."""
    raw = await request.body()
    if len(raw) > 100_000:
        return PlainTextResponse("too large", status_code=413)
    try:
        # Plisio sends form data, but we verify by making an API call back to them
        payload = json.loads(raw) if raw.startswith(b"{") else dict(await request.form())
        if not payload.get("txn_id"):
            raise ValueError("unsupported payload")
        sec.reject_operators(payload)
    except (ValueError, HTTPException):
        await audit(request, "webhook.malformed", category="security", level="warn")
        return PlainTextResponse("Malformed payload", status_code=400)
    
    key = hashlib.sha256(raw).hexdigest()
    try:
        await db()["webhook_events"].insert_one({"event_key": key, "provider": "plisio", "track_id": str(payload["txn_id"]), "status": payload.get("status"),
                                                 "payload": {k: payload.get(k) for k in ("txn_id", "status", "amount", "currency", "order_number", "date")},
                                                 "processed": False, "attempts": 1, "received_at": utcnow()})
    except DuplicateKeyError:
        ev = await db()["webhook_events"].find_one_and_update({"event_key": key}, {"$inc": {"attempts": 1}})
        if ev and ev.get("processed"):
            return PlainTextResponse("ok")  # replayed delivery: already handled
    pay = await db()["payment_records"].find_one({"track_id": str(payload["txn_id"])})
    if not pay:
        await db()["webhook_events"].update_one({"event_key": key}, {"$set": {"processed": True, "result": "unknown_track_id"}})
        await audit(request, "webhook.unknown_track", target=payload["txn_id"], category="security", level="warn")
        return PlainTextResponse("ok")
    try:
        verified = await provider_state(pay["track_id"])  # independent server-to-server verification
    except HTTPException:
        return PlainTextResponse("provider unavailable", status_code=503)  # Plisio retries later
    result = await apply_provider_state(pay, verified, "webhook")
    await db()["webhook_events"].update_one({"event_key": key}, {"$set": {"processed": result != "inconsistent", "result": result, "processed_at": utcnow()}})
    if result == "inconsistent":
        return PlainTextResponse("inconsistent", status_code=400)
    return PlainTextResponse("ok")


# ---- wallet / membership / rewards / referrals ----------------------------------
@api.get("/wallet")
async def wallet(user: dict = Depends(current_user)) -> dict:
    acct = await db()["wallet_accounts"].find_one({"user_id": user["_id"]}) or {"balance_minor": 0}
    cur = db()["wallet_ledger"].find({"user_id": user["_id"]}).sort("created_at", -1).limit(50)
    return {"balance_minor": acct["balance_minor"], "currency": settings.default_currency,
            "ledger": [{"type": e["type"], "amount_minor": e["amount_minor"], "status": e["status"], "ref": e.get("ref"), "created_at": iso(e["created_at"])} async for e in cur]}


@api.post("/wallet/deposit", status_code=201)
async def wallet_deposit(body: DepositIn, request: Request, user: dict = Depends(current_user)) -> dict:
    minor = money_to_minor(body.amount, "Amount", allow_zero=False)
    if minor < settings.wallet_deposit_min_minor or minor > settings.wallet_deposit_max_minor:
        raise HTTPException(422, f"Deposits must be between {minor_to_str(settings.wallet_deposit_min_minor)} and {minor_to_str(settings.wallet_deposit_max_minor)}")
    pay = await create_invoice(user, "DEP-" + secrets.token_hex(5).upper(), minor, "wallet_deposit", None, "Wallet top-up")
    await audit(request, "wallet.deposit_started", user["_id"], target=pay["reference"], category="payment")
    return {"payment_url": pay["payment_url"], "reference": pay["reference"]}


@api.get("/membership")
async def membership(user: dict = Depends(current_user)) -> dict:
    tiers = await get_tiers()
    tier = tier_for(user, tiers)
    spend = user.get("lifetime_spend_minor", 0)
    nxt = next((t for t in tiers if t["threshold_minor"] > spend and t["threshold_minor"] > tier["threshold_minor"]), None)
    prog = 100 if not nxt else min(100, int((spend - tier["threshold_minor"]) * 100 / max(1, nxt["threshold_minor"] - tier["threshold_minor"])))
    pub = lambda t: {k: t[k] for k in ("key", "name", "threshold_minor", "discount_bps", "reward_multiplier_bps", "benefits")}  # noqa: E731
    return {"tier": pub(tier), "next_tier": pub(nxt) if nxt else None, "progress_percent": prog, "lifetime_spend_minor": spend,
            "points_balance": user.get("points_balance", 0), "currency": settings.default_currency}


@api.get("/rewards")
async def rewards(user: dict = Depends(current_user)) -> dict:
    cur = db()["reward_ledger"].find({"user_id": user["_id"]}).sort("created_at", -1).limit(50)
    return {"points_balance": user.get("points_balance", 0), "ledger": [{"type": r["type"], "points": r["points"], "order_number": r.get("order_number"), "created_at": iso(r["created_at"])} async for r in cur]}


@api.get("/referrals")
async def referrals(user: dict = Depends(current_user)) -> dict:
    total = await db()["referrals"].count_documents({"referrer_id": user["_id"]})
    rewarded = await db()["referrals"].count_documents({"referrer_id": user["_id"], "status": "rewarded"})
    return {"code": user.get("referral_code"), "link": f"{settings.public_app_url}/?ref={user.get('referral_code')}", "referred": total, "rewarded": rewarded, "points_per_reward": settings.referral_reward_points}


# ---- notifications / support ------------------------------------------------------
@api.get("/notifications")
async def notifications(user: dict = Depends(current_user)) -> dict:
    cur = db()["notifications"].find({"user_id": user["_id"]}).sort("created_at", -1).limit(30)
    items = [{"id": str(n["_id"]), "title": n["title"], "body": n["body"], "read": n["read"], "created_at": iso(n["created_at"])} async for n in cur]
    return {"items": items, "unread": sum(1 for n in items if not n["read"])}


@api.post("/notifications/read")
async def notifications_read(user: dict = Depends(current_user)) -> dict:
    await db()["notifications"].update_many({"user_id": user["_id"], "read": False}, {"$set": {"read": True}})
    return {"ok": True}


@api.post("/support/tickets", status_code=201)
async def create_ticket(body: TicketIn, request: Request, user: dict = Depends(current_user)) -> dict:
    await sensitive(request, "ticket", str(user["_id"]))
    t = {"user_id": user["_id"], "subject": sec.clean_text(body.subject, 120), "order_number": sec.clean_text(body.order_number or "", 20).upper() or None, "status": "open",
         "messages": [{"from": "customer", "body": sec.clean_text(body.message, 3000, multiline=True), "at": utcnow()}], "created_at": utcnow(), "updated_at": utcnow()}
    res = await db()["support_tickets"].insert_one(t)
    return {"id": str(res.inserted_id)}


@api.get("/support/tickets")
async def my_tickets(user: dict = Depends(current_user)) -> dict:
    cur = db()["support_tickets"].find({"user_id": user["_id"]}).sort("updated_at", -1).limit(50)
    return {"tickets": [{"id": str(t["_id"]), "subject": t["subject"], "status": t["status"], "order_number": t.get("order_number"), "updated_at": iso(t["updated_at"]),
                         "messages": [{"from": m["from"], "body": m["body"], "at": iso(m["at"])} for m in t["messages"]]} async for t in cur]}


@api.post("/support/tickets/{tid}/messages")
async def ticket_reply(tid: str, body: MessageIn, user: dict = Depends(current_user)) -> dict:
    res = await db()["support_tickets"].update_one({"_id": oid(tid), "user_id": user["_id"], "status": {"$ne": "closed"}},
                                                   {"$push": {"messages": {"from": "customer", "body": sec.clean_text(body.message, 3000, multiline=True), "at": utcnow()}}, "$set": {"status": "open", "updated_at": utcnow()}})
    if not res.modified_count:
        raise HTTPException(404, "Ticket not found or closed")
    return {"ok": True}


# =============================================================================
# 10. ADMIN ROUTES
# =============================================================================
admin = APIRouter(prefix="/api/admin", dependencies=[Depends(api_rate_limit)])


@admin.get("/stats")
async def admin_stats(user: dict = Depends(staff)) -> dict:
    c = lambda col, q: db()[col].count_documents(q)  # noqa: E731
    low_q = {"status": "published", "$expr": {"$anyElementTrue": {"$map": {"input": "$variants", "as": "v", "in": {"$and": [{"$gt": ["$$v.available", 0]}, {"$lte": ["$$v.available", "$low_stock_threshold"]}]}}}}}
    out_q = {"status": "published", "$expr": {"$lte": [{"$sum": "$variants.available"}, 0]}}
    rev = await (await db()["orders"].aggregate([{"$match": {"status": {"$in": ["paid", "refunded"]}}}, {"$group": {"_id": None, "rev": {"$sum": "$total_minor"}, "ref": {"$sum": "$refund_total_minor"}}}])).to_list(1)
    since = utcnow() - timedelta(days=30)
    sec_events = [{"action": e["action"], "level": e.get("level"), "ip": e.get("ip"), "at": iso(e["created_at"])} async for e in db()["audit_logs"].find({"category": "security"}).sort("created_at", -1).limit(8)]
    inv = [{"sku": m["sku"], "type": m["type"], "delta": m["delta_stock"] or m["delta_available"], "at": iso(m["created_at"])} async for m in db()["inventory_movements"].find({}).sort("created_at", -1).limit(8)]
    return {"currency": settings.default_currency, "customers_total": await c("users", {"role": "customer"}),
            "customers_active": await c("sessions", {"kind": "full", "last_seen": {"$gt": since}}), "products_published": await c("products", {"status": "published"}),
            "low_stock": await c("products", low_q), "out_of_stock": await c("products", out_q), "orders_total": await c("orders", {}),
            "orders_pending": await c("orders", {"status": {"$in": ["created", "pending"]}}), "orders_completed": await c("orders", {"status": "paid"}),
            "payments_pending": await c("payment_records", {"status": "pending"}), "revenue_minor": (rev[0]["rev"] - rev[0]["ref"]) if rev else 0,
            "refunds_minor": rev[0]["ref"] if rev else 0, "recent_security_events": sec_events, "recent_inventory": inv}


# ---- products --------------------------------------------------------------------
@admin.get("/products")
async def admin_products(q: str = "", status: str = "", page: int = Query(1, ge=1, le=500), user: dict = Depends(staff)) -> dict:
    flt: Dict[str, Any] = {}
    if status in ("draft", "published", "archived"):
        flt["status"] = status
    if q:
        flt["name_lc"] = {"$regex": re.escape(sec.clean_text(q, 80).lower())}
    cur = db()["products"].find(flt).sort("created_at", -1).skip((page - 1) * 25).limit(25)
    return {"items": [admin_product(p) async for p in cur], "total": await db()["products"].count_documents(flt)}


@admin.get("/products/{pid}")
async def admin_product_get(pid: str, user: dict = Depends(staff)) -> dict:
    p = await db()["products"].find_one({"_id": oid(pid)})
    if not p:
        raise HTTPException(404, "Product not found")
    return admin_product(p)


@admin.post("/products", status_code=201)
async def admin_product_create(body: ProductIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    doc = await build_product_doc(body)
    doc.update({"slug": slugify(doc["name"]), "created_at": utcnow(), "sold_total": 0, "rating_sum": 0, "rating_count": 0, "created_by": user["_id"]})
    res = await db()["products"].insert_one(doc)
    for v in doc["variants"]:
        if v["stock"]:
            await log_movement(res.inserted_id, v["sku"], "initial", v["stock"], v["stock"], 0, "initial stock", user["_id"])
    await audit(request, "product.create", user["_id"], res.inserted_id)
    return {"id": str(res.inserted_id), "slug": doc["slug"]}


@admin.put("/products/{pid}")
async def admin_product_update(pid: str, body: ProductIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    p = await db()["products"].find_one({"_id": oid(pid)})
    if not p:
        raise HTTPException(404, "Product not found")
    doc = await build_product_doc(body, existing=p)
    await db()["products"].update_one({"_id": p["_id"]}, {"$set": doc})
    await audit(request, "product.update", user["_id"], pid)
    return {"ok": True}


@admin.post("/products/{pid}/status")
async def admin_product_status(pid: str, body: StatusIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    res = await db()["products"].update_one({"_id": oid(pid)}, {"$set": {"status": body.status, "updated_at": utcnow()}})
    if not res.matched_count:
        raise HTTPException(404, "Product not found")
    await audit(request, f"product.{body.status}", user["_id"], pid)
    return {"ok": True}


@admin.delete("/products/{pid}")
async def admin_product_archive(pid: str, request: Request, user: dict = Depends(admin_user)) -> dict:
    res = await db()["products"].update_one({"_id": oid(pid)}, {"$set": {"status": "archived", "updated_at": utcnow()}})
    if not res.matched_count:
        raise HTTPException(404, "Product not found")
    await audit(request, "product.archive", user["_id"], pid)
    return {"ok": True}


@admin.post("/uploads")
async def admin_upload(request: Request, file: UploadFile = File(...), user: dict = Depends(admin_user)) -> dict:
    """Proxy upload to ImgBB; the API key never leaves the server."""
    if not settings.imgbb_enabled:
        raise HTTPException(503, "Image uploads are disabled. Set IMGBB_API_KEY, or paste an existing ImgBB image URL.")
    await sensitive(request, "upload", str(user["_id"]))
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, "Image is too large")
    if not sec.sniff_image_type(data):
        raise HTTPException(415, "Only JPEG, PNG, GIF or WebP images are accepted")
    assert S.http is not None
    try:
        r = await S.http.post(settings.imgbb_upload_url, data={"key": settings.imgbb_api_key, "image": base64.b64encode(data).decode()}, timeout=30)
        j = r.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Image host is unavailable")
    if r.status_code != 200 or not j.get("success"):
        log.error("ImgBB upload failed status=%s", r.status_code)
        raise HTTPException(502, "Image host rejected the upload")
    url = sec.validate_image_url(j["data"]["url"])
    await audit(request, "upload.image", user["_id"], url)
    return {"url": url}


# ---- categories / countries / schemas -----------------------------------------------
@admin.get("/categories")
async def admin_categories(user: dict = Depends(staff)) -> dict:
    cur = db()["categories"].find({}).sort([("order", 1), ("name", 1)])
    return {"categories": [jsonable({**c, "id": c["_id"]}) | {"_id": None} async for c in cur]}


def category_doc(body: CategoryIn) -> dict:
    return {"name": sec.clean_text(body.name, 60), "parent_id": oid(body.parent_id) if body.parent_id else None, "description": sec.clean_text(body.description, 300),
            "image_url": sec.validate_image_url(body.image_url) if body.image_url else None, "enabled": body.enabled, "featured": body.featured, "order": body.order}


@admin.post("/categories", status_code=201)
async def admin_category_create(body: CategoryIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    doc = category_doc(body)
    doc["slug"] = slugify(doc["name"])
    res = await db()["categories"].insert_one(doc)
    invalidate_cache()
    await audit(request, "category.create", user["_id"], res.inserted_id)
    return {"id": str(res.inserted_id)}


@admin.put("/categories/{cid}")
async def admin_category_update(cid: str, body: CategoryIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    res = await db()["categories"].update_one({"_id": oid(cid)}, {"$set": category_doc(body)})
    if not res.matched_count:
        raise HTTPException(404, "Category not found")
    invalidate_cache()
    await audit(request, "category.update", user["_id"], cid)
    return {"ok": True}


@admin.post("/categories/reorder")
async def admin_category_reorder(body: ReorderIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    for i, cid in enumerate(body.ids):
        await db()["categories"].update_one({"_id": oid(cid)}, {"$set": {"order": i}})
    invalidate_cache()
    await audit(request, "category.reorder", user["_id"])
    return {"ok": True}


@admin.get("/countries")
async def admin_countries(user: dict = Depends(staff)) -> dict:
    return {"countries": [{"code": c["code"], "name": c["name"], "region": c.get("region", ""), "enabled": c.get("enabled", True), "order": c.get("order", 0)} async for c in db()["countries"].find({}).sort("order", 1)]}


@admin.put("/countries")
async def admin_country_upsert(body: CountryIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    code = body.code.upper()
    await db()["countries"].update_one({"code": code}, {"$set": {"code": code, "name": sec.clean_text(body.name, 60), "region": sec.clean_text(body.region, 60), "enabled": body.enabled, "order": body.order}}, upsert=True)
    invalidate_cache()
    await audit(request, "country.upsert", user["_id"], code)
    return {"ok": True}


@admin.get("/schemas")
async def admin_schemas(user: dict = Depends(staff)) -> dict:
    return {"schemas": [jsonable({**s, "id": s["_id"]}) | {"_id": None} async for s in db()["product_schemas"].find({})], "field_types": FIELD_TYPES}


@admin.put("/schemas")
async def admin_schema_save(body: SchemaIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    cat = oid(body.category_id) if body.category_id else None
    keys = [f.key for f in body.fields]
    if len(set(keys)) != len(keys):
        raise HTTPException(422, "Duplicate field keys")
    fields = []
    for f in body.fields:
        d = f.model_dump()
        d["label"], d["placeholder"], d["help_text"] = sec.clean_text(d["label"], 60), sec.clean_text(d["placeholder"], 100), sec.clean_text(d["help_text"], 200)
        d["options"] = [sec.clean_text(o, 60) for o in d["options"]]
        if d["type"] in ("dropdown", "multiselect") and not d["options"]:
            raise HTTPException(422, f"Field {d['key']} needs at least one option")
        fields.append(d)
    await db()["product_schemas"].update_one({"category_id": cat}, {"$set": {"category_id": cat, "name": sec.clean_text(body.name, 60), "fields": fields, "active": body.active, "updated_at": utcnow()}}, upsert=True)
    invalidate_cache()
    await audit(request, "schema.save", user["_id"], str(cat))
    return {"ok": True}


@admin.get("/schemas/effective")
async def admin_schema_effective(category_id: Optional[str] = None, user: dict = Depends(staff)) -> dict:
    """Merged form definition used by the dynamic product form."""
    sf = await schema_fields_for(oid(category_id) if category_id else None)
    return {"fields": sorted(sf.values(), key=lambda f: f.get("position", 0))}


# ---- inventory ---------------------------------------------------------------------
@admin.get("/inventory")
async def admin_inventory(q: str = "", page: int = Query(1, ge=1, le=500), user: dict = Depends(staff)) -> dict:
    flt: Dict[str, Any] = {"status": {"$ne": "archived"}}
    if q:
        flt["name_lc"] = {"$regex": re.escape(sec.clean_text(q, 80).lower())}
    rows = []
    async for p in db()["products"].find(flt).sort("name_lc", 1).skip((page - 1) * 25).limit(25):
        for v in p["variants"]:
            rows.append({"product_id": str(p["_id"]), "name": p["name"], "sku": v["sku"], "label": v["label"], "stock": v["stock"], "reserved": v.get("reserved", 0),
                         "available": v.get("available", 0), "sold": v.get("sold", 0), "low_stock_threshold": p.get("low_stock_threshold", 0), "status": p["status"]})
    return {"rows": rows}


@admin.post("/inventory/adjust")
async def admin_inventory_adjust(body: AdjustIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    pid = oid(body.product_id)
    if (body.delta is None) == (body.set_to is None):
        raise HTTPException(422, "Provide exactly one of delta or set_to")
    delta = body.delta
    if delta is None:
        p = await db()["products"].find_one({"_id": pid, "variants.sku": body.sku}, {"variants.$": 1})
        if not p:
            raise HTTPException(404, "SKU not found")
        delta = body.set_to - p["variants"][0]["stock"]
    await adjust_stock(pid, body.sku, delta, sec.clean_text(body.reason, 200), user["_id"])
    await audit(request, "inventory.adjust", user["_id"], pid, {"sku": body.sku, "delta": delta}, category="inventory")
    return {"ok": True, "delta": delta}


@admin.post("/inventory/threshold/{pid}")
async def admin_threshold(pid: str, body: ThresholdIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    await db()["products"].update_one({"_id": oid(pid)}, {"$set": {"low_stock_threshold": body.low_stock_threshold}})
    await audit(request, "inventory.threshold", user["_id"], pid, category="inventory")
    return {"ok": True}


@admin.get("/inventory/movements")
async def admin_movements(product_id: str = "", page: int = Query(1, ge=1, le=500), user: dict = Depends(staff)) -> dict:
    flt = {"product_id": oid(product_id)} if product_id else {}
    cur = db()["inventory_movements"].find(flt).sort("created_at", -1).skip((page - 1) * 50).limit(50)
    return {"movements": [{"product_id": str(m["product_id"]), "sku": m["sku"], "type": m["type"], "delta_stock": m["delta_stock"], "delta_available": m["delta_available"],
                           "delta_reserved": m["delta_reserved"], "reason": m.get("reason", ""), "at": iso(m["created_at"])} async for m in cur]}


@admin.get("/inventory/export")
async def admin_inventory_export(request: Request, user: dict = Depends(admin_user)) -> Response:
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["product_id", "name", "sku", "stock", "reserved", "available", "sold"])
    async for p in db()["products"].find({"status": {"$ne": "archived"}}):
        for v in p["variants"]:
            name = p["name"]
            w.writerow([str(p["_id"]), ("'" + name) if name[:1] in "=+-@" else name, v["sku"], v["stock"], v.get("reserved", 0), v.get("available", 0), v.get("sold", 0)])
    await audit(request, "inventory.export", user["_id"], category="inventory")
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="inventory.csv"'})


@admin.post("/inventory/import")
async def admin_inventory_import(body: ImportIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    applied, errors = 0, []
    for i, row in enumerate(body.rows):
        try:
            pid = oid(row.product_id)
            p = await db()["products"].find_one({"_id": pid, "variants.sku": row.sku}, {"variants.$": 1})
            if not p:
                raise HTTPException(404, "SKU not found")
            delta = row.stock - p["variants"][0]["stock"]
            if delta:
                await adjust_stock(pid, row.sku, delta, f"import: {sec.clean_text(body.reason, 150)}", user["_id"], kind="import")
            applied += 1
        except HTTPException as exc:
            errors.append({"row": i + 1, "error": exc.detail})
    await audit(request, "inventory.import", user["_id"], meta={"applied": applied, "errors": len(errors)}, category="inventory")
    return {"applied": applied, "errors": errors}


# ---- orders / payments --------------------------------------------------------------
@admin.get("/orders")
async def admin_orders(q: str = "", status: str = "", page: int = Query(1, ge=1, le=500), user: dict = Depends(staff)) -> dict:
    flt: Dict[str, Any] = {}
    if status in ORDER_TRANSITIONS:
        flt["status"] = status
    if q:
        flt["number"] = {"$regex": "^" + re.escape(sec.clean_text(q, 20).upper())}
    cur = db()["orders"].find(flt).sort("created_at", -1).skip((page - 1) * 25).limit(25)
    return {"orders": [{"number": o["number"], "status": o["status"], "fulfillment_status": o.get("fulfillment_status"), "total_minor": o["total_minor"], "currency": o["currency"],
                        "payment_method": o["payment_method"], "created_at": iso(o["created_at"]), "refund_total_minor": o.get("refund_total_minor", 0)} async for o in cur]}


@admin.get("/orders/{number}")
async def admin_order(number: str, user: dict = Depends(staff)) -> dict:
    o = await db()["orders"].find_one({"number": sec.clean_text(number, 20).upper()})
    if not o:
        raise HTTPException(404, "Order not found")
    cust = await db()["users"].find_one({"_id": o["user_id"]}, {"email": 1, "name": 1})
    pay = await db()["payment_records"].find_one({"order_id": o["_id"]}, {"payment_url": 0})
    tickets = [str(t["_id"]) async for t in db()["support_tickets"].find({"order_number": o["number"]}, {"_id": 1})]
    return {"order": public_order(o), "customer": {"email": cust["email"], "name": cust.get("name")} if cust else None,
            "payment": jsonable({k: v for k, v in (pay or {}).items() if k not in ("_id", "user_id", "order_id")}), "refunds": jsonable(o.get("refunds", [])), "support_tickets": tickets}


@admin.post("/orders/{number}/fulfillment")
async def admin_fulfillment(number: str, body: FulfillIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    res = await db()["orders"].update_one({"number": sec.clean_text(number, 20).upper(), "status": "paid"}, {"$set": {"fulfillment_status": body.fulfillment_status, "updated_at": utcnow()}})
    if not res.matched_count:
        raise HTTPException(409, "Only paid orders can be fulfilled")
    await audit(request, "order.fulfillment", user["_id"], number, {"to": body.fulfillment_status}, category="order")
    return {"ok": True}


@admin.post("/orders/{number}/refund")
async def admin_refund(number: str, body: RefundIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    """Full refund to store credit (wallet ledger). Crypto payouts back to the payer are an off-platform procedure."""
    o = await db()["orders"].find_one({"number": sec.clean_text(number, 20).upper()})
    if not o:
        raise HTTPException(404, "Order not found")
    fresh = await transition("orders", o["_id"], "refunded", note=body.reason, actor=user["_id"],
                             extra={"refund_total_minor": o["total_minor"]})
    if not fresh:
        raise HTTPException(409, "Only paid orders can be refunded")
    await db()["orders"].update_one({"_id": o["_id"]}, {"$push": {"refunds": {"amount_minor": o["total_minor"], "reason": sec.clean_text(body.reason, 300), "actor": user["_id"], "at": utcnow(), "method": "wallet_credit"}}})
    await wallet_post(o["user_id"], o["total_minor"], "refund", f"refund:{o['number']}", ref=o["number"], actor=user["_id"], note=body.reason[:100])
    await db()["payment_records"].update_one({"order_id": o["_id"], "status": "paid"}, {"$set": {"status": "refunded", "updated_at": utcnow()}})
    pts = o.get("points_earned", 0)
    if pts:
        try:
            await db()["reward_ledger"].insert_one({"user_id": o["user_id"], "ref": str(o["_id"]), "type": "reversal", "points": -pts, "order_number": o["number"], "created_at": utcnow()})
            await db()["users"].update_one({"_id": o["user_id"]}, {"$inc": {"points_balance": -pts}})
        except DuplicateKeyError:
            pass
    await db()["users"].update_one({"_id": o["user_id"]}, {"$inc": {"lifetime_spend_minor": -o["total_minor"]}})
    if body.restock:
        for it in o["items"]:
            await adjust_stock(it["product_id"], it["sku"], it["qty"], f"refund restock {o['number']}", user["_id"], kind="refund_restock")
    await notify(o["user_id"], f"Order {o['number']} refunded", "The amount was credited to your wallet.")
    await audit(request, "order.refund", user["_id"], number, {"reason": body.reason}, category="payment")
    return {"ok": True}


@admin.get("/payments")
async def admin_payments(status: str = "", page: int = Query(1, ge=1, le=500), user: dict = Depends(staff)) -> dict:
    flt = {"status": status} if status in ORDER_TRANSITIONS else {}
    cur = db()["payment_records"].find(flt, {"payment_url": 0, "history": 0}).sort("created_at", -1).skip((page - 1) * 25).limit(25)
    out = []
    async for p in cur:
        ver = p.get("verification") or {}
        out.append({"provider": p["provider"], "purpose": p.get("purpose"), "reference": p["reference"], "invoice_ref": p.get("track_id"), "expected_minor": p["expected_minor"],
                    "verified_minor": p.get("verified_minor"), "currency": p["currency"], "status": p["status"], "created_at": iso(p["created_at"]), "paid_at": iso(p.get("paid_at")),
                    "verification": "ok" if ver.get("ok") else ("failed: " + str(ver.get("reason"))) if ver else "not yet checked",
                    "reconciliation": "matched" if p["status"] == "paid" and ver.get("ok") else ("mismatch" if ver and not ver.get("ok") else "pending"), "sandbox": p.get("sandbox")})
    return {"payments": out}


@admin.get("/webhooks")
async def admin_webhooks(user: dict = Depends(admin_user)) -> dict:
    cur = db()["webhook_events"].find({}, {"event_key": 0}).sort("received_at", -1).limit(50)
    return {"events": [jsonable({k: v for k, v in e.items() if k != "_id"}) async for e in cur]}


@admin.post("/payments/reconcile")
async def admin_reconcile(request: Request, user: dict = Depends(admin_user)) -> dict:
    """Re-verify every pending payment with the provider (never trusts admin input as proof of payment)."""
    checked, paid = 0, 0
    async for pay in db()["payment_records"].find({"status": "pending", "provider": "plisio"}).limit(100):
        try:
            res = await apply_provider_state(pay, await provider_state(pay["track_id"]), "reconcile")
        except HTTPException:
            continue
        checked += 1
        paid += res == "paid"
    await sweep_once()
    await audit(request, "payments.reconcile", user["_id"], meta={"checked": checked, "paid": paid}, category="payment")
    return {"checked": checked, "newly_paid": paid}


# ---- users ---------------------------------------------------------------------------
@admin.get("/users")
async def admin_users(q: str = "", page: int = Query(1, ge=1, le=500), user: dict = Depends(staff)) -> dict:
    flt: Dict[str, Any] = {}
    if q:
        flt["email"] = {"$regex": "^" + re.escape(sec.clean_text(q, 80).lower())}
    tiers = await get_tiers()
    cur = db()["users"].find(flt, {"password_hash": 0, "mfa": 0, "reset": 0}).sort("created_at", -1).skip((page - 1) * 25).limit(25)
    return {"users": [{"id": str(u["_id"]), "email": u["email"], "name": u.get("name"), "role": u.get("role"), "status": u.get("status"), "tier": tier_for(u, tiers)["name"],
                       "points_balance": u.get("points_balance", 0), "lifetime_spend_minor": u.get("lifetime_spend_minor", 0), "created_at": iso(u.get("created_at")),
                       "referrals": await db()["referrals"].count_documents({"referrer_id": u["_id"]})} async for u in cur]}


@admin.post("/users/{uid}/status")
async def admin_user_status(uid: str, body: UserStatusIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    target = await db()["users"].find_one({"_id": oid(uid)})
    if not target or target["_id"] == user["_id"] or ROLE_LEVEL.get(target.get("role", "customer"), 0) >= ROLE_LEVEL[user["role"]]:
        raise HTTPException(403, "You cannot change this account")
    await db()["users"].update_one({"_id": target["_id"]}, {"$set": {"status": body.status}})
    if body.status == "suspended":
        await S.sessions.revoke_user(target["_id"])
    await audit(request, f"user.{body.status}", user["_id"], uid, {"reason": body.reason}, category="security")
    return {"ok": True}


@admin.post("/users/{uid}/revoke-sessions")
async def admin_user_revoke(uid: str, request: Request, user: dict = Depends(admin_user)) -> dict:
    n = await S.sessions.revoke_user(oid(uid))
    await audit(request, "user.revoke_sessions", user["_id"], uid, category="security")
    return {"revoked": n}


@admin.post("/users/{uid}/membership")
async def admin_user_membership(uid: str, body: TierOverrideIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    if body.tier_key and not any(t["key"] == body.tier_key for t in await get_tiers()):
        raise HTTPException(422, "Unknown tier")
    op = {"$set": {"membership_override": body.tier_key}} if body.tier_key else {"$unset": {"membership_override": ""}}
    await db()["users"].update_one({"_id": oid(uid)}, op)
    await audit(request, "user.membership", user["_id"], uid, {"tier": body.tier_key})
    return {"ok": True}


@admin.post("/users/{uid}/reset-link")
async def admin_reset_link(uid: str, request: Request, user: dict = Depends(admin_user)) -> dict:
    token = sec.new_token(24)
    res = await db()["users"].update_one({"_id": oid(uid), "role": "customer"}, {"$set": {"reset": {"token_hash": sec.hash_token(token), "expires_at": utcnow() + timedelta(minutes=30)}}})
    if not res.matched_count:
        raise HTTPException(404, "Customer not found")
    await audit(request, "user.reset_link", user["_id"], uid, category="security")
    return {"reset_url": f"{settings.public_app_url}/?reset={token}", "expires_in_minutes": 30}


@admin.get("/users/{uid}/rewards")
async def admin_user_rewards(uid: str, user: dict = Depends(staff)) -> dict:
    cur = db()["reward_ledger"].find({"user_id": oid(uid)}).sort("created_at", -1).limit(50)
    return {"ledger": [{"type": r["type"], "points": r["points"], "order_number": r.get("order_number"), "at": iso(r["created_at"])} async for r in cur]}


@admin.post("/users/{uid}/wallet-adjust")
async def admin_wallet_adjust(uid: str, body: WalletAdjustIn, request: Request, user: dict = Depends(superadmin_user)) -> dict:
    if body.amount_minor == 0:
        raise HTTPException(422, "Amount must be non-zero")
    res = await wallet_post(oid(uid), body.amount_minor, "adjustment", f"adj:{secrets.token_hex(8)}", actor=user["_id"], note=sec.clean_text(body.reason, 200))
    await audit(request, "wallet.adjust", user["_id"], uid, {"amount_minor": body.amount_minor, "reason": body.reason}, category="payment", level="warn")
    return {"balance_minor": res.get("balance_minor")}


# ---- invitations / tiers / settings / tickets / audit -----------------------------------
@admin.get("/invitations")
async def admin_invitations(user: dict = Depends(staff)) -> dict:
    cur = db()["invitations"].find({}, {"code_hash": 0}).sort("created_at", -1).limit(100)
    return {"invitations": [{"id": str(i["_id"]), "email": i["email"], "name": i.get("name"), "message": i.get("message"), "status": i["status"], "created_at": iso(i.get("created_at"))} async for i in cur]}


async def issue_invite(email: str) -> str:
    code = sec.new_token(12)
    await db()["invitations"].update_one({"email": email}, {"$set": {"code_hash": sec.hash_token(code), "status": "approved", "expires_at": utcnow() + timedelta(days=14), "approved_at": utcnow()}, "$setOnInsert": {"email": email, "created_at": utcnow()}}, upsert=True)
    return code


@admin.post("/invitations/{iid}/approve")
async def admin_invite_approve(iid: str, request: Request, user: dict = Depends(admin_user)) -> dict:
    inv = await db()["invitations"].find_one({"_id": oid(iid)})
    if not inv:
        raise HTTPException(404, "Invitation not found")
    code = await issue_invite(inv["email"])
    await audit(request, "invitation.approve", user["_id"], iid)
    return {"email": inv["email"], "invite_code": code, "note": "Shown once. Send it to the applicant securely."}


@admin.post("/invitations/{iid}/reject")
async def admin_invite_reject(iid: str, request: Request, user: dict = Depends(admin_user)) -> dict:
    await db()["invitations"].update_one({"_id": oid(iid)}, {"$set": {"status": "rejected"}, "$unset": {"code_hash": ""}})
    await audit(request, "invitation.reject", user["_id"], iid)
    return {"ok": True}


@admin.post("/invitations")
async def admin_invite_create(body: InviteIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    email = sec.clean_email(body.email)
    code = await issue_invite(email)
    await audit(request, "invitation.create", user["_id"], email)
    return {"email": email, "invite_code": code}


@admin.get("/tiers")
async def admin_tiers(user: dict = Depends(staff)) -> dict:
    return {"tiers": [jsonable({k: v for k, v in t.items() if k != "_id"}) async for t in db()["memberships"].find({"kind": "tier"}).sort("threshold_minor", 1)]}


@admin.put("/tiers")
async def admin_tier_save(body: TierIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    d = body.model_dump()
    d["benefits"] = [sec.clean_text(b, 80) for b in d["benefits"]]
    await db()["memberships"].update_one({"key": body.key}, {"$set": {**d, "kind": "tier"}}, upsert=True)
    invalidate_cache()
    await audit(request, "tier.save", user["_id"], body.key)
    return {"ok": True}


@admin.get("/settings")
async def admin_settings_get(user: dict = Depends(staff)) -> dict:
    return {"settings": {k: v for k, v in (await get_site()).items() if k != "_id"}}


@admin.put("/settings")
async def admin_settings_save(body: SiteIn, request: Request, user: dict = Depends(admin_user)) -> dict:
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    for k in ("support_url", "terms_url"):
        if upd.get(k):
            upd[k] = sec.validate_https_url(upd[k], k)
    if upd.get("support_email"):
        upd["support_email"] = sec.clean_email(upd["support_email"])
    for k in ("brand_name", "tagline", "hero_title", "hero_subtitle"):
        if k in upd:
            upd[k] = sec.clean_text(upd[k], 200)
    await db()["site_settings"].update_one({"_id": "site"}, {"$set": upd}, upsert=True)
    invalidate_cache()
    await audit(request, "settings.update", user["_id"], meta={"keys": list(upd)})
    return {"ok": True}


@admin.get("/tickets")
async def admin_tickets(status: str = "", user: dict = Depends(staff)) -> dict:
    flt = {"status": status} if status in ("open", "pending", "closed") else {}
    out = []
    async for t in db()["support_tickets"].find(flt).sort("updated_at", -1).limit(50):
        u = await db()["users"].find_one({"_id": t["user_id"]}, {"email": 1})
        out.append({"id": str(t["_id"]), "subject": t["subject"], "status": t["status"], "email": (u or {}).get("email"), "order_number": t.get("order_number"),
                    "updated_at": iso(t["updated_at"]), "messages": [{"from": m["from"], "body": m["body"], "at": iso(m["at"])} for m in t["messages"]]})
    return {"tickets": out}


@admin.post("/tickets/{tid}/reply")
async def admin_ticket_reply(tid: str, body: MessageIn, request: Request, user: dict = Depends(staff)) -> dict:
    t = await db()["support_tickets"].find_one_and_update({"_id": oid(tid)}, {"$push": {"messages": {"from": "support", "body": sec.clean_text(body.message, 3000, multiline=True), "at": utcnow()}}, "$set": {"status": "pending", "updated_at": utcnow()}})
    if not t:
        raise HTTPException(404, "Ticket not found")
    await notify(t["user_id"], "Support replied", f"New reply on: {t['subject']}")
    await audit(request, "ticket.reply", user["_id"], tid)
    return {"ok": True}


@admin.post("/tickets/{tid}/status")
async def admin_ticket_status(tid: str, body: TicketStatusIn, request: Request, user: dict = Depends(staff)) -> dict:
    await db()["support_tickets"].update_one({"_id": oid(tid)}, {"$set": {"status": body.status, "updated_at": utcnow()}})
    await audit(request, "ticket.status", user["_id"], tid, {"status": body.status})
    return {"ok": True}


@admin.get("/audit")
async def admin_audit(category: str = "", page: int = Query(1, ge=1, le=500), user: dict = Depends(admin_user)) -> dict:
    flt = {"category": sec.clean_text(category, 20)} if category else {}
    cur = db()["audit_logs"].find(flt).sort("created_at", -1).skip((page - 1) * 50).limit(50)
    return {"logs": [{"action": a["action"], "category": a.get("category"), "level": a.get("level"), "actor": str(a["actor_id"]) if a.get("actor_id") else None, "target": a.get("target"),
                      "ip": a.get("ip"), "meta": jsonable(a.get("meta", {})), "at": iso(a["created_at"])} async for a in cur]}


@admin.get("/reviews")
async def admin_reviews(user: dict = Depends(staff)) -> dict:
    cur = db()["reviews"].find({}).sort("created_at", -1).limit(50)
    return {"reviews": [{"id": str(r["_id"]), "product_id": str(r["product_id"]), "rating": r["rating"], "title": r["title"], "body": r["body"], "status": r["status"]} async for r in cur]}


@admin.post("/reviews/{rid}/hide")
async def admin_review_hide(rid: str, request: Request, user: dict = Depends(admin_user)) -> dict:
    r = await db()["reviews"].find_one_and_update({"_id": oid(rid), "status": "published"}, {"$set": {"status": "hidden"}})
    if r:
        await db()["products"].update_one({"_id": r["product_id"]}, {"$inc": {"rating_sum": -r["rating"], "rating_count": -1}})
    await audit(request, "review.hide", user["_id"], rid)
    return {"ok": True}


# =============================================================================
# 11. APPLICATION FACTORY, STATIC PAGES, ERROR HANDLING, CLI, TESTS
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    S.http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    if settings.mongodb_uri:
        S.client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=5000, appname="marketplace")
        S.db = S.client[settings.mongodb_db]
        S.sessions, S.limiter = sec.SessionService(S.db), sec.RateLimiter(S.db)
        try:
            await ensure_indexes(S.db)
            await seed_defaults(S.db)
            S.sweeper = asyncio.create_task(sweeper_loop())
        except PyMongoError as exc:
            log.error("MongoDB not reachable at startup (%s). API will answer 503 until it is.", type(exc).__name__)
    else:
        log.warning("MONGODB_URI is not set - running without a database (health reports 'degraded').")
    yield
    if S.sweeper:
        S.sweeper.cancel()
    if S.http:
        await S.http.aclose()
    if S.client:
        await S.client.close()


app = FastAPI(title=f"{settings.brand_name} Marketplace API", version="1.0.0", lifespan=lifespan,
              docs_url=None if settings.is_production else "/docs", redoc_url=None, openapi_url=None if settings.is_production else "/openapi.json")
app.add_middleware(sec.SecurityHeadersMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=settings.effective_origins(), allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "Idempotency-Key"], max_age=600)


@app.middleware("http")
async def body_size_limit(request: Request, call_next):
    limit = 7_000_000 if request.url.path.endswith("/uploads") else 1_000_000
    if int(request.headers.get("content-length") or 0) > limit:
        return JSONResponse({"error": "Request body too large"}, status_code=413)
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def http_exc(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 429 and S.sessions:
        await audit(request, "ratelimit.hit", category="security", level="warn", meta={"path": request.url.path})
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_exc(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query"))
    return JSONResponse({"error": f"Invalid input{(' for ' + loc) if loc else ''}: {first.get('msg', 'validation failed')}"}, status_code=422)


@app.exception_handler(PyMongoError)
async def mongo_exc(request: Request, exc: PyMongoError):
    log.error("database error: %s", type(exc).__name__)
    return JSONResponse({"error": "The database is temporarily unavailable. Please try again shortly."}, status_code=503)


@app.exception_handler(Exception)
async def generic_exc(request: Request, exc: Exception):
    rid = secrets.token_hex(4)
    log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
    return JSONResponse({"error": f"Unexpected server error (ref {rid})"}, status_code=500)


app.include_router(api)
app.include_router(admin)


def _page(name: str, request: Request) -> HTMLResponse:
    path = BASE_DIR / name
    if not path.exists():
        raise HTTPException(404, f"{name} not found")
    html = path.read_text(encoding="utf-8").replace("{{NONCE}}", getattr(request.state, "csp_nonce", ""))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
async def index_page(request: Request):
    return _page("index.html", request)


@app.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    return _page("admin.html", request)


@app.get("/style.css", include_in_schema=False)
async def css() -> Response:
    return Response((BASE_DIR / "style.css").read_bytes(), media_type="text/css", headers={"Cache-Control": "public, max-age=300"})


@app.get("/script.js", include_in_schema=False)
async def js() -> Response:
    return Response((BASE_DIR / "script.js").read_bytes(), media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> Response:
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#2563eb"/></linearGradient></defs><rect width="64" height="64" rx="16" fill="#0a1226"/><path d="M32 12 50 52h-9l-3-8H26l-3 8h-9z M29 36h6l-3-9z" fill="url(#g)"/></svg>'
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ---- CLI --------------------------------------------------------------------------------
async def _cli(argv: List[str]) -> int:
    if not settings.mongodb_uri:
        print("MONGODB_URI is not set.")
        return 1
    client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=8000)
    d = client[settings.mongodb_db]
    try:
        cmd = argv[0] if argv else ""
        if cmd == "init-indexes":
            await ensure_indexes(d)
            await seed_defaults(d)
            print("Indexes and default configuration are in place.")
        elif cmd == "create-admin":
            if len(argv) < 2:
                print("Usage: python main.py create-admin EMAIL [superadmin|admin|support]")
                return 1
            email = sec.clean_email(argv[1])
            role = argv[2] if len(argv) > 2 else "superadmin"
            if role not in STAFF_ROLES:
                print("Role must be one of:", ", ".join(sorted(STAFF_ROLES)))
                return 1
            pw = getpass.getpass("Password (min 10 chars, mixed): ")
            if pw != getpass.getpass("Repeat password: "):
                print("Passwords do not match.")
                return 1
            sec.validate_password_strength(pw)
            await ensure_indexes(d)
            await d["users"].update_one({"email": email}, {"$set": {"password_hash": sec.hash_password(pw), "role": role, "status": "active"}, "$unset": {"mfa": ""},
                                                           "$setOnInsert": {"email": email, "name": "Administrator", "created_at": utcnow(), "lifetime_spend_minor": 0, "points_balance": 0, "wishlist": [], "referral_code": secrets.token_hex(4).upper()}}, upsert=True)
            print(f"{role} {email} saved. TOTP enrolment happens at first sign-in on /admin.")
        elif cmd == "reconcile":
            S.db, S.client = d, client
            S.http = httpx.AsyncClient(timeout=15)
            await sweep_once()
            print("Reconciliation sweep complete.")
        else:
            print("Commands: init-indexes | create-admin EMAIL [role] | reconcile")
            return 1
    finally:
        await client.close()
    return 0


# ---- tests (pytest main.py security.py -q) ------------------------------------------------
def test_money_parsing() -> None:
    assert money_to_minor("49.99") == 4999 and money_to_minor(10) == 1000
    for bad in ("-1", "1.999", "abc", "1e9", "NaN", "10000000"):
        try:
            money_to_minor(bad)
        except HTTPException:
            continue
        raise AssertionError(f"accepted {bad}")


def test_discount_rounding() -> None:
    assert apply_discount(1000, 15) == 850 and apply_discount(999, 10) == 899


def test_state_machine() -> None:
    assert can_transition("pending", "paid") and can_transition("paid", "refunded")
    assert not can_transition("failed", "paid") and not can_transition("refunded", "paid") and not can_transition("expired", "paid")


def test_amount_verification() -> None:
    pay = {"expected_minor": 5000, "currency": "USD", "reference": "ORD-1"}
    assert verify_amounts(pay, {"amount": 50, "currency": "usd", "order_number": "ORD-1"}) is None
    assert verify_amounts(pay, {"amount": 49.99, "currency": "USD", "order_number": "ORD-1"})
    assert verify_amounts(pay, {"amount": 50, "currency": "EUR", "order_number": "ORD-1"})
    assert verify_amounts(pay, {"amount": 50, "currency": "USD", "order_number": "ORD-2"})


def test_provider_status_mapping() -> None:
    assert map_provider_status("completed") == "paid" and map_provider_status("pending") == "pending" and map_provider_status("expired") == "expired"


def test_dynamic_field_validation() -> None:
    fd = {"label": "Size", "type": "dropdown", "options": ["S", "M"], "required": True}
    assert validate_field_value(fd, "S", set()) == "S"
    for bad in ("XL", {"$ne": 1}, None):
        try:
            validate_field_value(fd, bad, set())
        except HTTPException:
            continue
        raise AssertionError("accepted invalid dropdown value")
    assert validate_field_value({"label": "Note", "type": "text"}, "<b>hi</b><script>x</script>", set()) == "hix"
    assert str(validate_field_value({"label": "P", "type": "decimal"}, "12.50", set()).to_decimal()) == "12.50"


def test_tier_selection() -> None:
    tiers = [{"key": "basic", "name": "Basic", "threshold_minor": 0}, {"key": "gold", "name": "Gold", "threshold_minor": 1000}]
    assert tier_for({"lifetime_spend_minor": 999}, tiers)["key"] == "basic" and tier_for({"lifetime_spend_minor": 1000}, tiers)["key"] == "gold"


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(asyncio.run(_cli(sys.argv[1:])))
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=not settings.is_production)
