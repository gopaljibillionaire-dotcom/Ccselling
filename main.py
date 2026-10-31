"""main.py - FastAPI backend for CCS Telegram Mini App shop + simplified admin."""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

import security as sec
from config import settings
from gemini_lookup import parse_and_enrich
from security import utcnow

log = logging.getLogger("market")
BASE_DIR = Path(__file__).resolve().parent
ROLE_LEVEL = {"customer": 0, "support": 1, "admin": 2, "superadmin": 3}


class AppState:
    client: Any = None
    db: Any = None
    sessions: Optional[sec.SessionService] = None
    limiter: sec.RateLimiter = sec.RateLimiter(None)
    http: Any = None


S = AppState()


def db() -> Any:
    if S.db is None:
        raise HTTPException(503, "Database is not configured. Set MONGODB_URI.")
    return S.db


def oid(value: Any, label: str = "id") -> ObjectId:
    return ObjectId(sec.object_id_str(value, label))


def iso(dt: Any) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def money_to_minor(value: Any, label: str = "amount") -> int:
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise HTTPException(422, f"{label} must be a valid decimal")
    if d < 0 or d > Decimal("999999.99"):
        raise HTTPException(422, f"{label} out of range")
    return int(d * 100)


async def ensure_indexes(d: Any) -> None:
    A, D = ASCENDING, DESCENDING
    await d["users"].create_index([("email", A)], unique=True, sparse=True)
    await d["users"].create_index([("tg_id", A)], unique=True, sparse=True)
    await d["sessions"].create_index([("expires_at", A)], expireAfterSeconds=0)
    await d["sessions"].create_index([("user_id", A)])
    await d["rate_limits"].create_index([("expires_at", A)], expireAfterSeconds=0)
    await d["products"].create_index([("slug", A)], unique=True)
    await d["products"].create_index([("status", A), ("created_at", D)])
    await d["products"].create_index([("bin", A)])
    await d["cards"].create_index([("cc_hash", A)], unique=True)
    await d["cards"].create_index([("status", A), ("created_at", D)])
    await d["cards"].create_index([("bin", A)])
    await d["orders"].create_index([("number", A)], unique=True)
    await d["orders"].create_index([("user_id", A), ("created_at", D)])
    await d["wallet_accounts"].create_index([("user_id", A)], unique=True)
    await d["site_settings"].create_index([("_id", A)])


async def seed_defaults(d: Any) -> None:
    await d["site_settings"].update_one(
        {"_id": "site"},
        {"$setOnInsert": {
            "_id": "site",
            "brand_name": settings.brand_name,
            "tagline": "Cards marketplace via Telegram",
            "hero_title": "CCS Shop",
            "hero_subtitle": "Browse and buy securely inside Telegram.",
            "invite_only": settings.invite_only,
            "tax_rate_bps": 0,
            "points_per_unit": 1,
        }},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TgAuthIn(Strict):
    init_data: str = Field(min_length=10, max_length=4000)


class CardBulkIn(Strict):
    text: str = Field(min_length=5, max_length=200_000)
    price_minor: Optional[int] = Field(default=None, ge=0, le=50_000_00)
    status: Literal["available", "sold", "hold"] = "available"


class CardPriceIn(Strict):
    price_minor: int = Field(ge=0, le=50_000_00)
    status: Optional[Literal["available", "sold", "hold"]] = None


class ProductQuickIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    price: str
    bin: str = Field(default="", max_length=8)
    country: str = Field(default="", max_length=2)
    brand: str = Field(default="", max_length=40)
    card_type: str = Field(default="", max_length=20)
    stock: int = Field(default=1, ge=0, le=9999)
    description: str = Field(default="", max_length=2000)


class CheckoutIn(Strict):
    card_id: str


# ---------------------------------------------------------------------------
# Auth deps
# ---------------------------------------------------------------------------
async def get_session(request: Request) -> Optional[dict]:
    if getattr(request.state, "session_loaded", False):
        return request.state.session
    token = None
    via_cookie = False
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
    else:
        token = request.cookies.get(settings.cookie_name)
        via_cookie = True
    sess = await S.sessions.resolve(token) if (token and S.sessions) else None
    request.state.session_loaded = True
    request.state.session = sess
    request.state.via_cookie = via_cookie
    request.state.token = token
    return sess


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
            raise HTTPException(403, "Insufficient permissions")
        return user
    return dep


admin_user = require_role("admin")


def public_user(u: dict) -> dict:
    return {
        "id": str(u["_id"]),
        "name": u.get("name") or u.get("tg_first_name") or "User",
        "username": u.get("tg_username") or "",
        "photo_url": u.get("tg_photo_url") or "",
        "tg_id": u.get("tg_id"),
        "role": u.get("role", "customer"),
        "is_admin": ROLE_LEVEL.get(u.get("role", "customer"), 0) >= ROLE_LEVEL["admin"],
        "balance_minor": u.get("balance_minor", 0),
        "theme": u.get("theme", "dark"),
    }


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.mongodb_uri:
        S.client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=5000)
        S.db = S.client[settings.mongodb_db]
        S.sessions = sec.SessionService(S.db)
        S.limiter = sec.RateLimiter(S.db)
        try:
            await ensure_indexes(S.db)
            await seed_defaults(S.db)
        except PyMongoError as exc:
            log.error("MongoDB startup error: %s", type(exc).__name__)
    else:
        log.warning("MONGODB_URI not set")
    yield
    if S.client:
        await S.client.close()


