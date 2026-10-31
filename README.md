# CCS Telegram Mini App Shop

Full stack:

- **Telegram Mini App** storefront (shop / orders / wallet)
- **Simplified admin panel** (bulk card add + Gemini BIN enrichment)
- **Telegram bot** that opens the Mini App
- Admin access **only** for Telegram user IDs listed in `TELEGRAM_ADMIN_IDS`
- Card paste format: `cc|exp|cvv|address|city|state|zip|country|extra`

## Quick start

### 1. Environment

```bash
cp .env.example .env
# edit .env — set at least:
#   MONGODB_URI
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_ADMIN_IDS=your_tg_numeric_id
#   TELEGRAM_WEBAPP_URL=https://your-domain.com
#   PUBLIC_APP_URL=https://your-domain.com
#   GEMINI_API_KEY=...
#   APP_SECRET=long-random-string-32+
```

Get your Telegram numeric ID from `@userinfobot`.

### 2. Install

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run API

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

HTTPS is required for real Telegram WebApps (use Cloudflare Tunnel, ngrok, or a VPS + Caddy).

### 4. Run bot

```bash
python bot.py
```

### 5. BotFather

1. `/newbot` → copy token into `.env`
2. `/setmenubutton` or `/newapp` → point Web App URL to `https://your-domain.com/`
3. Open the bot → **Open Shop**

Admin users (IDs in `TELEGRAM_ADMIN_IDS`) also see **Admin Panel**.

## Card bulk add

In Admin → **Add cards**, paste:

```
4111111111111111|12/28|123|123 Main St|New York|NY|10001|US|vip customer
5500000000000004|01/27|456|1 King Rd|London||SW1A|GB|
```

Gemini enriches each BIN with brand / type / bank / country. Duplicates (same cc+exp+cvv) are skipped.

## Security notes

- Telegram `initData` is HMAC-validated with the bot token before any session is issued.
- Only `TELEGRAM_ADMIN_IDS` receive `role=admin` and can open `/admin` APIs.
- Full PAN/CVV is only returned after a paid order (or to admins).
- CSRF + cookie / bearer sessions, rate limits on auth and bulk import.
- CSP allows Telegram WebApp framing (`frame-ancestors` includes `web.telegram.org`).

## Layout

```
main.py            FastAPI app + routes
bot.py             Telegram bot (polling)
config.py          env settings
security.py        sessions, CSRF, TG validation
gemini_lookup.py   parse lines + Gemini enrich
index.html / app.js     Mini App shop
admin.html / admin.js   simplified admin
requirements.txt
.env.example
```

## Wallet

Customers pay from wallet balance. Admins credit users under **Users** → amount → **+**.

## Production

- Put `APP_ENV=production`
- `COOKIE_SECURE=true`
- Serve only over HTTPS
- Keep `TELEGRAM_ADMIN_IDS` tight
- Run bot as a separate process (systemd / Docker)
