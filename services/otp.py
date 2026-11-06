"""Fetch the latest Telegram login code for a sold account.

When an account is stocked with a Telethon StringSession, the buyer can log
in on their own device and press "Get OTP": the bot reads the newest message
from Telegram's service account (777000) through that session and extracts
the login code. Requires API_ID / API_HASH and the optional `telethon`
package. Without them the feature reports itself as unavailable.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import config

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")
TELEGRAM_SERVICE_ID = 777000


def available() -> bool:
    if not (config.API_ID and config.API_HASH):
        return False
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


async def fetch_code(session: str, max_age_minutes: int = 10) -> tuple[Optional[str], str]:
    """Return (code, status). status: ok | none | invalid_session | error."""
    from telethon import TelegramClient
    from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedError
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session), config.API_ID, config.API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None, "invalid_session"
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        async for message in client.iter_messages(TELEGRAM_SERVICE_ID, limit=5):
            if message.date < cutoff:
                break
            match = CODE_RE.search(message.message or "")
            if match:
                return match.group(1), "ok"
        return None, "none"
    except (AuthKeyUnregisteredError, UserDeactivatedError):
        return None, "invalid_session"
    except Exception:  # network errors, flood waits, etc.
        log.exception("OTP fetch failed")
        return None, "error"
    finally:
        await client.disconnect()
