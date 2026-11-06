"""Customer-facing screens: home, shop, purchase, orders, wallet, referral."""
from __future__ import annotations

import math
import time

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from core import Ctx, clear_state, is_admin, log_event, on_input, set_state, show, user_link
from database import PurchaseError, db
from services import otp
from ui.theme import (
    back, back_home, blue, btn, card, copy_btn, e, green, kb, local_time, money, pager, red, row,
    stock_bar, stock_label, url_btn,
)

COUNTRIES_PER_PAGE = 10
ORDERS_PER_PAGE = 8
MAX_QTY = 10
OTP_COOLDOWN_SEC = 15


# ----------------------------------------------------------------- home
async def home_screen(update: Update, context: Ctx) -> None:
    clear_state(context)
    user = update.effective_user
    data = await db.get_user(user.id)
    store = e(await db.get_setting("store_name"))
    orders = await db.count_user_orders(user.id)

    text = card(
        f"✨ {store}",
        f"👋 Welcome, <b>{e(user.first_name)}</b>!",
        "",
        row("💰 Balance", money(data["balance"])),
        row("📦 Orders", str(orders)),
        row("🆔 User ID", f"<code>{user.id}</code>"),
        footer="Fresh accounts • Instant delivery • 24/7",
    )
    rows = [
        [blue("🛒  Buy Accounts", "shop:0")],
        [green("💳 Deposit", "dep"), btn("👛 Wallet", "wallet")],
        [btn("📦 My Orders", "orders:0"), btn("🎁 Refer & Earn", "ref")],
        [btn("🎟 Redeem Code", "redeem"), btn("🆘 Help", "help")],
    ]
    if is_admin(user.id):
        rows.append([red("⚙️ Admin Panel", "adm")])
    await show(update, text, kb(*rows))


async def start_cmd(update: Update, context: Ctx) -> None:
    await home_screen(update, context)


async def home_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    await home_screen(update, context)


async def noop_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()


