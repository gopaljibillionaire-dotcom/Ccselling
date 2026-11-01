# Security Audit & Implementation Plan — CCS Marketplace

**Target:** OWASP ASVS Level 2 (where applicable)  
**Scope:** Existing FastAPI + MongoDB + Redis marketplace (main.py, security.py, admin.html, script.js)  
**Date:** 2026-10-28

---

## 1. Original controls already present (strengths)

| Area | Status | Notes |
|------|--------|-------|
| Password hashing | Strong | Argon2id via argon2-cffi, rehash-on-login |
| MFA for staff | Strong | TOTP (pyotp), encrypted secret at rest (Fernet), replay protection |
| Sessions | Strong | Opaque tokens, only SHA-256 stored, TTL + idle timeout, rotation on login |
| CSRF | Present | Double-submit for cookie sessions; Bearer admin path is CSRF-immune |
| Rate limiting | Present | Shared store (Redis → Mongo → memory); login, API, sensitive, payment scopes |
| NoSQL injection | Strong | `reject_operators`, `scalar`, Pydantic `extra="forbid"`, allow-listed sorts |
| XSS | Strong (FE) | No `innerHTML`; DOM built via `createElement` / text nodes |
| Uploads | Present | Magic-byte sniff, size caps, ImgBB allow-list hosts |
| Security headers | Present | CSP with per-request nonce, HSTS (prod), XFO, COOP, nosniff |
| Audit trail | Present | `audit_logs` collection; used on auth, admin, payments |
| RBAC | Present | `require_role` / ROLE_LEVEL; staff/admin/superadmin gates |
| Bootstrap admin | Safe | CLI `create-admin` only — no public create-admin endpoint |
| Money / stock | Strong | Integer minor units, no float; inventory movements audited |

---

## 2. Gaps identified (pre-change)

| Priority | Gap | Risk |
|----------|-----|------|
| P1 | No dedicated **security monitoring dashboard** in admin UI | Operators cannot see failed logins, rate hits, or incidents in one place |
| P1 | Audit logs are generic; no structured **security_events** with severity/outcome/request_id | Hard to alert, correlate, or retain with TTL |
| P1 | No **incident** grouping / investigation workflow | High-risk patterns not escalated |
| P2 | Admin `/audit` exists but is not surfaced in the admin nav as a first-class Security view | Underused |
| P2 | Request correlation ID not attached to every request | Weaker forensics |
| P2 | Session list for investigation limited to per-user account page | No global active-session view for responders |
| P3 | Account restriction after confirmed compromise is only “suspend” on users page | Needs explicit security-driven restrict + audit |
| P3 | Automated security regression suite not packaged for CI | Drift risk |

**Not found / not issues in this codebase:**

- Hardcoded production secrets in source (config is env-only; production fail-closed).
- Public privilege-escalation endpoint for role assignment.
- Trust of client-side role checks as authorization boundary (backend enforces).
- `innerHTML` XSS sinks in storefront/admin scripts.

---

## 3. Planned changes (executed in this integration)

1. **`security_events.py`** — structured events, 90-day TTL, incident grouping, overview/query helpers.
2. **`main.py`**
   - Import + index setup for security collections.
   - Enhanced `audit()` mirrors security-relevant events into `security_events`.
   - Admin routes under `/api/admin/security/*` (overview, events, incidents, sessions, revoke, restrict).
   - `X-Request-ID` middleware for correlation.
3. **`admin.html`** — new **Security** nav section + dashboard (overview cards, filters, incidents, events table, session revoke).
4. **`test_security.py`** — unit/smoke tests for modules, secret redaction, route registration, auth gates.
5. Documentation: this file, `.env.example`, runbook notes below.

**Explicitly preserved:** product catalog, checkout, wallet, Plisio, storefront UI, existing admin sections, session/cookie model, MFA flow.

---

## 4. ASVS L2 mapping (selected)

| ASVS | Control | Status after change |
|------|---------|---------------------|
| V2.1 / V2.2 | Password storage & MFA | Complete (pre-existing) |
| V3 | Session management | Complete (pre-existing) + global session list |
| V4 | Access control | Complete (pre-existing RBAC) |
| V5 | Validation / sanitization | Complete (pre-existing) |
| V7 | Error handling & logging | Improved — structured security events, no secret logging |
| V8 | Data protection | Pre-existing env secrets + Fernet MFA; TLS required in prod config |
| V9 | Communications | HSTS + HTTPS enforcement middleware in prod |
| V11 | Business logic | Stock/payment verification pre-existing |
| V14 | Config | Fail-closed production validation pre-existing |

**Incomplete without operator action:** external log shipping, Mongo/Redis network ACLs, WAF, and continuous pen-test — see Remaining risks.

---

## 5. Remaining risks & limitations

- **No live Mongo/Redis in this build environment** — full integration tests against a real DB must be run in your staging cluster.
- **Alert notifications** (email/Slack) are not wired; high severity only creates in-app incidents. Hook `security_incidents` to your notifier if needed.
- **IP-based signals** can be noisy behind CGNAT; do not auto-ban on a single failed login.
- **File origin admin** (`ADMIN_ALLOW_FILE_ORIGIN`) remains a deliberate dev escape hatch — disable in production.
- Security dashboard is **admin/superadmin only**; support role cannot access it (by design).

---

## 6. Files created / modified

| Path | Action |
|------|--------|
| `security_events.py` | **Created** |
| `main.py` | **Modified** (import, audit, indexes, routes, middleware) |
| `admin.html` | **Modified** (Security section + view) |
| `test_security.py` | **Created** |
| `SECURITY_AUDIT_AND_PLAN.md` | **Created** |
| `.env.example` | **Created** |
| `config.py`, `security.py`, `script.js`, `style.css`, `index.html`, `requirements.txt` | Unchanged (no new dependencies required) |

Original copies also kept under this folder as the working tree.
