# 🛒 Telegram Account Store Bot

A Telegram bot for selling Telegram accounts, with colored buttons, UPI deposits, instant delivery, and a button-driven admin panel.

Built with **python-telegram-bot 22.8** (Bot API 10.0) and SQLite.

## ✨ Features

### 🎨 UI
- **Colored buttons** using Bot API 9.4 `style`: 🔵 primary, 🟢 success, 🔴 danger. Telegram apps older than Feb 2026 show them in the default color.
- Screens update in place (message edit) so the chat stays clean.
- Card-style layouts, stock bars (▰▰▱), page navigation (◀️ 1/5 ▶️).
- **Copy buttons** for phone, 2FA, UPI ID, OTP code and referral link.

### 👤 Customers
- 🛒 Buy by country, with a quantity selector (1–10).
- 💳 UPI deposit with an auto-generated **QR code**. The user submits a 12-digit UTR and an admin verifies it.
- 📦 Order history with full account details.
- 📲 **Get OTP**: reads the login code automatically (needs a session string + `API_ID`/`API_HASH`).
- 👛 Wallet with transaction history.
- 🎁 Refer & earn (a % of every referred user's deposit).
- 🎟 Gift codes.

### ⚙️ Admin panel (`/admin` or the button on the home screen)
- 📊 Dashboard: sales today / 7 days / all time, users, stock, pending deposits.
- 🌍 Add, edit, hide and delete countries; change prices.
- 📦 Bulk-add stock by pasting lines or uploading a `.txt` file. Duplicates and invalid lines are reported.
- 💳 Approve or reject deposits, or approve a different amount.
- 👤 Find a user by ID or @username: add/deduct balance, ban/unban, send a direct message.
- 📢 Broadcast any message (text, photo, video…) to all users.
- 🎟 Create and delete gift codes.
- 🛠 Settings (store name, UPI, support, minimum deposit, referral %, OTP window) and maintenance mode.
- 💾 Database backup sent as a file.

### 🔒 Safety
- Purchases run in a single locked database transaction, so the same account is never sold twice and the balance can't go negative.
- Each UTR can be used only once. Payment requests expire. Each deposit can be approved only once, even with several admins.
- Rate limiting, banned-user blocking, admin-only checks on every admin action.
- All secrets come from `.env`. Nothing is hard-coded.

## 🚀 Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # then edit BOT_TOKEN, ADMIN_IDS, UPI_ID, ...
python bot.py
```

Requires Python 3.10+.

## 📦 Adding stock

Admin Panel → 🌍 Countries & Stock → ➕ Add Country:
```
India | IN | 🇮🇳 | 45
```
Open the country → ➕ Add Stock and send one account per line (or a `.txt` file):
```
+919876543210 | 2fa_password | session_string
+14155550123 | - | -
```
Use `-` for empty fields. The **session** is a Telethon `StringSession`. When it is present and `API_ID`/`API_HASH` are set, the buyer's 📲 Get OTP button fetches the login code from Telegram (777000) automatically. Without a session, the buyer is asked to contact support.

## 🗂 Structure

```
bot.py               entry point, gate (ban / maintenance / rate limit), error handler
config.py            settings from .env
database.py          SQLite schema and all queries (atomic purchase, deposits, coupons, stats)
core.py              screen rendering, text-input states, admin notifications
ui/theme.py          colored button helpers, card layout, formatting
handlers/user.py     home, shop, purchase, orders, OTP, wallet, referral, gift codes, help
handlers/deposit.py  UPI QR, UTR, admin approve/reject
handlers/admin.py    admin panel
services/otp.py      optional Telethon OTP reader
```

## 💡 Tips
- Set `LOG_CHANNEL_ID` to get every sale and approved deposit posted to a private channel.
- Take a 💾 Backup regularly. The file contains account details, so keep it private.
