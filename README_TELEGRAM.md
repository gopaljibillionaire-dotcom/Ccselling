# CCS Marketplace — Telegram Mini App build

Same UI and marketplace behaviour as the original site, adapted to run as a Telegram Mini App.

## What changed

1. **No forced login UI for Telegram users**  
   When the storefront or `/admin` is opened inside Telegram, `initData` is verified with your bot token and a session is created automatically.  
   Profile uses Telegram user id, display name, and photo.

2. **Admin via Telegram IDs**  
   Set `TELEGRAM_ADMIN_IDS=123456789,987654321` (numeric Telegram user ids).  
   Those accounts are promoted to `admin` on login. The storefront shows an **Admin** button for staff roles; it opens `/admin`.

3. **Bulk card import (admin)**  
   New admin section **Bulk cards**. Paste lines:

   ```
   cc | exp | cvv | address | city | state | zip | country | extra info
   ```

   - With `GEMINI_API_KEY` set, lines are structured via Gemini.
   - Without it, a local `"|"` / `","` / `";"` splitter is used.
   - **Parse lines** previews rows. **Parse + create products** creates one product per valid row (stock 1) with delivery fields filled for the buyer after payment.

4. **UI**  
   Storefront and admin layouts are unchanged. Only additions: Admin button (staff), Telegram avatar badge, Bulk cards admin page, Telegram WebApp script.

## Env vars (new)

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_ADMIN_IDS=111111111,222222222
TELEGRAM_AUTH_MAX_AGE_SECONDS=86400
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash
```

Keep existing MongoDB, Plisio, ImgBB, APP_SECRET, PUBLIC_APP_URL, etc.

## Telegram setup

1. Create a bot with @BotFather.
2. `/newapp` (or BotFather Mini App menu) → set Web App URL to your `PUBLIC_APP_URL` (HTTPS).
3. Put the bot token in `TELEGRAM_BOT_TOKEN`.
4. Open the mini app once with your Telegram account; note your numeric user id (or use `@userinfobot`). Add it to `TELEGRAM_ADMIN_IDS`.
5. Reopen the mini app → Admin button appears → Bulk cards / full admin panel.

## API additions

| Method | Path | Who | Purpose |
|--------|------|-----|---------|
| POST | `/api/auth/telegram` | public | Exchange `init_data` for session token + user |
| POST | `/api/admin/cards/parse` | admin | Parse lines (optional `create_products`) |
| POST | `/api/admin/cards/bulk-create` | admin | Create products from pre-parsed objects |

Password login still works for browser access to `/admin` (email + MFA as before). Telegram path is additional.

## Run

```bash
cd telegram_miniapp
# set env (or .env loaded by your process manager)
pip install -r requirements.txt
python main.py init-indexes
uvicorn main:app --host 0.0.0.0 --port 8000
```

Deploy behind HTTPS; set `PUBLIC_APP_URL` and `ALLOWED_ORIGINS` to that origin. Telegram WebView must load the same origin.
