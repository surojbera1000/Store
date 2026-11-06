"""Shared helpers: screen rendering, input states, admin notifications."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardMarkup, InputFile, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

import config

log = logging.getLogger(__name__)

Ctx = ContextTypes.DEFAULT_TYPE
InputHandler = Callable[[Update, Ctx, dict], Awaitable[None]]

# name -> (coroutine(update, context, state), accepts non-text messages, admin only)
INPUT_HANDLERS: dict[str, tuple[InputHandler, bool, bool]] = {}


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def on_input(name: str, any_message: bool = False, admin: bool = False):
    """Register a handler for the next message a user sends while in state `name`.

    By default only text messages are accepted. Set `any_message` to accept
    photos, documents, etc. Set `admin` to restrict it to admins.
    """
    def decorator(func: InputHandler) -> InputHandler:
        INPUT_HANDLERS[name] = (func, any_message, admin)
        return func
    return decorator


async def route_input(update: Update, context: Ctx) -> None:
    """Dispatch a private message to the handler of the user's current state."""
    from ui.theme import btn, kb

    state = get_state(context)
    entry = INPUT_HANDLERS.get(state["name"]) if state else None
    if not entry:
        await update.message.reply_text("👇 Use the menu to continue.",
                                        reply_markup=kb([btn("🏠 Open Menu", "home")]))
        return
    func, any_message, admin_only = entry
    if admin_only and not is_admin(update.effective_user.id):
        clear_state(context)
        return
    if not any_message and not update.message.text:
        await update.message.reply_text("✏️ Please send a text message.")
        return
    await func(update, context, state)


def set_state(context: Ctx, name: str, **data) -> None:
    context.user_data["state"] = {"name": name, **data}


def clear_state(context: Ctx) -> None:
    context.user_data.pop("state", None)


def get_state(context: Ctx) -> Optional[dict]:
    return context.user_data.get("state")


async def show(update: Update, text: str, markup: Optional[InlineKeyboardMarkup] = None,
               photo: Optional[bytes] = None) -> Optional[Message]:
    """Render a screen.

    From a button press the current message is edited in place so the chat
    stays clean. If the old message cannot be edited into the new one (e.g.
    photo <-> text) it is replaced. From a typed message a new one is sent.
    """
    query = update.callback_query
    if query and query.message:
        msg = query.message
        try:
            if photo is None and msg.text is not None:
                return await msg.edit_text(text, parse_mode=ParseMode.HTML,
                                           reply_markup=markup)
            if photo is None and msg.photo and msg.caption is not None and len(text) <= 1024:
                return await msg.edit_caption(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return msg
            log.debug("edit failed, resending: %s", exc)
        try:
            await msg.delete()
        except BadRequest:
            pass
    chat = update.effective_chat
    if photo is not None:
        return await chat.send_photo(InputFile(photo, filename="qr.png"), caption=text,
                                     parse_mode=ParseMode.HTML, reply_markup=markup)
    return await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def send(context: Ctx, chat_id: int, text: str,
               markup: Optional[InlineKeyboardMarkup] = None) -> bool:
    """Send a message, ignoring users who blocked the bot. Returns success."""
    try:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=markup)
        return True
    except (Forbidden, BadRequest) as exc:
        log.info("could not message %s: %s", chat_id, exc)
        return False


async def notify_admins(context: Ctx, text: str,
                        markup: Optional[InlineKeyboardMarkup] = None) -> None:
    for admin_id in config.ADMIN_IDS:
        await send(context, admin_id, text, markup)


async def log_event(context: Ctx, text: str) -> None:
    if config.LOG_CHANNEL_ID:
        await send(context, config.LOG_CHANNEL_ID, text)


def user_link(user_id: int, username: Optional[str], first_name: Optional[str]) -> str:
    from ui.theme import e
    name = e(first_name or "User")
    tag = f" (@{e(username)})" if username else ""
    return f'<a href="tg://user?id={user_id}">{name}</a>{tag} <code>{user_id}</code>'
