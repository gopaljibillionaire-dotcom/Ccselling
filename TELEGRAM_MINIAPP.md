# CCS Marketplace → Telegram Mini App

UI and storefront behaviour are unchanged. This build adds Telegram WebApp login, admin elevation by Telegram ID, and bulk card-unit import with Gemini parsing.

## What changed

1. **No email login required inside Telegram**
   - Mini App sends `initData` to `POST /api/auth/telegram`
   - Backend verifies HMAC (`WebAppData` secret = bot token)
   - User is upserted with `telegram_id`, name, username, photo
   - Session cookie + bearer token returned

2. **Admin button**
   - If Telegram user id is listed in `TELEGRAM_ADMIN_IDS`, role becomes `admin`
   - Storefront shows **Admin** when `role` is support/admin/superadmin
   - Admin panel opens at `/admin` (same UI)

3. **Bulk card data**
   Format per line:

   ```
   cc | exp | cvv | address | city | state | zip | country | extra info
   ```

   - Admin API: `POST /api/admin/inventory/units/bulk`
   - Body: `{ "product_id", "sku", "raw_text", "use_gemini": true }`
   - Gemini normalises messy lines when `GEMINI_API_KEY` is set; otherwise local pipe/tab/comma splitter
   - Each row → `stock_units` document + stock +1
   - On paid order, `issue_delivery` assigns unique units to the buyer

4. **Same UI**
   - `index.html` / `style.css` / `admin.html` layout kept
   - Telegram script injected; theme/colours untouched

## Bot setup

1. Create bot with @BotFather → copy token → `TELEGRAM_BOT_TOKEN`
2. Set Mini App URL to your HTTPS host (same as `PUBLIC_APP_URL`)
3. Put your Telegram numeric user id(s) in `TELEGRAM_ADMIN_IDS`
4. Deploy (Heroku example uses the Procfile)

## Env (minimum)

```
APP_SECRET=...
APP_ENV=production
PUBLIC_APP_URL=https://your-app.example
MONGODB_URI=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_IDS=123456789
GEMINI_API_KEY=...          # optional but recommended for bulk paste
COOKIE_SECURE=true
COOKIE_SAMESITE=none        # required for some Telegram WebView cookie cases
```

If cookies are blocked inside Telegram WebView, the client still sends `Authorization: Bearer <token>` from the telegram auth response.

## Admin bulk import (from admin UI)

In browser console on `/admin` (or use the helper):

```js
openBulkUnitsImporter("<productObjectId>", "DEFAULT")
```

Paste lines when prompted.

## Procfile

```
web: gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:$PORT --timeout 120 main:app
release: python main.py init-indexes
```

## Local run

```bash
export SKIP_CONFIG_VALIDATION=1
export APP_SECRET=dev-secret-at-least-32-characters-long
export MONGODB_URI=mongodb://127.0.0.1:27017
export TELEGRAM_BOT_TOKEN=123:ABC
export TELEGRAM_ADMIN_IDS=your_tg_id
export GEMINI_API_KEY=
python main.py
```

Open the Mini App from Telegram (or test `POST /api/auth/telegram` with captured initData).
