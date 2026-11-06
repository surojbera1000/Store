"""Entry point: python bot.py"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque

from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter, Application, ApplicationHandlerStop, Defaults, MessageHandler, TypeHandler,
    filters,
)

import config
from core import Ctx, clear_state, is_admin, route_input
from database import db
from handlers import admin, deposit, user

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

REF_RE = re.compile(r"^/start\s+ref_(\d+)$")
_hits: dict[int, deque] = defaultdict(deque)


async def gate(update: Update, context: Ctx) -> None:
    """Runs before every handler: registers users, rate-limits, enforces bans/maintenance."""
    tg_user = update.effective_user
    if not tg_user or not update.effective_chat:
        raise ApplicationHandlerStop
    if update.effective_chat.type != "private" and not update.callback_query:
        raise ApplicationHandlerStop

    # Simple sliding-window rate limit
    if not is_admin(tg_user.id):
        window = _hits[tg_user.id]
        now = time.monotonic()
        while window and now - window[0] > 10:
            window.popleft()
        window.append(now)
        if len(window) > config.RATE_LIMIT:
            if update.callback_query:
                await update.callback_query.answer("⏳ Slow down a little.", show_alert=False)
            raise ApplicationHandlerStop

    referrer = None
    if update.message and update.message.text:
        match = REF_RE.match(update.message.text.strip())
        if match:
            referrer = int(match.group(1))
    if not context.user_data.get("registered") or referrer:
        await db.upsert_user(tg_user.id, tg_user.username, tg_user.first_name, referrer)
        context.user_data["registered"] = True

    if is_admin(tg_user.id):
        if update.callback_query:
            clear_state(context)
        return

    data = await db.get_user(tg_user.id)
    if data and data["banned"]:
        if update.callback_query:
            await update.callback_query.answer("🚫 You are banned from this store.", show_alert=True)
        else:
            await update.effective_chat.send_message("🚫 You are banned from this store.")
        raise ApplicationHandlerStop

    if await db.get_setting("maintenance") == "1":
        text = "🛠 The store is under maintenance. Please check back soon."
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        else:
            await update.effective_chat.send_message(text)
        raise ApplicationHandlerStop

    # Pressing any button cancels a pending text prompt; the handler sets a new one if needed.
    if update.callback_query:
        clear_state(context)


async def on_error(update: object, context: Ctx) -> None:
    log.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            if update.callback_query:
                await update.callback_query.answer("⚠️ Something went wrong. Please try again.",
                                                   show_alert=True)
            else:
                await update.effective_chat.send_message("⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


async def post_init(app: Application) -> None:
    await db.connect()
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 Main menu"),
        BotCommand("help", "🆘 Help & support"),
    ])
    log.info("Bot started as @%s", app.bot.username)


async def post_shutdown(app: Application) -> None:
    await db.close()


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: nobody can open the admin panel.")

    defaults = Defaults(parse_mode=ParseMode.HTML,
                        link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .defaults(defaults)
        .rate_limiter(AIORateLimiter(max_retries=2))
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(TypeHandler(Update, gate), group=-1)
    user.register(app)
    deposit.register(app)
    admin.register(app)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, route_input))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