app = FastAPI(title=f"{settings.brand_name} Mini App", version="2.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(sec.SecurityHeadersMiddleware)
_cors = settings.effective_origins() or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Telegram-Init-Data"],
    max_age=600,
)

api = APIRouter(prefix="/api")
admin = APIRouter(prefix="/api/admin", dependencies=[Depends(admin_user)])


# ---------------------------------------------------------------------------
# Public / config
# ---------------------------------------------------------------------------
@api.get("/health")
async def health():
    ok = S.db is not None
    return {"ok": ok, "status": "ok" if ok else "degraded"}


@api.get("/config")
async def get_config(user: Optional[dict] = Depends(optional_user)):
    site = await db()["site_settings"].find_one({"_id": "site"}) or {}
    return {
        "brand_name": site.get("brand_name") or settings.brand_name,
        "tagline": site.get("tagline", ""),
        "hero_title": site.get("hero_title", ""),
        "hero_subtitle": site.get("hero_subtitle", ""),
        "currency": settings.default_currency,
        "invite_only": site.get("invite_only", settings.invite_only),
        "is_admin": bool(user and ROLE_LEVEL.get(user.get("role", "customer"), 0) >= ROLE_LEVEL["admin"]),
        "telegram_webapp": True,
    }


# ---------------------------------------------------------------------------
# Telegram auth → session
# ---------------------------------------------------------------------------
@api.post("/auth/telegram")
async def auth_telegram(body: TgAuthIn, request: Request, response: Response):
    if not settings.telegram_bot_token:
        raise HTTPException(503, "Telegram bot is not configured")
    await S.limiter.enforce("tg_auth", sec.client_ip(request), settings.rl_login_max, settings.rl_login_window)

    data = sec.validate_telegram_webapp(body.init_data, settings.telegram_bot_token)
    tg_user = data.get("user") or {}
    tg_id = tg_user.get("id")
    if not tg_id:
        raise HTTPException(401, "No Telegram user in initData")

    tg_id = int(tg_id)
    first = (tg_user.get("first_name") or "")[:80]
    last = (tg_user.get("last_name") or "")[:80]
    username = (tg_user.get("username") or "")[:64]
    photo = (tg_user.get("photo_url") or "")[:500]
    name = (first + " " + last).strip() or username or f"tg_{tg_id}"

    # Promote to admin if tg_id is in TELEGRAM_ADMIN_IDS
    role = "admin" if settings.is_telegram_admin(tg_id) else "customer"

    now = utcnow()
    existing = await db()["users"].find_one({"tg_id": tg_id})
    if existing:
        update = {
            "tg_username": username,
            "tg_first_name": first,
            "tg_last_name": last,
            "tg_photo_url": photo,
            "name": name,
            "last_login_at": now,
        }
        # Keep elevated role if already staff; otherwise apply admin list
        if ROLE_LEVEL.get(existing.get("role", "customer"), 0) < ROLE_LEVEL["admin"] and role == "admin":
            update["role"] = "admin"
        elif settings.is_telegram_admin(tg_id) and existing.get("role") != "admin":
            update["role"] = "admin"
        await db()["users"].update_one({"_id": existing["_id"]}, {"$set": update})
        user = await db()["users"].find_one({"_id": existing["_id"]})
    else:
        user = {
            "tg_id": tg_id,
            "tg_username": username,
            "tg_first_name": first,
            "tg_last_name": last,
            "tg_photo_url": photo,
            "name": name,
            "email": None,
            "role": role,
            "status": "active",
            "balance_minor": 0,
            "created_at": now,
            "last_login_at": now,
            "theme": "dark",
        }
        res = await db()["users"].insert_one(user)
        user["_id"] = res.inserted_id

    token, sess = await S.sessions.create(user, request)
    response.set_cookie(
        settings.cookie_name,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
       )
    return {
        "user": public_user(user),
        "csrf": sess["csrf"],
        "token": token,  # Mini App can also keep bearer if cookie blocked
    }


