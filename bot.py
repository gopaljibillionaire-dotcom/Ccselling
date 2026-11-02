"""bot.py — Telegram entry bot for the CCS mini app.

Only handles /start (and /help). Opens the web mini app + support contact.
Does not touch marketplace backend logic.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ccs-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MINI_APP_URL = os.environ.get("MINI_APP_URL", "").strip().rstrip("/")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support").strip().lstrip("@")
BRAND_NAME = os.environ.get("BRAND_NAME", "CCS Shop").strip() or "CCS Shop"


def _welcome_text(username: str | None) -> str:
    name = f"@{username}" if username else "there"
    return (
        f"<b>Hello dear {name}</b>\n"
        f"\n"
        f"Welcome to <b>{BRAND_NAME}</b>.\n"
        f"\n"
        f"Here you can buy many-country charged CCs, BINs &amp; fullz "
        f"at different prices — with full trust, a refundable guarantee, "
        f"and real human support when you need it.\n"
        f"\n"
        f"Tap <b>Open Shop</b> below to launch the mini app.\n"
        f"Need help? Hit <b>Support</b> and message us directly."
    )


def _keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if MINI_APP_URL:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🛒  Open Shop",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                )
            ]
        )
    else:
        log.warning("MINI_APP_URL is empty — Open Shop button will be missing")
    rows.append(
        [
            InlineKeyboardButton(
                text="💬  Support",
                url=f"https://t.me/{SUPPORT_USERNAME}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    user = update.effective_user
    text = _welcome_text(user.username or user.first_name)
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_keyboard(),
        disable_web_page_preview=True,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in the environment or .env")
    if not MINI_APP_URL:
        log.warning("MINI_APP_URL not set — set it to your https shop URL")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    log.info("bot online | mini_app=%s support=@%s", MINI_APP_URL or "(none)", SUPPORT_USERNAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
