"""Admin panel. Everything is driven by buttons; no commands to memorise.

Callback data format: "adm" or "adm:<action>[:<arg>...]".
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import tempfile
from datetime import datetime

from telegram import InputFile, Update
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from core import Ctx, clear_state, is_admin, on_input, send, set_state, show, user_link
from database import db
from handlers.deposit import admin_deposit_keyboard, admin_deposit_text
from ui.theme import (
    back, back_home, blue, btn, card, e, green, kb, local_time, money, pager, red, row,
    stock_bar,
)

log = logging.getLogger(__name__)

STOCK_PER_PAGE = 10
PHONE_RE = re.compile(r"^\+?\d{7,15}$")
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

SETTINGS = {
    "store_name": ("🏷 Store name", "text"),
    "upi_id": ("🏦 UPI ID", "text"),
    "upi_name": ("👤 UPI name", "text"),
    "support": ("📞 Support contact", "text"),
    "min_deposit": ("⬇️ Minimum deposit", "number"),
    "ref_percent": ("🎁 Referral %", "number"),
    "otp_hours": ("⏱ OTP window (hours)", "number"),
}


# ============================================================ dashboard
async def dashboard(update: Update, context: Ctx) -> None:
    clear_state(context)
    s = await db.stats()
    maintenance = await db.get_setting("maintenance") == "1"
    text = card(
        "⚙️ Admin Dashboard",
        "<b>💰 Sales</b>",
        row("Today", f"{money(s['today'][0])} • {s['today'][1]} orders"),
        row("7 days", f"{money(s['week'][0])} • {s['week'][1]} orders"),
        row("All time", f"{money(s['all'][0])} • {s['all'][1]} orders"),
        "",
        "<b>📊 Store</b>",
        row("Users", f"{s['users']} (+{s['users_today']} today)"),
        row("Stock / Sold", f"{s['stock']} / {s['sold']}"),
        row("Deposits approved", money(s["deposits"])),
        row("User balances", money(s["balances"])),
        row("Pending deposits", str(s["pending"])),
        "",
        f"▸ Mode: {'🔴 <b>Maintenance</b>' if maintenance else '🟢 <b>Live</b>'}",
    )
    pending = f" ({s['pending']})" if s["pending"] else ""
    await show(update, text, kb(
        [blue("🌍 Countries & Stock", "adm:cs")],
        [green(f"💳 Deposits{pending}", "adm:deps"), btn("🧾 Orders", "adm:orders")],
        [btn("👤 Users", "adm:users"), btn("📢 Broadcast", "adm:bc")],
        [btn("🎟 Coupons", "adm:cp"), btn("🛠 Settings", "adm:set")],
        [btn("💾 Backup", "adm:backup"), btn("🔄 Refresh", "adm")],
        [btn("🏠 Exit to Store", "home")],
    ))


async def admin_cmd(update: Update, context: Ctx) -> None:
    if is_admin(update.effective_user.id):
        await dashboard(update, context)


# ============================================================ countries
async def countries_screen(update: Update, context: Ctx) -> None:
    countries = await db.countries(only_active=False)
    rows = []
    for c in countries:
        hidden = " 🙈" if not c["active"] else ""
        rows.append([btn(f"{c['flag']} {c['name']} • {money(c['price'])} • 📦 {c['stock']}{hidden}",
                         f"adm:c:{c['id']}")])
    text = card("🌍 Countries & Stock",
                "Tap a country to manage its price and stock." if countries
                else "No countries yet. Add your first one.",
                footer="🙈 = hidden from customers")
    await show(update, text, kb(*rows, [green("➕ Add Country", "adm:cadd")], back("adm")))


async def country_screen(update: Update, context: Ctx, cid: int) -> None:
    c = await db.get_country(cid)
    if not c:
        await countries_screen(update, context)
        return
    text = card(
        f"{c['flag']} {e(c['name'])} ({e(c['code'])})",
        row("💵 Price", money(c["price"])),
        row("📦 Stock", f"{stock_bar(c['stock'])} {c['stock']}"),
        row("👁 Visible", "Yes" if c["active"] else "No"),
    )
    await show(update, text, kb(
        [green("➕ Add Stock", f"adm:sadd:{cid}"), blue("📦 View Stock", f"adm:st:{cid}:0")],
        [btn("💵 Change Price", f"adm:cprice:{cid}"), btn("✏️ Edit Name/Flag", f"adm:cname:{cid}")],
        [btn("🙈 Hide" if c["active"] else "👁 Show", f"adm:ctog:{cid}"),
         red("🗑 Delete", f"adm:cdel:{cid}")],
        back_home("adm:cs"),
    ))


@on_input("cadd", admin=True)
async def cadd_input(update: Update, context: Ctx, state: dict) -> None:
    parts = [p.strip() for p in update.message.text.split("|")]
    try:
        name, code, flag, price = parts
        price = float(price)
        assert name and code and price > 0
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ Format: <code>Name | CODE | Flag | Price</code>\n"
            "Example: <code>India | IN | 🇮🇳 | 45</code>")
        return
    clear_state(context)
    cid = await db.add_country(name, code, flag or "🌐", price)
    await update.message.reply_text(f"✅ {e(flag)} <b>{e(name)}</b> added at {money(price)}.",
                                    reply_markup=kb([green("➕ Add Stock", f"adm:sadd:{cid}")],
                                                    [btn("‹ Countries", "adm:cs")]))


@on_input("cprice", admin=True)
async def cprice_input(update: Update, context: Ctx, state: dict) -> None:
    try:
        price = round(float(update.message.text.strip()), 2)
        assert price > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Send a positive number.")
        return
    clear_state(context)
    await db.update_country(state["cid"], "price", price)
    await update.message.reply_text(f"✅ Price updated to {money(price)}.",
                                    reply_markup=kb([btn("‹ Back", f"adm:c:{state['cid']}")]))


@on_input("cname", admin=True)
async def cname_input(update: Update, context: Ctx, state: dict) -> None:
    parts = [p.strip() for p in update.message.text.split("|")]
    if not parts[0]:
        await update.message.reply_text("❌ Format: <code>Name | Flag</code>")
        return
    clear_state(context)
    await db.update_country(state["cid"], "name", parts[0])
    if len(parts) > 1 and parts[1]:
        await db.update_country(state["cid"], "flag", parts[1])
    await update.message.reply_text("✅ Country updated.",
                                    reply_markup=kb([btn("‹ Back", f"adm:c:{state['cid']}")]))


# ================================================================ stock
def parse_stock(raw: str) -> tuple[list[tuple[str, str | None, str | None]], list[str]]:
    """Parse lines of `phone | 2fa | session`. Returns (items, bad_lines)."""
    items, bad = [], []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        phone = parts[0].replace(" ", "").replace("-", "")
        if not PHONE_RE.match(phone):
            bad.append(line[:40])
            continue
        if not phone.startswith("+"):
            phone = "+" + phone
        twofa = parts[1] if len(parts) > 1 and parts[1] not in ("", "-") else None
        session = parts[2] if len(parts) > 2 and parts[2] not in ("", "-") else None
        items.append((phone, twofa, session))
    return items, bad


@on_input("sadd", any_message=True, admin=True)
async def stock_input(update: Update, context: Ctx, state: dict) -> None:
    msg = update.message
    if msg.document:
        if msg.document.file_size and msg.document.file_size > MAX_UPLOAD_BYTES:
            await msg.reply_text("❌ File is too large (max 5 MB).")
            return
        file = await msg.document.get_file()
        raw = (await file.download_as_bytearray()).decode("utf-8", errors="ignore")
    elif msg.text:
        raw = msg.text
    else:
        await msg.reply_text("❌ Send the accounts as text or a .txt file.")
        return

    items, bad = parse_stock(raw)
    if not items:
        await msg.reply_text("❌ No valid lines found. Format: <code>+phone | 2fa | session</code>")
        return
    clear_state(context)
    added, dupes = await db.add_accounts(state["cid"], items)
    lines = [row("✅ Added", str(added)), row("♻️ Duplicates skipped", str(dupes))]
    if bad:
        lines.append(row("❌ Invalid lines", str(len(bad))))
        lines += [f"   • <code>{e(b)}</code>" for b in bad[:5]]
    await msg.reply_text(card("📦 Stock Updated", *lines),
                         reply_markup=kb([green("➕ Add More", f"adm:sadd:{state['cid']}"),
                                          btn("‹ Back", f"adm:c:{state['cid']}")]))


async def stock_screen(update: Update, context: Ctx, cid: int, page: int) -> None:
    c = await db.get_country(cid)
    if not c:
        return
    pages = max(1, math.ceil(c["stock"] / STOCK_PER_PAGE))
    page = max(0, min(page, pages - 1))
    accounts = await db.stock_page(cid, page * STOCK_PER_PAGE, STOCK_PER_PAGE)
    rows = [[btn(f"#{a['id']} • {a['phone']}{' • 🔑' if a['session'] else ''}", f"adm:a:{a['id']}")]
            for a in accounts]
    text = card(f"📦 Stock • {c['flag']} {e(c['name'])}",
                row("Available", str(c["stock"])),
                "Tap an account to view or delete it.",
                footer="🔑 = has session (auto OTP)")
    await show(update, text, kb(*rows, pager(f"adm:st:{cid}", page, pages),
                                back_home(f"adm:c:{cid}")))


async def account_screen(update: Update, context: Ctx, account_id: int) -> None:
    a = await db.get_account(account_id)
    if not a:
        await update.callback_query.answer("Account not found.", show_alert=True)
        return
    text = card(f"📱 Account #{a['id']}",
                f"▸ Phone: <code>{e(a['phone'])}</code>",
                f"▸ 2FA: <code>{e(a['twofa'] or '-')}</code>",
                row("Session", "Yes" if a["session"] else "No"),
                row("Status", a["status"]),
                row("Added", local_time(a["added_at"])))
    rows = []
    if a["status"] == "available":
        rows.append([red("🗑 Delete Account", f"adm:adel:{a['id']}")])
    rows.append(back(f"adm:st:{a['country_id']}:0"))
    await show(update, text, kb(*rows))


# ============================================================= deposits
async def deposits_screen(update: Update, context: Ctx) -> None:
    deps = await db.pending_deposits()
    rows = [[btn(f"#{d['id']} • {money(d['amount'])} • {d['first_name'] or d['user_id']}",
                 f"adm:dep:{d['id']}")] for d in deps]
    text = card("💳 Pending Deposits",
                f"{len(deps)} waiting for review." if deps else "✨ All caught up. Nothing pending.")
    await show(update, text, kb(*rows, back("adm")))


async def deposit_screen(update: Update, context: Ctx, deposit_id: int) -> None:
    d = await db.get_deposit(deposit_id)
    if not d or d["status"] != "pending":
        await update.callback_query.answer("Already handled.", show_alert=True)
        await deposits_screen(update, context)
        return
    u = await db.get_user(d["user_id"])
    markup = admin_deposit_keyboard(deposit_id)
    await show(update, admin_deposit_text(d, u["id"], u["username"], u["first_name"]),
               kb(*markup.inline_keyboard, back("adm:deps")))


async def orders_screen(update: Update, context: Ctx) -> None:
    orders = await db.recent_orders(15)
    lines = [f"<b>#{o['id']}</b> {o['country_flag']} <code>{e(o['phone'])}</code> • {money(o['price'])}\n"
             f"     👤 {e(o['first_name'])} <code>{o['user_id']}</code> • {local_time(o['created_at'])}"
             for o in orders] or ["No orders yet."]
    await show(update, card("🧾 Recent Orders", *lines, footer="Last 15 orders"),
               kb([btn("🔄 Refresh", "adm:orders")], back("adm")))


# ================================================================ users
async def user_screen(update: Update, context: Ctx, user_id: int) -> None:
    u = await db.get_user(user_id)
    if not u:
        await show(update, "❌ User not found.", kb(back("adm")))
        return
    orders = await db.count_user_orders(user_id)
    refs = await db.referral_count(user_id)
    text = card(
        "👤 User Profile",
        f"▸ {user_link(u['id'], u['username'], u['first_name'])}",
        row("💰 Balance", money(u["balance"])),
        row("📥 Deposited", money(u["total_deposit"])),
        row("🛍 Spent", money(u["total_spent"])),
        row("📦 Orders", str(orders)),
        row("👥 Referrals", str(refs)),
        row("📅 Joined", local_time(u["joined_at"])),
        row("Status", "🚫 Banned" if u["banned"] else "✅ Active"),
    )
    await show(update, text, kb(
        [green("➕ Add Balance", f"adm:ubal:{user_id}:add"),
         red("➖ Deduct", f"adm:ubal:{user_id}:sub")],
        [btn("✅ Unban" if u["banned"] else "🚫 Ban", f"adm:uban:{user_id}"),
         btn("✉️ Message", f"adm:umsg:{user_id}")],
        [btn("🔍 Find another", "adm:users"), btn("‹ Dashboard", "adm")],
    ))


@on_input("ufind", admin=True)
async def ufind_input(update: Update, context: Ctx, state: dict) -> None:
    u = await db.find_user(update.message.text)
    if not u:
        await update.message.reply_text("❌ No user found. Send a user ID or @username.")
        return
    clear_state(context)
    await user_screen(update, context, u["id"])


@on_input("ubal", admin=True)
async def ubal_input(update: Update, context: Ctx, state: dict) -> None:
    try:
        amount = round(float(update.message.text.strip()), 2)
        assert amount > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Send a positive number.")
        return
    clear_state(context)
    signed = amount if state["mode"] == "add" else -amount
    try:
        balance = await db.admin_adjust_balance(state["uid"], signed, update.effective_user.id)
    except Exception as exc:  # PurchaseError for insufficient balance
        await update.message.reply_text(f"❌ {e(exc)}")
        return
    verb = "added to" if signed > 0 else "deducted from"
    await send(context, state["uid"],
               f"🛠 {money(amount)} was {verb} your wallet by an admin.\n"
               f"💰 New balance: <b>{money(balance)}</b>")
    await update.message.reply_text(f"✅ Done. New balance: <b>{money(balance)}</b>",
                                    reply_markup=kb([btn("‹ Profile", f"adm:u:{state['uid']}")]))


@on_input("umsg", any_message=True, admin=True)
async def umsg_input(update: Update, context: Ctx, state: dict) -> None:
    clear_state(context)
    try:
        await update.message.copy(state["uid"])
        result = "✅ Message delivered."
    except TelegramError as exc:
        result = f"❌ Could not deliver: {e(exc)}"
    await update.message.reply_text(result, reply_markup=kb([btn("‹ Profile", f"adm:u:{state['uid']}")]))


# ============================================================ broadcast
@on_input("bc", any_message=True, admin=True)
async def broadcast_input(update: Update, context: Ctx, state: dict) -> None:
    clear_state(context)
    context.user_data["bc"] = (update.effective_chat.id, update.message.message_id)
    total = len(await db.all_user_ids())
    await update.message.reply_text(
        card("📢 Confirm Broadcast", f"The message above will be sent to <b>{total}</b> users."),
        reply_markup=kb([green("🚀 Send Now", "adm:bcgo"), red("✖ Cancel", "adm")]),
    )


async def run_broadcast(context: Ctx, admin_chat: int, from_chat: int, message_id: int) -> None:
    ids = await db.all_user_ids()
    ok = failed = 0
    for i, uid in enumerate(ids, 1):
        try:
            await context.bot.copy_message(uid, from_chat, message_id)
            ok += 1
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await context.bot.copy_message(uid, from_chat, message_id)
                ok += 1
            except TelegramError:
                failed += 1
        except (Forbidden, TelegramError):
            failed += 1
        if i % 25 == 0:
            await asyncio.sleep(1)  # stay under Telegram's ~30 msg/s limit
    await send(context, admin_chat, card("📢 Broadcast Finished",
                                         row("✅ Delivered", str(ok)),
                                         row("❌ Failed", str(failed))))


# ============================================================== coupons
async def coupons_screen(update: Update, context: Ctx) -> None:
    coupons = await db.coupons()
    rows = [[btn(f"🎟 {c['code']} • {money(c['amount'])} • {c['used']}/{c['max_uses']}",
                 f"adm:cpx:{c['code']}")] for c in coupons]
    text = card("🎟 Gift Codes", "Users redeem these for wallet balance." if coupons
                else "No codes yet.", footer="Tap a code to delete it.")
    await show(update, text, kb(*rows, [green("➕ Create Code", "adm:cpadd")], back("adm")))


@on_input("cpadd", admin=True)
async def coupon_input(update: Update, context: Ctx, state: dict) -> None:
    parts = [p.strip() for p in update.message.text.split("|")]
    try:
        code, amount, uses = parts
        amount, uses = float(amount), int(uses)
        assert re.fullmatch(r"[A-Za-z0-9_-]{3,32}", code) and amount > 0 and uses > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Format: <code>CODE | amount | max uses</code>\n"
                                        "Example: <code>WELCOME10 | 10 | 100</code>")
        return
    clear_state(context)
    if not await db.create_coupon(code, amount, uses):
        await update.message.reply_text("⚠️ A code with that name already exists.")
        return
    await update.message.reply_text(
        f"✅ Code <code>{e(code.upper())}</code> created: {money(amount)} × {uses} uses.",
        reply_markup=kb([btn("‹ Coupons", "adm:cp")]))


# ============================================================= settings
async def settings_screen(update: Update, context: Ctx) -> None:
    lines, rows = [], []
    for key, (label, _) in SETTINGS.items():
        value = await db.get_setting(key)
        lines.append(f"{label}: <b>{e(value) or '—'}</b>")
        rows.append([btn(f"✏️ {label}", f"adm:sv:{key}")])
    maintenance = await db.get_setting("maintenance") == "1"
    rows.append([green("🟢 Go Live", "adm:maint") if maintenance
                 else red("🔴 Maintenance Mode", "adm:maint")])
    await show(update, card("🛠 Settings", *lines), kb(*rows, back("adm")))


@on_input("sv", admin=True)
async def setting_input(update: Update, context: Ctx, state: dict) -> None:
    key = state["key"]
    value = update.message.text.strip()
    if SETTINGS[key][1] == "number":
        try:
            assert float(value) >= 0
        except (ValueError, AssertionError):
            await update.message.reply_text("❌ Send a number.")
            return
    clear_state(context)
    await db.set_setting(key, value)
    await update.message.reply_text(f"✅ {SETTINGS[key][0]} updated.",
                                    reply_markup=kb([btn("‹ Settings", "adm:set")]))


async def send_backup(update: Update, context: Ctx) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"store-backup-{stamp}.db")
        await db.backup(path)
        with open(path, "rb") as fh:
            await update.effective_chat.send_document(
                InputFile(fh, filename=os.path.basename(path)),
                caption="💾 Database backup. Keep it private: it contains account details.")


# =============================================================== router
PROMPTS = {
    "cadd": "➕ Send the new country as:\n<code>Name | CODE | Flag | Price</code>\n\n"
            "Example: <code>India | IN | 🇮🇳 | 45</code>",
    "cprice": "💵 Send the new price per account.",
    "cname": "✏️ Send <code>Name | Flag</code> (flag optional).",
    "sadd": "📦 Send accounts, one per line, or upload a <b>.txt</b> file:\n"
            "<code>+phone | 2fa | session</code>\n\n"
            "Use <code>-</code> for empty fields. The session (Telethon StringSession) is "
            "optional and enables auto OTP.\n\n"
            "Example:\n<code>+919876543210 | mypass | -\n+14155550123 | - | 1BVts...</code>",
    "ufind": "🔍 Send a user ID or @username.",
    "umsg": "✉️ Send the message for this user (text, photo, anything).",
    "bc": "📢 Send the message to broadcast (text, photo, video, anything).\n"
          "You'll get a preview to confirm.",
    "cpadd": "🎟 Send:\n<code>CODE | amount | max uses</code>\n\n"
             "Example: <code>WELCOME10 | 10 | 100</code>",
}


async def ask(update: Update, context: Ctx, name: str, cancel_to: str, text: str | None = None,
              **data) -> None:
    set_state(context, name, **data)
    await show(update, card("✍️ Input Needed", text or PROMPTS[name]),
               kb([red("✖ Cancel", cancel_to)]))


async def admin_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    args = parts[2:]
    clear_state(context)

    if action in ("adel", "cdelok", "cpx", "maint", "ctog", "uban", "bcgo", "backup"):
        pass  # these answer the query themselves below
    else:
        await query.answer()

    if action == "":
        await dashboard(update, context)
    elif action == "cs":
        await countries_screen(update, context)
    elif action == "cadd":
        await ask(update, context, "cadd", "adm:cs")
    elif action == "c":
        await country_screen(update, context, int(args[0]))
    elif action == "cprice":
        await ask(update, context, "cprice", f"adm:c:{args[0]}", cid=int(args[0]))
    elif action == "cname":
        await ask(update, context, "cname", f"adm:c:{args[0]}", cid=int(args[0]))
    elif action == "ctog":
        c = await db.get_country(int(args[0]))
        await db.update_country(c["id"], "active", 0 if c["active"] else 1)
        await query.answer("Hidden from store" if c["active"] else "Now visible")
        await country_screen(update, context, c["id"])
    elif action == "cdel":
        c = await db.get_country(int(args[0]))
        await show(update, card("🗑 Delete Country?",
                                f"Delete {c['flag']} <b>{e(c['name'])}</b>? This cannot be undone.",
                                "Countries that ever had stock can only be hidden."),
                   kb([red("🗑 Yes, Delete", f"adm:cdelok:{c['id']}"),
                       btn("✖ Cancel", f"adm:c:{c['id']}")]))
    elif action == "cdelok":
        if await db.delete_country(int(args[0])):
            await query.answer("Deleted")
            await countries_screen(update, context)
        else:
            await query.answer("This country has accounts. Hide it instead.", show_alert=True)
    elif action == "sadd":
        await ask(update, context, "sadd", f"adm:c:{args[0]}", cid=int(args[0]))
    elif action == "st":
        await stock_screen(update, context, int(args[0]), int(args[1]))
    elif action == "a":
        await account_screen(update, context, int(args[0]))
    elif action == "adel":
        a = await db.get_account(int(args[0]))
        if a and await db.delete_account(a["id"]):
            await query.answer("Account deleted")
            await stock_screen(update, context, a["country_id"], 0)
        else:
            await query.answer("Could not delete (already sold?)", show_alert=True)
    elif action == "deps":
        await deposits_screen(update, context)
    elif action == "dep":
        await deposit_screen(update, context, int(args[0]))
    elif action == "orders":
        await orders_screen(update, context)
    elif action == "users":
        await ask(update, context, "ufind", "adm")
    elif action == "u":
        await user_screen(update, context, int(args[0]))
    elif action == "ubal":
        uid, mode = int(args[0]), args[1]
        verb = "add to" if mode == "add" else "deduct from"
        await ask(update, context, "ubal", f"adm:u:{uid}",
                  f"💰 Send the amount to {verb} user <code>{uid}</code>.", uid=uid, mode=mode)
    elif action == "uban":
        u = await db.get_user(int(args[0]))
        await db.set_banned(u["id"], not u["banned"])
        await query.answer("User unbanned" if u["banned"] else "User banned")
        await user_screen(update, context, u["id"])
    elif action == "umsg":
        await ask(update, context, "umsg", f"adm:u:{args[0]}", uid=int(args[0]))
    elif action == "bc":
        await ask(update, context, "bc", "adm")
    elif action == "bcgo":
        saved = context.user_data.pop("bc", None)
        if not saved:
            await query.answer("Nothing to send.", show_alert=True)
            return
        await query.answer("🚀 Broadcast started")
        await show(update, "📢 Broadcast is running. You'll get a report when it finishes.",
                   kb(back("adm")))
        context.application.create_task(
            run_broadcast(context, update.effective_chat.id, saved[0], saved[1]))
    elif action == "cp":
        await coupons_screen(update, context)
    elif action == "cpadd":
        await ask(update, context, "cpadd", "adm:cp")
    elif action == "cpx":
        await db.delete_coupon(args[0])
        await query.answer("Code deleted")
        await coupons_screen(update, context)
    elif action == "set":
        await settings_screen(update, context)
    elif action == "sv":
        key = args[0]
        if key in SETTINGS:
            current = await db.get_setting(key)
            await ask(update, context, "sv", "adm:set",
                      f"{SETTINGS[key][0]}\nCurrent: <b>{e(current) or '—'}</b>\n\nSend the new value.",
                      key=key)
    elif action == "maint":
        on = await db.get_setting("maintenance") != "1"
        await db.set_setting("maintenance", "1" if on else "0")
        await query.answer("🔴 Maintenance ON" if on else "🟢 Store is live")
        await settings_screen(update, context)
    elif action == "backup":
        await query.answer("Preparing backup…")
        await send_backup(update, context)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^adm(:.*)?$"))
