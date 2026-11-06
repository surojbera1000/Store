"""Async SQLite data layer.

A single shared connection is used. Every write that touches more than one
row runs inside `Database.tx()`, which holds an asyncio lock and an
IMMEDIATE transaction, so purchases and balance changes are atomic and the
same account can never be sold twice.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

import aiosqlite

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    balance       REAL    NOT NULL DEFAULT 0,
    total_deposit REAL    NOT NULL DEFAULT 0,
    total_spent   REAL    NOT NULL DEFAULT 0,
    referrer_id   INTEGER,
    ref_earned    REAL    NOT NULL DEFAULT 0,
    banned        INTEGER NOT NULL DEFAULT 0,
    joined_at     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS countries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT    NOT NULL,
    code    TEXT    NOT NULL,
    flag    TEXT    NOT NULL DEFAULT '🌐',
    price   REAL    NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id INTEGER NOT NULL REFERENCES countries(id),
    phone      TEXT    NOT NULL UNIQUE,
    twofa      TEXT,
    session    TEXT,
    status     TEXT    NOT NULL DEFAULT 'available',
    added_at   TEXT    NOT NULL,
    sold_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_accounts_stock ON accounts(country_id, status);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    country_id INTEGER NOT NULL,
    price      REAL    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE TABLE IF NOT EXISTS deposits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    amount     REAL    NOT NULL,
    utr        TEXT UNIQUE,
    status     TEXT    NOT NULL DEFAULT 'created',
    admin_id   INTEGER,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    type          TEXT    NOT NULL,
    amount        REAL    NOT NULL,
    balance_after REAL    NOT NULL,
    note          TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id);
CREATE TABLE IF NOT EXISTS coupons (
    code       TEXT PRIMARY KEY,
    amount     REAL    NOT NULL,
    max_uses   INTEGER NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS coupon_uses (
    code    TEXT    NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (code, user_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")


def local_midnight_utc() -> str:
    """UTC timestamp of the start of 'today' in the configured time zone."""
    offset = timedelta(minutes=config.TZ_OFFSET_MINUTES)
    local = datetime.now(timezone.utc) + offset
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - offset
    return start.strftime("%Y-%m-%d %H:%M:%S")


class PurchaseError(Exception):
    """Raised when a purchase cannot be completed. Message is user-facing."""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        for key, value in config.DEFAULT_SETTINGS.items():
            await self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    async def one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def scalar(self, sql: str, params: tuple = ()) -> Any:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def run(self, sql: str, params: tuple = ()) -> int:
        """Run a single write statement. Returns lastrowid."""
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            return cur.lastrowid

    # ------------------------------------------------------------- settings
    async def get_setting(self, key: str) -> str:
        value = await self.scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return value if value is not None else config.DEFAULT_SETTINGS.get(key, "")

    async def set_setting(self, key: str, value: str) -> None:
        await self.run(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---------------------------------------------------------------- users
    async def upsert_user(self, user_id: int, username: Optional[str], first_name: str,
                          referrer_id: Optional[int] = None) -> bool:
        """Create or refresh a user. Returns True when the user is new."""
        async with self.tx() as c:
            async with c.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)) as cur:
                exists = await cur.fetchone()
            if exists:
                await c.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE id = ?",
                    (username, first_name, user_id),
                )
                return False
            if referrer_id == user_id:
                referrer_id = None
            if referrer_id:
                async with c.execute("SELECT 1 FROM users WHERE id = ?", (referrer_id,)) as cur:
                    if not await cur.fetchone():
                        referrer_id = None
            await c.execute(
                "INSERT INTO users(id, username, first_name, referrer_id, joined_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, username, first_name, referrer_id, now()),
            )
            return True

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    async def find_user(self, query: str) -> Optional[dict]:
        query = query.strip().lstrip("@")
        if query.isdigit():
            return await self.get_user(int(query))
        return await self.one("SELECT * FROM users WHERE lower(username) = lower(?)", (query,))

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.run("UPDATE users SET banned = ? WHERE id = ?", (int(banned), user_id))

    async def all_user_ids(self) -> list[int]:
        rows = await self.all("SELECT id FROM users WHERE banned = 0")
        return [r["id"] for r in rows]

    async def referral_count(self, user_id: int) -> int:
        return await self.scalar("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))

    @staticmethod
    async def _change_balance(c: aiosqlite.Connection, user_id: int, amount: float,
                              tx_type: str, note: str) -> float:
        async with c.execute("SELECT balance FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise PurchaseError("User not found.")
        new_balance = round(row["balance"] + amount, 2)
        if new_balance < 0:
            raise PurchaseError("Insufficient balance.")
        await c.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))
        await c.execute(
            "INSERT INTO transactions(user_id, type, amount, balance_after, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, tx_type, round(amount, 2), new_balance, note, now()),
        )
        return new_balance

    async def admin_adjust_balance(self, user_id: int, amount: float, admin_id: int) -> float:
        async with self.tx() as c:
            return await self._change_balance(
                c, user_id, amount, "admin", f"Adjusted by admin {admin_id}"
            )

    async def transactions(self, user_id: int, limit: int = 10) -> list[dict]:
        return await self.all(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    # ------------------------------------------------------------ countries
    async def countries(self, only_active: bool = True) -> list[dict]:
        where = "WHERE c.active = 1" if only_active else ""
        return await self.all(
            f"""SELECT c.*, (SELECT COUNT(*) FROM accounts a
                    WHERE a.country_id = c.id AND a.status = 'available') AS stock
                FROM countries c {where}
                ORDER BY stock = 0, c.name"""
        )

    async def get_country(self, country_id: int) -> Optional[dict]:
        return await self.one(
            """SELECT c.*, (SELECT COUNT(*) FROM accounts a
                    WHERE a.country_id = c.id AND a.status = 'available') AS stock
               FROM countries c WHERE c.id = ?""",
            (country_id,),
        )

    async def add_country(self, name: str, code: str, flag: str, price: float) -> int:
        return await self.run(
            "INSERT INTO countries(name, code, flag, price) VALUES (?, ?, ?, ?)",
            (name, code.upper(), flag, round(price, 2)),
        )

    async def update_country(self, country_id: int, field: str, value: Any) -> None:
        if field not in {"name", "code", "flag", "price", "active"}:
            raise ValueError(field)
        await self.run(f"UPDATE countries SET {field} = ? WHERE id = ?", (value, country_id))

    async def delete_country(self, country_id: int) -> bool:
        """Delete a country only if it has never had any accounts."""
        async with self.tx() as c:
            async with c.execute(
                "SELECT COUNT(*) FROM accounts WHERE country_id = ?", (country_id,)
            ) as cur:
                if (await cur.fetchone())[0]:
                    return False
            await c.execute("DELETE FROM countries WHERE id = ?", (country_id,))
            return True

    # ------------------------------------------------------------- accounts
    async def add_accounts(self, country_id: int,
                           items: list[tuple[str, Optional[str], Optional[str]]]) -> tuple[int, int]:
        """Insert (phone, twofa, session) items. Returns (added, duplicates)."""
        added = dupes = 0
        async with self.tx() as c:
            for phone, twofa, session in items:
                cur = await c.execute(
                    "INSERT OR IGNORE INTO accounts(country_id, phone, twofa, session, added_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (country_id, phone, twofa, session, now()),
                )
                if cur.rowcount:
                    added += 1
                else:
                    dupes += 1
        return added, dupes

    async def stock_page(self, country_id: int, offset: int, limit: int) -> list[dict]:
        return await self.all(
            "SELECT * FROM accounts WHERE country_id = ? AND status = 'available' "
            "ORDER BY id LIMIT ? OFFSET ?",
            (country_id, limit, offset),
        )

    async def get_account(self, account_id: int) -> Optional[dict]:
        return await self.one("SELECT * FROM accounts WHERE id = ?", (account_id,))

    async def delete_account(self, account_id: int) -> bool:
        async with self._lock:
            cur = await self.conn.execute(
                "DELETE FROM accounts WHERE id = ? AND status = 'available'", (account_id,)
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------- purchase
    async def purchase(self, user_id: int, country_id: int, qty: int) -> list[dict]:
        """Atomically charge the user and reserve `qty` accounts.

        Returns the created orders joined with account details.
        Raises PurchaseError with a user-facing message on failure.
        """
        async with self.tx() as c:
            async with c.execute(
                "SELECT * FROM countries WHERE id = ? AND active = 1", (country_id,)
            ) as cur:
                country = await cur.fetchone()
            if not country:
                raise PurchaseError("This country is not available right now.")
            async with c.execute(
                "SELECT id FROM accounts WHERE country_id = ? AND status = 'available' "
                "ORDER BY id LIMIT ?",
                (country_id, qty),
            ) as cur:
                account_ids = [r["id"] for r in await cur.fetchall()]
            if len(account_ids) < qty:
                raise PurchaseError(f"Only {len(account_ids)} in stock. Please lower the quantity.")

            total = round(country["price"] * qty, 2)
            await self._change_balance(
                c, user_id, -total, "purchase", f"{qty} x {country['name']}"
            )
            await c.execute(
                "UPDATE users SET total_spent = total_spent + ? WHERE id = ?", (total, user_id)
            )
            order_ids = []
            ts = now()
            for acc_id in account_ids:
                await c.execute(
                    "UPDATE accounts SET status = 'sold', sold_at = ? WHERE id = ?", (ts, acc_id)
                )
                cur = await c.execute(
                    "INSERT INTO orders(user_id, account_id, country_id, price, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, acc_id, country_id, country["price"], ts),
                )
                order_ids.append(cur.lastrowid)

        placeholders = ",".join("?" * len(order_ids))
        return await self.all(
            f"""{ORDER_SELECT} WHERE o.id IN ({placeholders}) ORDER BY o.id""",
            tuple(order_ids),
        )

    # --------------------------------------------------------------- orders
    async def get_order(self, order_id: int) -> Optional[dict]:
        return await self.one(f"{ORDER_SELECT} WHERE o.id = ?", (order_id,))

    async def user_orders(self, user_id: int, offset: int, limit: int) -> list[dict]:
        return await self.all(
            f"{ORDER_SELECT} WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )

    async def count_user_orders(self, user_id: int) -> int:
        return await self.scalar("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,))

    async def recent_orders(self, limit: int = 15) -> list[dict]:
        return await self.all(f"{ORDER_SELECT} ORDER BY o.id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------- deposits
    async def create_deposit(self, user_id: int, amount: float) -> int:
        return await self.run(
            "INSERT INTO deposits(user_id, amount, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, round(amount, 2), now(), now()),
        )

    async def get_deposit(self, deposit_id: int) -> Optional[dict]:
        return await self.one("SELECT * FROM deposits WHERE id = ?", (deposit_id,))

    async def count_pending_deposits(self, user_id: int) -> int:
        return await self.scalar(
            "SELECT COUNT(*) FROM deposits WHERE user_id = ? AND status = 'pending'", (user_id,)
        )

    async def submit_utr(self, deposit_id: int, user_id: int, utr: str) -> str:
        """Attach a UTR to a deposit. Returns 'ok', 'duplicate', 'expired' or 'invalid'."""
        async with self.tx() as c:
            async with c.execute(
                "SELECT * FROM deposits WHERE id = ? AND user_id = ?", (deposit_id, user_id)
            ) as cur:
                dep = await cur.fetchone()
            if not dep or dep["status"] != "created":
                return "invalid"
            if dep["created_at"] < ago(minutes=config.PAYMENT_TIMEOUT_MIN):
                await c.execute(
                    "UPDATE deposits SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now(), deposit_id),
                )
                return "expired"
            async with c.execute("SELECT 1 FROM deposits WHERE utr = ?", (utr,)) as cur:
                if await cur.fetchone():
                    return "duplicate"
            await c.execute(
                "UPDATE deposits SET utr = ?, status = 'pending', updated_at = ? WHERE id = ?",
                (utr, now(), deposit_id),
            )
            return "ok"

    async def cancel_deposit(self, deposit_id: int, user_id: int) -> None:
        await self.run(
            "UPDATE deposits SET status = 'cancelled', updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'created'",
            (now(), deposit_id, user_id),
        )

    async def resolve_deposit(self, deposit_id: int, approve: bool, admin_id: int,
                              amount: Optional[float] = None) -> Optional[dict]:
        """Approve or reject a pending deposit.

        On approval the user is credited (optionally with a corrected amount)
        and the referrer receives their commission. Returns a dict with the
        deposit, credited amount and referral bonus, or None if the deposit
        was not pending (e.g. another admin already handled it).
        """
        ref_percent = float(await self.get_setting("ref_percent") or 0)
        async with self.tx() as c:
            async with c.execute("SELECT * FROM deposits WHERE id = ?", (deposit_id,)) as cur:
                dep = await cur.fetchone()
            if not dep or dep["status"] != "pending":
                return None
            dep = dict(dep)
            if not approve:
                await c.execute(
                    "UPDATE deposits SET status = 'rejected', admin_id = ?, updated_at = ? "
                    "WHERE id = ?",
                    (admin_id, now(), deposit_id),
                )
                return {"deposit": dep, "credited": 0, "ref_bonus": 0, "referrer_id": None}

            credited = round(amount if amount is not None else dep["amount"], 2)
            await c.execute(
                "UPDATE deposits SET status = 'approved', amount = ?, admin_id = ?, updated_at = ? "
                "WHERE id = ?",
                (credited, admin_id, now(), deposit_id),
            )
            await self._change_balance(c, dep["user_id"], credited, "deposit", f"Deposit #{deposit_id}")
            await c.execute(
                "UPDATE users SET total_deposit = total_deposit + ? WHERE id = ?",
                (credited, dep["user_id"]),
            )
            async with c.execute(
                "SELECT referrer_id FROM users WHERE id = ?", (dep["user_id"],)
            ) as cur:
                referrer_id = (await cur.fetchone())["referrer_id"]
            bonus = 0.0
            if referrer_id and ref_percent > 0:
                bonus = round(credited * ref_percent / 100, 2)
                if bonus > 0:
                    await self._change_balance(
                        c, referrer_id, bonus, "referral", f"From user {dep['user_id']}"
                    )
                    await c.execute(
                        "UPDATE users SET ref_earned = ref_earned + ? WHERE id = ?",
                        (bonus, referrer_id),
                    )
            return {"deposit": dep, "credited": credited, "ref_bonus": bonus,
                    "referrer_id": referrer_id}

    async def pending_deposits(self, limit: int = 20) -> list[dict]:
        return await self.all(
            "SELECT d.*, u.username, u.first_name FROM deposits d JOIN users u ON u.id = d.user_id "
            "WHERE d.status = 'pending' ORDER BY d.id LIMIT ?",
            (limit,),
        )

    # -------------------------------------------------------------- coupons
    async def create_coupon(self, code: str, amount: float, max_uses: int) -> bool:
        try:
            await self.run(
                "INSERT INTO coupons(code, amount, max_uses, created_at) VALUES (?, ?, ?, ?)",
                (code.upper(), round(amount, 2), max_uses, now()),
            )
            return True
        except aiosqlite.IntegrityError:
            return False

    async def coupons(self) -> list[dict]:
        return await self.all("SELECT * FROM coupons ORDER BY created_at DESC")

    async def delete_coupon(self, code: str) -> None:
        await self.run("DELETE FROM coupons WHERE code = ?", (code,))

    async def redeem_coupon(self, user_id: int, code: str) -> tuple[str, float]:
        """Returns (status, amount). status: ok | invalid | used | exhausted."""
        code = code.strip().upper()
        async with self.tx() as c:
            async with c.execute("SELECT * FROM coupons WHERE code = ?", (code,)) as cur:
                coupon = await cur.fetchone()
            if not coupon:
                return "invalid", 0
            if coupon["used"] >= coupon["max_uses"]:
                return "exhausted", 0
            async with c.execute(
                "SELECT 1 FROM coupon_uses WHERE code = ? AND user_id = ?", (code, user_id)
            ) as cur:
                if await cur.fetchone():
                    return "used", 0
            await c.execute("INSERT INTO coupon_uses(code, user_id) VALUES (?, ?)", (code, user_id))
            await c.execute("UPDATE coupons SET used = used + 1 WHERE code = ?", (code,))
            await self._change_balance(c, user_id, coupon["amount"], "coupon", f"Code {code}")
            return "ok", coupon["amount"]

    # ---------------------------------------------------------------- stats
    async def stats(self) -> dict:
        today = local_midnight_utc()
        week = ago(days=7)

        async def revenue(since: Optional[str]) -> tuple[float, int]:
            if since:
                row = await self.one(
                    "SELECT COALESCE(SUM(price),0) s, COUNT(*) n FROM orders WHERE created_at >= ?",
                    (since,),
                )
            else:
                row = await self.one("SELECT COALESCE(SUM(price),0) s, COUNT(*) n FROM orders")
            return row["s"], row["n"]

        return {
            "users": await self.scalar("SELECT COUNT(*) FROM users"),
            "users_today": await self.scalar(
                "SELECT COUNT(*) FROM users WHERE joined_at >= ?", (today,)
            ),
            "stock": await self.scalar("SELECT COUNT(*) FROM accounts WHERE status = 'available'"),
            "sold": await self.scalar("SELECT COUNT(*) FROM accounts WHERE status = 'sold'"),
            "today": await revenue(today),
            "week": await revenue(week),
            "all": await revenue(None),
            "deposits": await self.scalar(
                "SELECT COALESCE(SUM(amount),0) FROM deposits WHERE status = 'approved'"
            ),
            "pending": await self.scalar("SELECT COUNT(*) FROM deposits WHERE status = 'pending'"),
            "balances": await self.scalar("SELECT COALESCE(SUM(balance),0) FROM users"),
        }

    async def backup(self, target: str) -> None:
        async with self._lock:
            async with aiosqlite.connect(target) as dst:
                await self.conn.backup(dst)


ORDER_SELECT = """
SELECT o.*, a.phone, a.twofa, a.session, c.name AS country_name, c.flag AS country_flag,
       u.username, u.first_name
FROM orders o
JOIN accounts a  ON a.id = o.account_id
JOIN countries c ON c.id = o.country_id
JOIN users u     ON u.id = o.user_id
"""

db = Database(config.DATABASE_PATH)
