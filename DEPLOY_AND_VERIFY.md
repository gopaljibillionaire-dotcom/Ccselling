# Deploy, bootstrap, and verify — Security integration

## 1. Copy files into your application root

From `security_integration/` into the directory that already contains `main.py`:

- `security_events.py` (new)
- `main.py` (replace)
- `admin.html` (replace)
- `test_security.py` (optional, for CI)
- `.env.example` (reference)

No new pip packages are required beyond your existing `requirements.txt`.

## 2. Environment

1. Set production variables from `.env.example` (especially `APP_SECRET` ≥ 32 chars, `MONGODB_URI`, `PUBLIC_APP_URL=https://…`, `COOKIE_SECURE=true`, `ADMIN_REQUIRE_MFA=true`).
2. Prefer `REDIS_URL` with TLS (`rediss://`) so rate limits and short-lived counters are shared across workers.
3. Restrict MongoDB network access to the app’s egress IPs only.

## 3. Bootstrap administrator (no public endpoint)

```bash
export MONGODB_URI='…'
export APP_SECRET='…'
python main.py init-indexes
python main.py create-admin admin@your-domain.example superadmin
```

Sign in once at `/admin`, enrol TOTP when prompted. There is no HTTP route that creates admins.

## 4. Access the security dashboard

1. Open `https://your-domain.example/admin`
2. Authenticate with staff account + MFA
3. Sidebar → **Security**
4. Use filters (time window, severity, category, search) and **Refresh**
5. Open incidents → set status / add investigation notes
6. Active sessions → **Revoke** after confirmation

API (Bearer token from admin login):

- `GET /api/admin/security/overview?hours=24`
- `GET /api/admin/security/events?severity=high&page=1`
- `GET /api/admin/security/incidents`
- `POST /api/admin/security/incidents/{id}` body `{"status":"investigating","note":"…"}`
- `GET /api/admin/security/sessions`
- `POST /api/admin/security/sessions/{sid}/revoke`
- `POST /api/admin/security/users/{uid}/restrict` (customers only)

Ordinary customers and support-role staff receive 401/403.

## 5. Automated tests

On a machine with project dependencies installed:

```bash
cd /path/to/app
SKIP_CONFIG_VALIDATION=1 \
APP_SECRET='test-secret-at-least-32-characters-xx' \
APP_ENV=development \
pytest test_security.py security.py -q
```

For full route tests against a real DB, set `MONGODB_URI` to an isolated test database (never production).

**Note:** In this packaging environment PyPI was unreachable (502), so test execution could not be completed here. The suite is included and AST-validated; run it in your CI/staging.

## 6. Smoke verification checklist

- [ ] `GET /api/health` returns ok/degraded without stack traces
- [ ] Customer login still works; staff login requires MFA
- [ ] Failed login increments and appears under Security → events (`auth.login_failed`)
- [ ] Hitting admin APIs without token → 401
- [ ] Customer session token cannot call `/api/admin/security/*` → 403
- [ ] Security dashboard loads cards and tables
- [ ] Session revoke forces re-login on that device
- [ ] Response headers include `Content-Security-Policy`, `X-Content-Type-Options`, `X-Request-ID`
- [ ] Production refuses to start if `APP_SECRET` short or `PUBLIC_APP_URL` not https

## 7. Incident response (operator)

1. Security → filter severity **high/critical**
2. Open matching incident → status **investigating**, add notes
3. Revoke suspicious sessions; if customer account compromised, **Restrict** (suspends + revokes sessions)
4. Rotate affected secrets (Plisio key via Settings, `APP_SECRET` only with planned re-login of all users)
5. Mark incident **resolved** or **false_positive**
6. Export/archive audit if required by policy (query `security_events` / `audit_logs`)

Do **not** mass-suspend based on a single rate-limit event.

## 8. What this does not replace

- Network firewall / Mongo Atlas IP allow-list
- WAF / DDoS at the edge
- External SIEM log shipping (optional next step: tail `security_events` to your collector)
- Formal penetration test
