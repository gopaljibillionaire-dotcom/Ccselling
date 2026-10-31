"""bot.py - Telegram bot that opens the Mini App + restricted admin entry."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
WEBAPP_URL = (os.environ.get("TELEGRAM_WEBAPP_URL") or os.environ.get("PUBLIC_APP_URL") or "").rstrip("/")
ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}


def webapp_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Open Shop", web_app=WebAppInfo(url=WEBAPP_URL + "/"))],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("Admin Panel", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_admin = user.id in ADMIN_IDS
    text = (
        f"Hey {user.first_name or 'there'}.\n\n"
        "This is the CCS Shop Mini App.\n"
        "Tap the button below to browse and buy."
    )
    if is_admin:
        text += "\n\nYou are an admin — Admin Panel button is visible."
    await update.message.reply_text(text, reply_markup=webapp_keyboard(is_admin))


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await update.message.reply_text("Not authorised.")
        return
    await update.message.reply_text(
        "Admin Mini App:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open Admin", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))]]
        ),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start — open shop\n"
        "/admin — admin panel (restricted)\n"
        "/help — this message"
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not WEBAPP_URL:
        raise SystemExit("TELEGRAM_WEBAPP_URL or PUBLIC_APP_URL is not set")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    log.info("Bot starting. WebApp URL=%s admins=%s", WEBAPP_URL, ADMIN_IDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
