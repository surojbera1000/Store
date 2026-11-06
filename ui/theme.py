"""Visual theme: colored buttons, text helpers and reusable message pieces.

Button colors use the `style` field added in Telegram Bot API 9.4:
    primary -> blue, success -> green, danger -> red, None -> default.
Clients older than Feb 2026 simply show the default color.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Optional

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

import config

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"

LINE = "━━━━━━━━━━━━━━━━━━"


def btn(text: str, data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, style=style)


def blue(text: str, data: str) -> InlineKeyboardButton:
    return btn(text, data, PRIMARY)


def green(text: str, data: str) -> InlineKeyboardButton:
    return btn(text, data, SUCCESS)


def red(text: str, data: str) -> InlineKeyboardButton:
    return btn(text, data, DANGER)


def url_btn(text: str, url: str, style: Optional[str] = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, url=url, style=style)


def copy_btn(text: str, value: str) -> InlineKeyboardButton:
    """Button that copies `value` to the clipboard when tapped."""
    return InlineKeyboardButton(text, copy_text=CopyTextButton(value[:256]))


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def back(data: str = "home", text: str = "‹ Back") -> list[InlineKeyboardButton]:
    return [btn(text, data)]


def back_home(back_data: str) -> list[InlineKeyboardButton]:
    return [btn("‹ Back", back_data), btn("🏠 Home", "home")]


def pager(prefix: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    """◀️ 2/5 ▶️ navigation row. `prefix` gets ':<page>' appended."""
    if total_pages <= 1:
        return []
    row = []
    row.append(btn("◀️", f"{prefix}:{page - 1}") if page > 0 else btn("•", "noop"))
    row.append(btn(f"{page + 1} / {total_pages}", "noop"))
    row.append(btn("▶️", f"{prefix}:{page + 1}") if page < total_pages - 1 else btn("•", "noop"))
    return row


# ------------------------------------------------------------------ text
def e(value) -> str:
    """HTML-escape any value."""
    return html.escape(str(value if value is not None else ""))


def money(amount: float) -> str:
    amount = float(amount or 0)
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return f"{config.CURRENCY}{text}"


def local_time(utc_text: str) -> str:
    try:
        dt = datetime.strptime(utc_text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return e(utc_text)
    dt += timedelta(minutes=config.TZ_OFFSET_MINUTES)
    return dt.strftime("%d %b %Y, %I:%M %p")


def stock_bar(stock: int, full: int = 20, width: int = 8) -> str:
    filled = min(width, round(width * stock / full)) if stock else 0
    if stock and not filled:
        filled = 1
    return "▰" * filled + "▱" * (width - filled)


def stock_label(stock: int) -> str:
    if stock == 0:
        return "🔴 Out of stock"
    if stock < 5:
        return f"🟠 Only {stock} left"
    return f"🟢 {stock} available"


def card(title: str, *lines: str, footer: Optional[str] = None) -> str:
    """Standard message layout used everywhere in the bot."""
    body = "\n".join(line for line in lines if line is not None)
    text = f"<b>{title}</b>\n{LINE}\n{body}"
    if footer:
        text += f"\n{LINE}\n<i>{footer}</i>"
    return text


def row(label: str, value: str) -> str:
    return f"▸ {label}: <b>{value}</b>"

