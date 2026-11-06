"""UPI deposits: amount -> QR code -> UTR -> admin verification."""
from __future__ import annotations

import io
import re
from urllib.parse import quote

import qrcode
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler

import config
from core import (
    Ctx, clear_state, is_admin, log_event, notify_admins, on_input, send, set_state, show,
    user_link,
)
from database import db
from ui.theme import back, btn, card, copy_btn, e, green, kb, money, red, row

PRESETS = [50, 100, 200, 500, 1000, 2000]
MAX_DEPOSIT = 100_000
UTR_RE = re.compile(r"^\d{12}$")


def upi_link(upi_id: str, name: str, amount: float, note: str) -> str:
    return (f"upi://pay?pa={quote(upi_id)}&pn={quote(name)}&am={amount:.2f}"
            f"&cu=INR&tn={quote(note)}")


def make_qr(data: str) -> bytes:
    qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


async def deposit_menu(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    clear_state(context)
    if not await db.get_setting("upi_id"):
        await show(update, card("💳 Deposit", "Deposits are not set up yet. Please contact support."),
                   kb(back()))
        return
    minimum = float(await db.get_setting("min_deposit") or 0)
    amounts = [a for a in PRESETS if a >= minimum] or [int(minimum)]
    rows, line = [], []
    for amount in amounts:
        line.append(btn(money(amount), f"dep:{amount}", style="primary"))
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([green("✏️ Custom Amount", "depc")])
    rows.append(back())
    text = card(
        "💳 Add Funds",
        "Choose how much you want to add to your wallet.",
        "",
        row("💠 Method", "UPI (GPay, PhonePe, Paytm…)"),
        row("⬇️ Minimum", money(minimum)),
        footer="Funds are credited after verification.",
    )
    await show(update, text, kb(*rows))


async def preset_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    await start_payment(update, context, float(update.callback_query.data.split(":")[1]))


async def custom_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    minimum = await db.get_setting("min_deposit")
    set_state(context, "dep_amount")
    await show(update, card("✏️ Custom Amount",
                            f"Send the amount you want to deposit (minimum {money(float(minimum))})."),
               kb([red("✖ Cancel", "dep")]))


@on_input("dep_amount")
async def custom_input(update: Update, context: Ctx, state: dict) -> None:
    try:
        amount = round(float(update.message.text.replace(",", "").strip()), 2)
    except ValueError:
        await update.message.reply_text("❌ Please send a number, for example <code>250</code>.")
        return
    clear_state(context)
    await start_payment(update, context, amount)


async def start_payment(update: Update, context: Ctx, amount: float) -> None:
    user_id = update.effective_user.id
    minimum = float(await db.get_setting("min_deposit") or 0)
    if amount < minimum or amount > MAX_DEPOSIT:
        await show(update, f"❌ Amount must be between {money(minimum)} and {money(MAX_DEPOSIT)}.",
                   kb([btn("‹ Back", "dep")]))
        return
    if await db.count_pending_deposits(user_id) >= config.MAX_PENDING_DEPOSITS:
        await show(update, "⏳ You already have deposits waiting for review. "
                           "Please wait until they are verified.", kb(back()))
        return

    upi_id = await db.get_setting("upi_id")
    upi_name = await db.get_setting("upi_name") or "Store"
    deposit_id = await db.create_deposit(user_id, amount)
    link = upi_link(upi_id, upi_name, amount, f"DEP{deposit_id}")

    text = card(
        f"🧾 Payment #{deposit_id}",
        row("💵 Amount", money(amount)),
        f"▸ 🏦 UPI ID: <code>{e(upi_id)}</code>",
        row("👤 Name", e(upi_name)),
        "",
        "1️⃣ Scan the QR code or pay to the UPI ID above.",
        "2️⃣ Pay the <b>exact</b> amount.",
        "3️⃣ Tap <b>✅ I've Paid</b> and send the 12-digit UTR.",
        footer=f"This request expires in {config.PAYMENT_TIMEOUT_MIN} minutes.",
    )
    await show(update, text, kb(
        [copy_btn("📋 Copy UPI ID", upi_id), copy_btn("📋 Copy Amount", f"{amount:g}")],
        [green("✅ I've Paid", f"paid:{deposit_id}")],
        [red("✖ Cancel", f"depx:{deposit_id}")],
    ), photo=make_qr(link))


async def paid_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    deposit_id = int(query.data.split(":")[1])
    dep = await db.get_deposit(deposit_id)
    if not dep or dep["user_id"] != update.effective_user.id or dep["status"] != "created":
        await query.answer("This payment request is no longer active.", show_alert=True)
        return
    await query.answer()
    set_state(context, "utr", deposit_id=deposit_id)
    await query.message.reply_text(
        card("🔢 Send UTR", f"Send the <b>12-digit UTR / reference number</b> for payment "
                           f"#{deposit_id} ({money(dep['amount'])}).",
             footer="Find it in your UPI app under transaction details."),
        reply_markup=kb([red("✖ Cancel", f"depx:{deposit_id}")]),
    )


@on_input("utr")
async def utr_input(update: Update, context: Ctx, state: dict) -> None:
    utr = re.sub(r"\s", "", update.message.text or "")
    if not UTR_RE.match(utr):
        await update.message.reply_text("❌ A UTR has exactly 12 digits. Please try again.")
        return
    user = update.effective_user
    deposit_id = state["deposit_id"]
    result = await db.submit_utr(deposit_id, user.id, utr)
    if result == "duplicate":
        await update.message.reply_text("⚠️ This UTR has already been submitted.")
        return
    clear_state(context)
    if result == "expired":
        await update.message.reply_text("⌛ This payment request expired. If you already paid, "
                                        "contact support with your UTR.",
                                        reply_markup=kb([btn("🏠 Home", "home")]))
        return
    if result != "ok":
        await update.message.reply_text("⚠️ This payment request is no longer active.",
                                        reply_markup=kb([btn("🏠 Home", "home")]))
        return

    dep = await db.get_deposit(deposit_id)
    await update.message.reply_text(
        card("⏳ Payment Under Review",
             row("🧾 Request", f"#{deposit_id}"),
             row("💵 Amount", money(dep["amount"])),
             f"▸ 🔢 UTR: <code>{utr}</code>",
             footer="You'll get a message as soon as it's verified."),
        reply_markup=kb([btn("🏠 Home", "home")]),
    )
    await notify_admins(context, admin_deposit_text(dep, user.id, user.username, user.first_name),
                        admin_deposit_keyboard(deposit_id))


def admin_deposit_text(dep: dict, user_id: int, username, first_name) -> str:
    return card(
        f"💳 Deposit #{dep['id']} • Pending",
        f"▸ User: {user_link(user_id, username, first_name)}",
        row("Amount", money(dep["amount"])),
        f"▸ UTR: <code>{e(dep['utr'])}</code>",
        footer="Check your UPI app before approving.",
    )


def admin_deposit_keyboard(deposit_id: int):
    return kb(
        [green("✅ Approve", f"da:{deposit_id}"), red("❌ Reject", f"dr:{deposit_id}")],
        [btn("✏️ Approve different amount", f"dm:{deposit_id}")],
    )


async def cancel_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    await query.answer("Payment cancelled.")
    clear_state(context)
    await db.cancel_deposit(int(query.data.split(":")[1]), update.effective_user.id)
    await show(update, card("✖ Payment Cancelled", "No money was taken from your wallet."),
               kb([btn("💳 New Deposit", "dep"), btn("🏠 Home", "home")]))


# ---------------------------------------------------------- admin side
async def resolve(update: Update, context: Ctx, deposit_id: int, approve: bool,
                  amount: float | None = None) -> None:
    admin = update.effective_user
    result = await db.resolve_deposit(deposit_id, approve, admin.id, amount)
    query = update.callback_query
    if result is None:
        if query:
            await query.answer("Already handled.", show_alert=True)
        else:
            await update.message.reply_text("⚠️ This deposit was already handled.")
        return

    dep = result["deposit"]
    user = await db.get_user(dep["user_id"])
    if approve:
        status = f"✅ Approved {money(result['credited'])} by {e(admin.first_name)}"
        await send(context, dep["user_id"], card(
            "✅ Deposit Approved",
            row("🧾 Request", f"#{deposit_id}"),
            row("💵 Credited", money(result["credited"])),
            row("💰 New balance", money(user["balance"])),
        ), kb([green("🛒 Shop Now", "shop:0")]))
        if result["ref_bonus"] and result["referrer_id"]:
            await send(context, result["referrer_id"],
                       f"🎁 You earned <b>{money(result['ref_bonus'])}</b> from a referral deposit!")
        await log_event(context, f"💳 Deposit #{deposit_id} approved: {money(result['credited'])} "
                                 f"for <code>{dep['user_id']}</code>")
    else:
        status = f"❌ Rejected by {e(admin.first_name)}"
        support = await db.get_setting("support")
        await send(context, dep["user_id"], card(
            "❌ Deposit Rejected",
            row("🧾 Request", f"#{deposit_id}"),
            f"We couldn't verify this payment. Contact {e(support or 'support')} if you think "
            f"this is a mistake.",
        ))

    text = admin_deposit_text(dep, user["id"], user["username"], user["first_name"])
    text = text.replace("• Pending", "• Done") + f"\n\n<b>{status}</b>"
    if query:
        await query.answer("Done")
        await show(update, text, kb([btn("💳 Pending Deposits", "adm:deps")]))
    else:
        await update.message.reply_text(text)


async def approve_cb(update: Update, context: Ctx) -> None:
    if not is_admin(update.effective_user.id):
        return
    await resolve(update, context, int(update.callback_query.data.split(":")[1]), True)


async def reject_cb(update: Update, context: Ctx) -> None:
    if not is_admin(update.effective_user.id):
        return
    await resolve(update, context, int(update.callback_query.data.split(":")[1]), False)


async def amount_cb(update: Update, context: Ctx) -> None:
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    deposit_id = int(query.data.split(":")[1])
    set_state(context, "dep_fix", deposit_id=deposit_id)
    await query.message.reply_text(f"✏️ Send the amount actually received for deposit #{deposit_id}.",
                                   reply_markup=kb([red("✖ Cancel", "adm")]))


@on_input("dep_fix", admin=True)
async def amount_input(update: Update, context: Ctx, state: dict) -> None:
    if not is_admin(update.effective_user.id):
        return
    try:
        amount = round(float(update.message.text.strip()), 2)
        assert amount > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Send a positive number.")
        return
    clear_state(context)
    await resolve(update, context, state["deposit_id"], True, amount)


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(deposit_menu, pattern=r"^dep$"))
    app.add_handler(CallbackQueryHandler(preset_cb, pattern=r"^dep:\d+$"))
    app.add_handler(CallbackQueryHandler(custom_cb, pattern=r"^depc$"))
    app.add_handler(CallbackQueryHandler(paid_cb, pattern=r"^paid:\d+$"))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern=r"^depx:\d+$"))
    app.add_handler(CallbackQueryHandler(approve_cb, pattern=r"^da:\d+$"))
    app.add_handler(CallbackQueryHandler(reject_cb, pattern=r"^dr:\d+$"))
    app.add_handler(CallbackQueryHandler(amount_cb, pattern=r"^dm:\d+$"))
