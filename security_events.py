"""security_events.py - Structured security event logging, incident grouping, and dashboard queries.

Extends the existing audit() helper without replacing it. High-severity events can raise
incidents that admins investigate from the Security dashboard.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

log = logging.getLogger("security_events")

# Severity ladder used for filtering and alerting.
SEVERITIES = ("info", "low", "medium", "high", "critical")

# Category taxonomy (maps to existing audit categories + new ones).
CATEGORIES = (
    "auth",
    "mfa",
    "authz",
    "session",
    "rate_limit",
    "input",
    "admin",
    "config",
    "payment",
    "order",
    "security",
    "system",
)

# Actions that auto-open or escalate an incident when severity >= high.
INCIDENT_TRIGGER_ACTIONS = {
    "auth.login_failed",
    "auth.mfa_failed",
    "authz.denied",
    "ratelimit.hit",
    "security.injection_attempt",
    "security.suspicious_request",
    "session.hijack_suspected",
    "admin.privilege_change",
    "config.security_change",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _event_id() -> str:
    return secrets.token_hex(8)


def _pseudo_actor(actor_id: Any) -> Optional[str]:
    """Pseudonymous identifier — never the raw ObjectId in exported feeds."""
    if actor_id is None:
        return None
    raw = str(actor_id).encode()
    return "u:" + hashlib.sha256(raw).hexdigest()[:12]


async def ensure_security_indexes(db: Any) -> None:
    """Idempotent indexes for security_events and security_incidents collections."""
    A, D = 1, -1  # ASC / DESC shorthand for create_index
    await db["security_events"].create_index([("created_at", D)])
    await db["security_events"].create_index([("severity", A), ("created_at", D)])
    await db["security_events"].create_index([("category", A), ("created_at", D)])
    await db["security_events"].create_index([("action", A), ("created_at", D)])
    await db["security_events"].create_index([("request_id", A)], sparse=True)
    await db["security_events"].create_index([("actor_pseudo", A), ("created_at", D)], sparse=True)
    await db["security_events"].create_index(
        [("created_at", A)],
        expireAfterSeconds=90 * 24 * 3600,  # 90-day retention
        name="security_events_ttl",
    )
    await db["security_incidents"].create_index([("status", A), ("updated_at", D)])
    await db["security_incidents"].create_index([("severity", A), ("created_at", D)])
    await db["security_incidents"].create_index([("created_at", A)], expireAfterSeconds=180 * 24 * 3600)


async def record_security_event(
    db: Any,
    *,
    action: str,
    severity: str = "info",
    category: str = "security",
    outcome: str = "observed",
    actor_id: Any = None,
    target: Any = None,
    ip: Optional[str] = None,
    request_id: Optional[str] = None,
    meta: Optional[dict] = None,
    correlation_key: Optional[str] = None,
) -> Optional[str]:
    """Insert a structured security event. Returns event_id or None on failure.

    Never stores passwords, tokens, session cookies, MFA secrets, or full request bodies.
    """
    if severity not in SEVERITIES:
        severity = "info"
    if category not in CATEGORIES:
        category = "security"
    # Sanitize meta — drop any keys that look like secrets.
    safe_meta: Dict[str, Any] = {}
    if meta:
        for k, v in meta.items():
            kl = str(k).lower()
            if any(s in kl for s in ("password", "token", "secret", "cookie", "authorization", "api_key", "mfa")):
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                safe_meta[k] = v if not isinstance(v, str) or len(v) < 200 else v[:200]
            elif isinstance(v, list) and len(v) < 20:
                safe_meta[k] = [str(x)[:80] for x in v[:10]]
    eid = _event_id()
    doc = {
        "event_id": eid,
        "action": action[:80],
        "severity": severity,
        "category": category,
        "outcome": outcome[:40],
        "actor_id": actor_id,
        "actor_pseudo": _pseudo_actor(actor_id),
        "target": str(target)[:120] if target is not None else None,
        "ip": (ip or "")[:64] or None,
        "request_id": request_id or None,
        "correlation_key": correlation_key or None,
        "meta": safe_meta,
        "created_at": _utcnow(),
    }
    try:
        await db["security_events"].insert_one(doc)
    except Exception as exc:  # pragma: no cover
        log.error("security_event write failed: %s", type(exc).__name__)
        return None

    # Incident escalation for high/critical + known trigger actions.
    if severity in ("high", "critical") or action in INCIDENT_TRIGGER_ACTIONS:
        try:
            await _maybe_open_incident(db, doc)
        except Exception as exc:  # pragma: no cover
            log.warning("incident escalation failed: %s", type(exc).__name__)
    return eid


async def _maybe_open_incident(db: Any, event: dict) -> None:
    """Group related high-severity events into an open incident (24h window, same action+ip)."""
    key = event.get("correlation_key") or f"{event['action']}|{event.get('ip') or 'none'}"
    window_start = _utcnow() - timedelta(hours=24)
    existing = await db["security_incidents"].find_one(
        {"correlation_key": key, "status": {"$in": ["open", "investigating"]}, "created_at": {"$gte": window_start}}
    )
    if existing:
        await db["security_incidents"].update_one(
            {"_id": existing["_id"]},
            {
                "$inc": {"event_count": 1},
                "$set": {"updated_at": _utcnow(), "severity": _max_sev(existing.get("severity", "info"), event["severity"])},
                "$push": {"event_ids": {"$each": [event["event_id"]], "$slice": -50}},
            },
        )
        return
    await db["security_incidents"].insert_one(
        {
            "correlation_key": key,
            "title": f"{event['action']} ({event.get('ip') or 'unknown'})",
            "severity": event["severity"],
            "status": "open",
            "event_count": 1,
            "event_ids": [event["event_id"]],
            "notes": [],
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
            "assigned_to": None,
        }
    )


def _max_sev(a: str, b: str) -> str:
    try:
        return a if SEVERITIES.index(a) >= SEVERITIES.index(b) else b
    except ValueError:
        return b


async def security_overview(db: Any, hours: int = 24) -> dict:
    """Aggregate counts for the dashboard overview cards."""
    since = _utcnow() - timedelta(hours=max(1, min(hours, 168)))
    pipeline = [
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": {"severity": "$severity", "category": "$category"}, "n": {"$sum": 1}}},
    ]
    by_sev: Dict[str, int] = {s: 0 for s in SEVERITIES}
    by_cat: Dict[str, int] = {}
    total = 0
    async for row in db["security_events"].aggregate(pipeline):
        sev = row["_id"].get("severity") or "info"
        cat = row["_id"].get("category") or "security"
        n = row["n"]
        total += n
        by_sev[sev] = by_sev.get(sev, 0) + n
        by_cat[cat] = by_cat.get(cat, 0) + n

    failed_logins = await db["security_events"].count_documents(
        {"action": "auth.login_failed", "created_at": {"$gte": since}}
    )
    mfa_fails = await db["security_events"].count_documents(
        {"action": "auth.mfa_failed", "created_at": {"$gte": since}}
    )
    authz_denials = await db["security_events"].count_documents(
        {"action": "authz.denied", "created_at": {"$gte": since}}
    )
    rate_hits = await db["security_events"].count_documents(
        {"action": {"$in": ["ratelimit.hit", "rate_limit.hit"]}, "created_at": {"$gte": since}}
    )
    open_incidents = await db["security_incidents"].count_documents({"status": {"$in": ["open", "investigating"]}})
    admin_actions = await db["security_events"].count_documents(
        {"category": "admin", "created_at": {"$gte": since}}
    )

    # Active sessions (approximate: non-expired).
    now = _utcnow()
    active_sessions = await db["sessions"].count_documents({"expires_at": {"$gt": now}, "kind": "full"})
    staff_sessions = await db["sessions"].count_documents(
        {"expires_at": {"$gt": now}, "kind": "full", "role": {"$in": ["support", "admin", "superadmin"]}}
    )

    return {
        "window_hours": hours,
        "total_events": total,
        "by_severity": by_sev,
        "by_category": by_cat,
        "failed_logins": failed_logins,
        "mfa_failures": mfa_fails,
        "authz_denials": authz_denials,
        "rate_limit_hits": rate_hits,
        "open_incidents": open_incidents,
        "admin_actions": admin_actions,
        "active_sessions": active_sessions,
        "staff_sessions": staff_sessions,
    }


async def query_events(
    db: Any,
    *,
    severity: str = "",
    category: str = "",
    action: str = "",
    q: str = "",
    hours: int = 72,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Paginated, filterable security event list for the dashboard."""
    page = max(1, min(page, 500))
    page_size = max(10, min(page_size, 100))
    since = _utcnow() - timedelta(hours=max(1, min(hours, 720)))
    flt: Dict[str, Any] = {"created_at": {"$gte": since}}
    if severity and severity in SEVERITIES:
        flt["severity"] = severity
    if category and category in CATEGORIES:
        flt["category"] = category
    if action:
        flt["action"] = {"$regex": f"^{action[:40]}"}
    if q:
        # Simple search on action / target / ip / event_id (no operator injection — q is scalar).
        safe = str(q)[:60].replace("$", "").replace(".", "")
        flt["$or"] = [
            {"action": {"$regex": safe, "$options": "i"}},
            {"target": {"$regex": safe, "$options": "i"}},
            {"ip": {"$regex": safe, "$options": "i"}},
            {"event_id": safe},
        ]
    total = await db["security_events"].count_documents(flt)
    cursor = (
        db["security_events"]
        .find(flt, {"_id": 0, "actor_id": 0})  # never return raw actor ObjectId to client
        .sort("created_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    async for e in cursor:
        items.append(
            {
                "event_id": e.get("event_id"),
                "action": e.get("action"),
                "severity": e.get("severity"),
                "category": e.get("category"),
                "outcome": e.get("outcome"),
                "actor_pseudo": e.get("actor_pseudo"),
                "target": e.get("target"),
                "ip": e.get("ip"),
                "request_id": e.get("request_id"),
                "meta": e.get("meta") or {},
                "at": e["created_at"].isoformat() if e.get("created_at") else None,
            }
        )
    return {"events": items, "total": total, "page": page, "page_size": page_size}


async def query_incidents(db: Any, status: str = "", page: int = 1) -> dict:
    page = max(1, min(page, 100))
    flt: Dict[str, Any] = {}
    if status in ("open", "investigating", "resolved", "false_positive"):
        flt["status"] = status
    cursor = db["security_incidents"].find(flt).sort("updated_at", -1).skip((page - 1) * 30).limit(30)
    items = []
    async for i in cursor:
        items.append(
            {
                "id": str(i["_id"]),
                "title": i.get("title"),
                "severity": i.get("severity"),
                "status": i.get("status"),
                "event_count": i.get("event_count", 0),
                "notes": i.get("notes") or [],
                "created_at": i["created_at"].isoformat() if i.get("created_at") else None,
                "updated_at": i["updated_at"].isoformat() if i.get("updated_at") else None,
            }
        )
    return {"incidents": items}


async def update_incident(
    db: Any,
    incident_id: str,
    *,
    status: Optional[str] = None,
    note: Optional[str] = None,
    actor_id: Any = None,
) -> bool:
    if not ObjectId.is_valid(incident_id):
        return False
    upd: Dict[str, Any] = {"updated_at": _utcnow()}
    if status in ("open", "investigating", "resolved", "false_positive"):
        upd["status"] = status
    ops: Dict[str, Any] = {"$set": upd}
    if note:
        ops["$push"] = {
            "notes": {
                "body": str(note)[:1000],
                "at": _utcnow(),
                "by": str(actor_id) if actor_id else None,
            }
        }
    res = await db["security_incidents"].update_one({"_id": ObjectId(incident_id)}, ops)
    return res.modified_count > 0


async def list_active_sessions(db: Any, limit: int = 100) -> List[dict]:
    """Staff-facing session list (no raw tokens)."""
    now = _utcnow()
    out = []
    async for s in db["sessions"].find({"expires_at": {"$gt": now}, "kind": "full"}).sort("last_seen", -1).limit(limit):
        out.append(
            {
                "id": s["_id"],  # hash of token — safe to show / revoke by
                "user_id": str(s["user_id"]),
                "role": s.get("role"),
                "ip": s.get("ip"),
                "ua": (s.get("ua") or "")[:80],
                "created_at": s["created_at"].isoformat() if s.get("created_at") else None,
                "last_seen": s["last_seen"].isoformat() if s.get("last_seen") else None,
                "expires_at": s["expires_at"].isoformat() if s.get("expires_at") else None,
            }
        )
    return out