# ----------------------------------------------------------------- shop
async def shop_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    countries = await db.countries()
    if not countries:
        await show(update, card("🛒 Store", "No countries are listed yet. Please check back soon."),
                   kb(back()))
        return

    pages = math.ceil(len(countries) / COUNTRIES_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = countries[page * COUNTRIES_PER_PAGE:(page + 1) * COUNTRIES_PER_PAGE]
    total_stock = sum(c["stock"] for c in countries)

    buttons, pair = [], []
    for c in chunk:
        mark = "" if c["stock"] else " ✖"
        pair.append(btn(f"{c['flag']} {c['name']} • {money(c['price'])}{mark}",
                        f"c:{c['id']}:1", style="primary" if c["stock"] else None))
        if len(pair) == 2:
            buttons.append(pair)
            pair = []
    if pair:
        buttons.append(pair)

    text = card(
        "🛒 Choose a Country",
        row("🌍 Countries", str(len(countries))),
        row("📦 Total stock", str(total_stock)),
        "",
        "Tap a country to see price and stock.",
        footer="✖ = out of stock",
    )
    await show(update, text, kb(*buttons, pager("shop", page, pages), back()))


async def country_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    _, cid, qty = query.data.split(":")
    country = await db.get_country(int(cid))
    if not country or not country["active"]:
        await query.answer("This country is no longer available.", show_alert=True)
        return
    await query.answer()

    stock = country["stock"]
    qty = max(1, min(int(qty), MAX_QTY, stock or 1))
    user = await db.get_user(update.effective_user.id)
    total = country["price"] * qty

    text = card(
        f"{country['flag']} {e(country['name'])}",
        row("💵 Price", f"{money(country['price'])} / account"),
        row("📦 Stock", f"{stock_bar(stock)} {stock}"),
        f"▸ Status: {stock_label(stock)}",
        "",
        row("🔢 Quantity", str(qty)),
        row("🧾 Total", money(total)),
        row("💰 Your balance", money(user["balance"])),
        footer="Every account is delivered instantly after payment.",
    )
    rows = []
    if stock:
        rows.append([
            btn("➖", f"c:{cid}:{qty - 1}") if qty > 1 else btn("•", "noop"),
            btn(f"{qty}", "noop"),
            btn("➕", f"c:{cid}:{qty + 1}") if qty < min(stock, MAX_QTY) else btn("•", "noop"),
        ])
        if user["balance"] >= total:
            rows.append([green(f"⚡ Buy Now • {money(total)}", f"cf:{cid}:{qty}")])
        else:
            need = total - user["balance"]
            rows.append([blue(f"💳 Add {money(need)} to buy", "dep")])
    else:
        rows.append([btn("🔔 Out of stock. Check back later", "noop")])
    rows.append(back_home("shop:0"))
    await show(update, text, kb(*rows))


async def confirm_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    await query.answer()
    _, cid, qty = query.data.split(":")
    country = await db.get_country(int(cid))
    if not country:
        return
    qty = int(qty)
    text = card(
        "🧾 Confirm Purchase",
        row("🌍 Country", f"{country['flag']} {e(country['name'])}"),
        row("🔢 Quantity", str(qty)),
        row("💵 Total", money(country["price"] * qty)),
        "",
        "The amount will be deducted from your wallet.",
    )
    await show(update, text, kb(
        [green("✅ Confirm & Pay", f"pay:{cid}:{qty}"), red("✖ Cancel", f"c:{cid}:{qty}")],
    ))


async def pay_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    _, cid, qty = query.data.split(":")
    user = update.effective_user
    qty = max(1, min(int(qty), MAX_QTY))
    try:
        orders = await db.purchase(user.id, int(cid), qty)
    except PurchaseError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer("✅ Purchase successful!")

    total = sum(o["price"] for o in orders)
    balance = (await db.get_user(user.id))["balance"]
    first = orders[0]
    summary = card(
        "✅ Purchase Successful",
        row("🌍 Country", f"{first['country_flag']} {e(first['country_name'])}"),
        row("🔢 Accounts", str(len(orders))),
        row("💵 Paid", money(total)),
        row("💰 Balance left", money(balance)),
        "",
        "Your account details are below 👇" if len(orders) > 1 else None,
    )
    if len(orders) == 1:
        await show(update, delivery_text(first), delivery_keyboard(first))
    else:
        await show(update, summary, kb([btn("📦 My Orders", "orders:0"), btn("🏠 Home", "home")]))
        for order in orders:
            await update.effective_chat.send_message(delivery_text(order),
                                                     reply_markup=delivery_keyboard(order))

    await log_event(context, card(
        "🛍 New Sale",
        f"▸ Buyer: {user_link(user.id, user.username, user.first_name)}",
        row("Country", f"{first['country_flag']} {e(first['country_name'])}"),
        row("Qty", str(len(orders))),
        row("Total", money(total)),
        row("Orders", ", ".join(f"#{o['id']}" for o in orders)),
    ))


def delivery_text(order: dict) -> str:
    otp_line = ("2️⃣ Tap <b>📲 Get OTP</b> below to receive the login code."
                if order.get("session") and otp.available()
                else "2️⃣ Contact support with your order ID to receive the login code.")
    return card(
        f"🎉 Order #{order['id']} Delivered",
        row("🌍 Country", f"{order['country_flag']} {e(order['country_name'])}"),
        f"▸ 📱 Phone: <code>{e(order['phone'])}</code>",
        f"▸ 🔐 2FA: <code>{e(order['twofa'] or 'Not set')}</code>",
        row("💵 Price", money(order["price"])),
        row("🕒 Date", local_time(order["created_at"])),
        "",
        "<b>How to log in</b>",
        "1️⃣ Open Telegram ➜ Add Account ➜ enter the phone number.",
        otp_line,
        "3️⃣ Enter the 2FA password if asked.",
        footer="Tap the phone or 2FA to copy it.",
    )


def delivery_keyboard(order: dict, back_label: str = "📦 My Orders"):
    copy_row = [copy_btn("📋 Copy Phone", order["phone"])]
    if order["twofa"]:
        copy_row.append(copy_btn("📋 Copy 2FA", order["twofa"]))
    return kb(
        copy_row,
        [blue("📲 Get OTP", f"otp:{order['id']}")],
        [btn(back_label, "orders:0"), btn("🏠 Home", "home")],
    )


# --------------------------------------------------------------- orders
async def orders_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    page = int(query.data.split(":")[1])
    total = await db.count_user_orders(user_id)
    if not total:
        await show(update, card("📦 My Orders", "You haven't bought anything yet."),
                   kb([blue("🛒 Start Shopping", "shop:0")], back()))
        return
    pages = math.ceil(total / ORDERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    orders = await db.user_orders(user_id, page * ORDERS_PER_PAGE, ORDERS_PER_PAGE)
    buttons = [[btn(f"#{o['id']} • {o['country_flag']} {o['phone']} • {money(o['price'])}",
                    f"o:{o['id']}")] for o in orders]
    text = card("📦 My Orders", row("Total orders", str(total)), "",
                "Tap an order to view its details.")
    await show(update, text, kb(*buttons, pager("orders", page, pages), back()))


async def order_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    order = await db.get_order(int(query.data.split(":")[1]))
    if not order or order["user_id"] != update.effective_user.id:
        await query.answer("Order not found.", show_alert=True)
        return
    await query.answer()
    await show(update, delivery_text(order), delivery_keyboard(order, back_label="‹ Back"))


async def otp_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    order = await db.get_order(int(query.data.split(":")[1]))
    if not order or order["user_id"] != update.effective_user.id:
        await query.answer("Order not found.", show_alert=True)
        return

    support = await db.get_setting("support")
    if not order["session"] or not otp.available():
        await query.answer(f"Auto OTP is not available for this account. "
                           f"Contact {support or 'support'} with order #{order['id']}.",
                           show_alert=True)
        return

    hours = int(await db.get_setting("otp_hours") or 24)
    if order["created_at"] < _hours_ago(hours):
        await query.answer(f"The OTP window ({hours}h) for this order has ended.", show_alert=True)
        return

    key = f"otp_last_{order['id']}"
    wait = OTP_COOLDOWN_SEC - (time.monotonic() - context.user_data.get(key, 0))
    if wait > 0:
        await query.answer(f"Please wait {int(wait) + 1}s before trying again.")
        return
    context.user_data[key] = time.monotonic()

    await query.answer("⏳ Checking for the code…")
    code, status = await otp.fetch_code(order["session"])
    if status == "ok":
        await update.effective_chat.send_message(
            card("📲 Login Code", f"▸ Order: <b>#{order['id']}</b>",
                 f"▸ Phone: <code>{e(order['phone'])}</code>",
                 f"▸ Code: <code>{code}</code>",
                 footer="Never share this code with anyone."),
            reply_markup=kb([copy_btn("📋 Copy Code", code)]),
        )
    elif status == "none":
        await query.message.reply_text(
            "⌛ No code yet. Request the code in Telegram first, wait a few seconds, "
            "then tap <b>📲 Get OTP</b> again.")
    else:
        await query.message.reply_text(
            f"⚠️ Could not read the code automatically. Contact {e(support or 'support')} "
            f"with order <b>#{order['id']}</b>.")


def _hours_ago(hours: int) -> str:
    from database import ago
    return ago(hours=hours)


# --------------------------------------------------------------- wallet
async def wallet_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    user = await db.get_user(update.effective_user.id)
    text = card(
        "👛 My Wallet",
        f"💰 <b>{money(user['balance'])}</b>",
        "",
        row("📥 Total deposited", money(user["total_deposit"])),
        row("🛍 Total spent", money(user["total_spent"])),
        row("🎁 Referral earnings", money(user["ref_earned"])),
    )
    await show(update, text, kb(
        [green("💳 Add Funds", "dep"), btn("📜 History", "txs")],
        back(),
    ))


TX_ICONS = {"deposit": "📥", "purchase": "🛍", "referral": "🎁", "coupon": "🎟", "admin": "🛠"}


async def txs_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    txs = await db.transactions(update.effective_user.id, 15)
    if not txs:
        lines = ["No transactions yet."]
    else:
        lines = [
            f"{TX_ICONS.get(t['type'], '•')} <b>{'+' if t['amount'] > 0 else '−'}"
            f"{money(abs(t['amount']))}</b> {e(t['note'] or t['type'])}\n"
            f"     <i>{local_time(t['created_at'])}</i>"
            for t in txs
        ]
    await show(update, card("📜 Transaction History", *lines, footer="Last 15 transactions"),
               kb(back("wallet")))


# ------------------------------------------------------------- referral
async def ref_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    count = await db.referral_count(user_id)
    percent = await db.get_setting("ref_percent")
    link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    text = card(
        "🎁 Refer & Earn",
        f"Earn <b>{e(percent)}%</b> of every deposit your friends make, forever.",
        "",
        row("👥 Friends invited", str(count)),
        row("💰 Earned", money(user["ref_earned"])),
        "",
        f"🔗 Your link:\n<code>{link}</code>",
    )
    share = f"https://t.me/share/url?url={link}"
    await show(update, text, kb(
        [url_btn("📤 Share Link", share, "primary"), copy_btn("📋 Copy Link", link)],
        back(),
    ))


# --------------------------------------------------------------- redeem
async def redeem_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    set_state(context, "redeem")
    await show(update, card("🎟 Redeem Code", "Send your gift code as a message."),
               kb([red("✖ Cancel", "home")]))


@on_input("redeem")
async def redeem_input(update: Update, context: Ctx, state: dict) -> None:
    clear_state(context)
    status, amount = await db.redeem_coupon(update.effective_user.id, update.message.text)
    messages = {
        "ok": f"🎉 Code redeemed! <b>{money(amount)}</b> was added to your wallet.",
        "invalid": "❌ This code is not valid.",
        "used": "⚠️ You have already used this code.",
        "exhausted": "⚠️ This code has reached its usage limit.",
    }
    await update.message.reply_text(messages[status],
                                    reply_markup=kb([btn("👛 Wallet", "wallet"), btn("🏠 Home", "home")]))


# ----------------------------------------------------------------- help
async def help_cb(update: Update, context: Ctx) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    support = await db.get_setting("support")
    text = card(
        "🆘 Help Center",
        "<b>How to buy</b>",
        "1️⃣ Add funds with 💳 <b>Deposit</b> (UPI).",
        "2️⃣ Open 🛒 <b>Buy Accounts</b> and pick a country.",
        "3️⃣ Confirm. The account is delivered instantly.",
        "4️⃣ Log in and tap 📲 <b>Get OTP</b> for the code.",
        "",
        "<b>Deposits</b>",
        "Pay the exact amount by UPI, then send the 12-digit UTR. "
        "Your wallet is credited once an admin verifies it.",
        "",
        row("📞 Support", e(support or "Not set")),
    )
    rows = []
    if support and support.startswith("@"):
        rows.append([url_btn("💬 Contact Support", f"https://t.me/{support[1:]}", "primary")])
    rows.append(back())
    await show(update, text, kb(*rows))


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["start", "menu"], start_cmd))
    app.add_handler(CommandHandler("help", help_cb))
    app.add_handler(CallbackQueryHandler(home_cb, pattern=r"^home$"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(shop_cb, pattern=r"^shop:-?\d+$"))
    app.add_handler(CallbackQueryHandler(country_cb, pattern=r"^c:\d+:-?\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_cb, pattern=r"^cf:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(pay_cb, pattern=r"^pay:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(orders_cb, pattern=r"^orders:-?\d+$"))
    app.add_handler(CallbackQueryHandler(order_cb, pattern=r"^o:\d+$"))
    app.add_handler(CallbackQueryHandler(otp_cb, pattern=r"^otp:\d+$"))
    app.add_handler(CallbackQueryHandler(wallet_cb, pattern=r"^wallet$"))
    app.add_handler(CallbackQueryHandler(txs_cb, pattern=r"^txs$"))
    app.add_handler(CallbackQueryHandler(ref_cb, pattern=r"^ref$"))
    app.add_handler(CallbackQueryHandler(redeem_cb, pattern=r"^redeem$"))
    app.add_handler(CallbackQueryHandler(help_cb, pattern=r"^help$"))
