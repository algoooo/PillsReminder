"""Daily reminder scheduling and Telegram delivery.

Clock times use REMINDER_TIMEZONE (Europe/Moscow by default). Pending deliveries
survive restarts. Today's missed times are queued on startup; earlier days are
only retried if a notification record already exists.
"""

import asyncio
import logging
import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import crud

logger = logging.getLogger(__name__)


async def send_notifications(bot: Bot, *, now: datetime | None = None) -> int:
    """Send/repeat unchecked occurrences at five-minute intervals; return send count."""
    now = now if now is not None else datetime.now(timezone.utc)
    sent = 0
    after = None
    while True:
        pending = await crud.get_due_notifications(now, limit=100, after=after)
        if not pending:
            return sent
        for notification in pending:
            try:
                async with crud.notification_delivery(notification.id, due_before=now) as delivery:
                    if delivery is None:
                        continue
                    record, reminder, medicine = delivery
                    mk = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✅ I have taken", callback_data=f"profile_logged_{record.id}")
                        ]
                    ])
                    await bot.send_message(
                        chat_id=reminder.user_id,
                        text=f"🕒 Time to take {medicine.name} ({reminder.remind_time})\n{reminder.text}",
                        parse_mode=None,
                        reply_markup=mk,
                    )
                    record.sent_at = now
                sent += 1
            except TelegramRetryAfter:
                # Respect Telegram's cooldown before making any further requests.
                raise
            except TelegramAPIError:
                logger.exception("Could not send notification %s; it remains pending", notification.id)
        # Keyset pagination visits later records even if an earlier send failed.
        after = (pending[-1].scheduled_for, pending[-1].id)


async def poll_reminders(
    bot: Bot, *, now: datetime | None = None, timezone_name: str | None = None,
) -> int:
    """Run one scheduling/delivery pass."""
    now = now if now is not None else datetime.now(timezone.utc)
    timezone_name = timezone_name or os.getenv("REMINDER_TIMEZONE", "Europe/Moscow")
    await crud.queue_due_notifications(now, timezone_name=timezone_name)
    return await send_notifications(bot, now=now)


async def poll_users(
    bot: Bot, *, interval: float = 15, timezone_name: str | None = None,
) -> None:
    """Poll until cancelled; database/API errors do not stop future passes."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be positive and finite")
    timezone_name = timezone_name or os.getenv("REMINDER_TIMEZONE", "Europe/Moscow")
    ZoneInfo(timezone_name)  # Fail at startup for a misconfigured timezone.
    while True:
        delay = interval
        try:
            await poll_reminders(bot, timezone_name=timezone_name)
        except TelegramRetryAfter as error:
            delay = max(interval, error.retry_after)
            logger.warning("Telegram rate limit; retrying in %s seconds", delay)
        except Exception:
            logger.exception("Reminder polling failed; retrying in %s seconds", interval)
        await asyncio.sleep(delay)
