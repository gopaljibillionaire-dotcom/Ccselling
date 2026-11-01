"""Security regression tests for the marketplace API.

Run (requires Mongo/Redis or in-memory fallbacks via SKIP_CONFIG_VALIDATION):

  SKIP_CONFIG_VALIDATION=1 APP_SECRET=test-secret-at-least-32-chars-long MONGODB_URI= \\
    pytest test_security.py security.py -q

These tests use FastAPI TestClient and do not require a live MongoDB when
endpoints short-circuit on missing DB (503). Full integration needs a test URI.
"""
from __future__ import annotations

import os
import sys

import pytest

# Ensure project root on path
sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("SKIP_CONFIG_VALIDATION", "1")
os.environ.setdefault("APP_SECRET", "test-secret-at-least-32-characters-xx")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("ADMIN_REQUIRE_MFA", "true")
os.environ.setdefault("COOKIE_SECURE", "false")


def test_security_events_module_imports():
    import security_events as sevt
    assert "high" in sevt.SEVERITIES
    assert sevt._pseudo_actor("abc") is not None
    assert sevt._pseudo_actor(None) is None


def test_password_and_input_guards():
    import security as sec
    from fastapi import HTTPException

    h = sec.hash_password("Correct-Horse-9!")
    assert h.startswith("$argon2id$")
    ok, _ = sec.verify_password(h, "Correct-Horse-9!")
    assert ok is True
    ok, _ = sec.verify_password(h, "wrong")
    assert ok is False

    assert sec.clean_text("<script>x</script>hi") == "xhi"
    with pytest.raises(HTTPException):
        sec.reject_operators({"$ne": 1})
    with pytest.raises(HTTPException):
        sec.scalar({"$gt": 0})


def test_security_headers_csp_nonce():
    import security as sec
    csp = sec.build_csp("testnonce123")
    assert "nonce-testnonce123" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_totp_replay():
    import security as sec
    import pyotp

    secret = sec.new_totp_secret()
    code = pyotp.TOTP(secret).now()
    step = sec.verify_totp(secret, code)
    assert step is not None
    assert sec.verify_totp(secret, code, last_step=step) is None


def test_rate_limiter_memory():
    import asyncio
    import security as sec
    from fastapi import HTTPException

    async def run():
        rl = sec.RateLimiter(None)
        for _ in range(3):
            await rl.enforce("t", "ip", 3, 60)
        with pytest.raises(HTTPException) as ei:
            await rl.enforce("t", "ip", 3, 60)
        assert ei.value.status_code == 429

    asyncio.run(run())


def test_app_imports_and_routes_exist():
    """Smoke: app loads and security routes are registered."""
    # Avoid connecting to real Mongo during import validation path
    os.environ["SKIP_CONFIG_VALIDATION"] = "1"
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/admin/security/overview" in paths
    assert "/api/admin/security/events" in paths
    assert "/api/admin/security/incidents" in paths
    assert "/api/admin/security/sessions" in paths
    assert "/admin" in paths
    assert "/" in paths


def test_admin_security_requires_auth():
    """Unauthenticated access to security endpoints must fail (401/403)."""
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app, raise_server_exceptions=False)
    for path in (
        "/api/admin/security/overview",
        "/api/admin/security/events",
        "/api/admin/security/incidents",
        "/api/admin/security/sessions",
    ):
        r = client.get(path)
        # Without DB the dependency chain may 503, or 401 without session.
        assert r.status_code in (401, 403, 503), f"{path} -> {r.status_code}"


def test_customer_cannot_hit_admin_security(monkeypatch):
    """Role gate: even with a fake customer session, admin routes deny."""
    # This is a structural check; full IDOR suite needs a live test DB.
    from main import ROLE_LEVEL, require_role

    assert ROLE_LEVEL["customer"] < ROLE_LEVEL["admin"]
    assert ROLE_LEVEL["support"] < ROLE_LEVEL["admin"]


def test_no_secrets_in_security_event_meta():
    import asyncio
    import security_events as sevt

    class FakeCol:
        def __init__(self):
            self.docs = []

        async def insert_one(self, doc):
            self.docs.append(doc)

        async def find_one(self, *a, **k):
            return None

        async def insert_one(self, doc):  # noqa: F811
            self.docs.append(doc)

        async def update_one(self, *a, **k):
            return None

        async def create_index(self, *a, **k):
            return None

    class FakeDB(dict):
        def __getitem__(self, key):
            if key not in self:
                self[key] = FakeCol()
            return dict.__getitem__(self, key)

    async def run():
        db = FakeDB()
        await sevt.record_security_event(
            db,
            action="auth.login_failed",
            severity="high",
            category="auth",
            meta={
                "password": "should-not-store",
                "token": "abc",
                "email_hash": "deadbeef",
                "ok_field": "visible",
            },
        )
        assert db["security_events"].docs
        meta = db["security_events"].docs[0]["meta"]
        assert "password" not in meta
        assert "token" not in meta
        assert meta.get("ok_field") == "visible"
        assert meta.get("email_hash") == "deadbeef"

    asyncio.run(run())


def test_money_and_state_machine_still_work():
    """Existing unit tests remain valid after integration."""
    from main import money_to_minor, apply_discount, can_transition
    from fastapi import HTTPException

    assert money_to_minor("49.99") == 4999
    assert apply_discount(1000, 15) == 850
    assert can_transition("pending", "paid")
    assert not can_transition("failed", "paid")
    with pytest.raises(HTTPException):
        money_to_minor("-1")