@api.get("/auth/me")
async def auth_me(request: Request, user: dict = Depends(current_user)):
    sess = request.state.session
    return {"user": public_user(user), "csrf": sess.get("csrf") if sess else None}


@api.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.state.token if hasattr(request.state, "token") else None
    if not token:
        auth = request.headers.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip()
        else:
            token = request.cookies.get(settings.cookie_name)
    if S.sessions and token:
        await S.sessions.revoke(token)
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Catalog (cards as products)
# ---------------------------------------------------------------------------
@api.get("/cards")
async def list_cards(
    q: str = "",
    brand: str = "",
    country: str = "",
    status: str = "available",
    page: int = Query(1, ge=1, le=200),
    user: Optional[dict] = Depends(optional_user),
):
    flt: Dict[str, Any] = {"status": status or "available"}
    if brand:
        flt["brand"] = {"$regex": f"^{sec.clean_text(brand, 40)}", "$options": "i"}
    if country:
        flt["country"] = sec.clean_text(country, 2).upper()
    if q:
        safe = sec.clean_text(q, 40)
        flt["$or"] = [
            {"bin": {"$regex": safe}},
            {"brand": {"$regex": safe, "$options": "i"}},
            {"bank": {"$regex": safe, "$options": "i"}},
            {"city": {"$regex": safe, "$options": "i"}},
        ]
    page_size = 40
    total = await db()["cards"].count_documents(flt)
    cur = db()["cards"].find(flt).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
    items = []
    async for c in cur:
        items.append({
            "id": str(c["_id"]),
            "bin": c.get("bin", ""),
            "brand": c.get("brand", ""),
            "card_type": c.get("card_type", ""),
            "bank": c.get("bank", ""),
            "country": c.get("country", ""),
            "city": c.get("city", ""),
            "state": c.get("state", ""),
            "price_minor": c.get("price_minor", 0),
            "status": c.get("status", "available"),
            "exp_masked": (c.get("exp") or "")[:5],
            # full cc/cvv never sent to non-buyers
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@api.get("/cards/{cid}")
async def get_card(cid: str, user: Optional[dict] = Depends(optional_user)):
    c = await db()["cards"].find_one({"_id": oid(cid)})
    if not c:
        raise HTTPException(404, "Card not found")
    # Only reveal full details after purchase or to admin
    is_admin = user and ROLE_LEVEL.get(user.get("role", "customer"), 0) >= ROLE_LEVEL["admin"]
    owned = False
    if user:
        owned = bool(await db()["orders"].find_one({
            "user_id": user["_id"],
            "card_id": c["_id"],
            "status": "paid",
        }))
    base = {
        "id": str(c["_id"]),
        "bin": c.get("bin", ""),
        "brand": c.get("brand", ""),
        "card_type": c.get("card_type", ""),
        "bank": c.get("bank", ""),
        "country": c.get("country", ""),
        "city": c.get("city", ""),
        "state": c.get("state", ""),
        "zip": c.get("zip", "") if (owned or is_admin) else "",
        "address": c.get("address", "") if (owned or is_admin) else "",
        "price_minor": c.get("price_minor", 0),
        "status": c.get("status", "available"),
        "exp": c.get("exp", "") if (owned or is_admin) else (c.get("exp") or "")[:2] + "/**",
        "extra": c.get("extra", "") if (owned or is_admin) else "",
        "gemini_notes": c.get("gemini_notes", ""),
    }
    if owned or is_admin:
        base["cc"] = c.get("cc", "")
        base["cvv"] = c.get("cvv", "")
    else:
        base["cc"] = (c.get("bin") or "") + "******"
        base["cvv"] = "***"
    return base


# ---------------------------------------------------------------------------
# Checkout (wallet balance for simplicity in mini-app)
# ---------------------------------------------------------------------------
@api.post("/checkout")
async def checkout(body: CheckoutIn, request: Request, user: dict = Depends(current_user)):
    c = await db()["cards"].find_one({"_id": oid(body.card_id), "status": "available"})
    if not c:
        raise HTTPException(404, "Card not available")
    price = int(c.get("price_minor") or 0)
    bal = int(user.get("balance_minor") or 0)
    if bal < price:
        raise HTTPException(402, "Insufficient wallet balance. Top up first.")

    # Deduct + mark sold + create order atomically-ish
    res = await db()["users"].update_one(
        {"_id": user["_id"], "balance_minor": {"$gte": price}},
        {"$inc": {"balance_minor": -price}},
    )
    if res.modified_count == 0:
        raise HTTPException(402, "Insufficient wallet balance")

    sold = await db()["cards"].find_one_and_update(
        {"_id": c["_id"], "status": "available"},
        {"$set": {"status": "sold", "sold_at": utcnow(), "sold_to": user["_id"]}},
    )
    if not sold:
        # refund
        await db()["users"].update_one({"_id": user["_id"]}, {"$inc": {"balance_minor": price}})
        raise HTTPException(409, "Card was just taken by someone else")

    number = "ORD-" + secrets.token_hex(5).upper()
    order = {
        "number": number,
        "user_id": user["_id"],
        "card_id": c["_id"],
        "total_minor": price,
        "currency": settings.default_currency,
        "status": "paid",
        "created_at": utcnow(),
        "delivery": {
            "cc": c.get("cc"),
            "exp": c.get("exp"),
            "cvv": c.get("cvv"),
            "address": c.get("address"),
            "city": c.get("city"),
            "state": c.get("state"),
            "zip": c.get("zip"),
            "country": c.get("country"),
            "extra": c.get("extra"),
        },
    }
    await db()["orders"].insert_one(order)
    return {"ok": True, "order_number": number, "delivery": order["delivery"]}


@api.get("/orders")
async def my_orders(user: dict = Depends(current_user)):
    cur = db()["orders"].find({"user_id": user["_id"]}).sort("created_at", -1).limit(50)
    out = []
    async for o in cur:
        out.append({
            "number": o["number"],
            "total_minor": o.get("total_minor", 0),
            "status": o.get("status"),
            "created_at": iso(o.get("created_at")),
            "delivery": o.get("delivery") if o.get("status") == "paid" else None,
        })
    return {"orders": out}


@api.get("/wallet")
async def wallet(user: dict = Depends(current_user)):
    u = await db()["users"].find_one({"_id": user["_id"]})
    return {"balance_minor": int(u.get("balance_minor") or 0), "currency": settings.default_currency}


# ---------------------------------------------------------------------------
# ADMIN — simplified
# ---------------------------------------------------------------------------
@admin.get("/overview")
async def admin_overview():
    cards_avail = await db()["cards"].count_documents({"status": "available"})
    cards_sold = await db()["cards"].count_documents({"status": "sold"})
    users = await db()["users"].count_documents({})
    orders = await db()["orders"].count_documents({"status": "paid"})
    rev = 0
    async for o in db()["orders"].find({"status": "paid"}, {"total_minor": 1}):
        rev += int(o.get("total_minor") or 0)
    return {
        "cards_available": cards_avail,
        "cards_sold": cards_sold,
        "users": users,
        "orders_paid": orders,
        "revenue_minor": rev,
    }


@admin.post("/cards/bulk")
async def admin_cards_bulk(body: CardBulkIn, request: Request, user: dict = Depends(admin_user)):
    """Paste many lines of cc|exp|cvv|address|city|state|zip|country|extra
    Gemini enriches brand/type/bank; cards are inserted as available.
    """
    await S.limiter.enforce("admin_bulk", str(user["_id"]), 10, 300)
    parsed = await parse_and_enrich(body.text)
    if not parsed:
        raise HTTPException(422, "No valid card lines found. Use format: cc|exp|cvv|address|city|state|zip|country|extra")

    default_price = body.price_minor if body.price_minor is not None else 500  # $5.00
    inserted = 0
    skipped = 0
    now = utcnow()
    for c in parsed:
        cc_hash = sec.hash_token(c["cc"] + c["exp"] + c["cvv"])
        doc = {
            "cc": c["cc"],
            "cc_hash": cc_hash,
            "bin": c["bin"],
            "exp": c["exp"],
            "cvv": c["cvv"],
            "address": c.get("address", ""),
            "city": c.get("city", ""),
            "state": c.get("state", ""),
            "zip": c.get("zip", ""),
            "country": (c.get("country") or c.get("gemini_country") or "").upper()[:2],
            "extra": c.get("extra", ""),
            "brand": c.get("brand", ""),
            "card_type": c.get("card_type", ""),
            "bank": c.get("bank", ""),
            "gemini_notes": c.get("gemini_notes", ""),
            "luhn": c.get("luhn", ""),
            "price_minor": default_price,
            "status": body.status,
            "created_at": now,
            "created_by": user["_id"],
        }
        try:
            await db()["cards"].insert_one(doc)
            inserted += 1
        except DuplicateKeyError:
            skipped += 1
        except Exception as exc:
            log.warning("card insert fail: %s", type(exc).__name__)
            skipped += 1

    return {
        "ok": True,
        "parsed": len(parsed),
        "inserted": inserted,
        "skipped": skipped,
        "sample": [
            {
                "bin": c["bin"],
                "brand": c.get("brand"),
                "card_type": c.get("card_type"),
                "bank": c.get("bank"),
                "country": c.get("country") or c.get("gemini_country"),
            }
            for c in parsed[:5]
        ],
    }


@admin.get("/cards")
async def admin_list_cards(
    status: str = "",
    q: str = "",
    page: int = Query(1, ge=1, le=500),
):
    flt: Dict[str, Any] = {}
    if status in ("available", "sold", "hold"):
        flt["status"] = status
    if q:
        safe = sec.clean_text(q, 40)
        flt["$or"] = [
            {"bin": {"$regex": safe}},
            {"cc": {"$regex": safe}},
            {"brand": {"$regex": safe, "$options": "i"}},
            {"bank": {"$regex": safe, "$options": "i"}},
        ]
    page_size = 50
    total = await db()["cards"].count_documents(flt)
    cur = db()["cards"].find(flt).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
    items = []
    async for c in cur:
        items.append({
            "id": str(c["_id"]),
            "cc": c.get("cc"),
            "bin": c.get("bin"),
            "exp": c.get("exp"),
            "cvv": c.get("cvv"),
            "address": c.get("address"),
            "city": c.get("city"),
            "state": c.get("state"),
            "zip": c.get("zip"),
            "country": c.get("country"),
            "extra": c.get("extra"),
            "brand": c.get("brand"),
            "card_type": c.get("card_type"),
            "bank": c.get("bank"),
            "price_minor": c.get("price_minor", 0),
            "status": c.get("status"),
            "luhn": c.get("luhn"),
            "created_at": iso(c.get("created_at")),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@admin.patch("/cards/{cid}")
async def admin_patch_card(cid: str, body: CardPriceIn):
    upd: Dict[str, Any] = {"price_minor": body.price_minor}
    if body.status:
        upd["status"] = body.status
    res = await db()["cards"].update_one({"_id": oid(cid)}, {"$set": upd})
    if res.matched_count == 0:
        raise HTTPException(404, "Card not found")
    return {"ok": True}


@admin.delete("/cards/{cid}")
async def admin_delete_card(cid: str):
    res = await db()["cards"].delete_one({"_id": oid(cid)})
    return {"ok": True, "deleted": res.deleted_count}


@admin.post("/users/{uid}/credit")
async def admin_credit_user(uid: str, amount: str = Query(...), user: dict = Depends(admin_user)):
    minor = money_to_minor(amount)
    target = await db()["users"].find_one({"_id": oid(uid)})
    if not target:
        raise HTTPException(404, "User not found")
    await db()["users"].update_one({"_id": target["_id"]}, {"$inc": {"balance_minor": minor}})
    return {"ok": True, "credited_minor": minor}


@admin.get("/users")
async def admin_users(page: int = Query(1, ge=1, le=200)):
    page_size = 40
    total = await db()["users"].count_documents({})
    cur = db()["users"].find({}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
    items = []
    async for u in cur:
        items.append({
            "id": str(u["_id"]),
            "tg_id": u.get("tg_id"),
            "name": u.get("name"),
            "username": u.get("tg_username"),
            "role": u.get("role"),
            "balance_minor": u.get("balance_minor", 0),
            "status": u.get("status"),
            "created_at": iso(u.get("created_at")),
        })
    return {"items": items, "total": total, "page": page}


@admin.get("/orders")
async def admin_orders(page: int = Query(1, ge=1, le=200)):
    page_size = 40
    total = await db()["orders"].count_documents({})
    cur = db()["orders"].find({}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
    items = []
    async for o in cur:
        items.append({
            "number": o.get("number"),
            "user_id": str(o.get("user_id")),
            "total_minor": o.get("total_minor"),
            "status": o.get("status"),
            "created_at": iso(o.get("created_at")),
        })
    return {"items": items, "total": total, "page": page}


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------
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
async def css():
    return Response((BASE_DIR / "style.css").read_bytes(), media_type="text/css", headers={"Cache-Control": "public, max-age=300"})


@app.get("/app.js", include_in_schema=False)
async def app_js():
    return Response((BASE_DIR / "app.js").read_bytes(), media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


@app.get("/admin.js", include_in_schema=False)
async def admin_js():
    return Response((BASE_DIR / "admin.js").read_bytes(), media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


app.include_router(api)
app.include_router(admin)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=not settings.is_production)
