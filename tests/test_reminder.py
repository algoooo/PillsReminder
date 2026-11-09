"""Worker control-flow checks; Telegram and database calls are mocked."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

import reminder


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_failure_is_retried_and_cancellation_propagates(self):
        with patch.object(reminder, "poll_reminders", AsyncMock(side_effect=[RuntimeError("offline"), 0])) as poll, patch.object(reminder.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])) as sleep:
            with self.assertLogs("reminder", level="ERROR"), self.assertRaises(asyncio.CancelledError):
                await reminder.poll_users(AsyncMock(), interval=1, timezone_name="UTC")
        self.assertEqual(poll.await_count, 2)
        self.assertEqual(sleep.await_count, 2)

    async def test_telegram_retry_after_is_respected(self):
        error = TelegramRetryAfter(method=SendMessage(chat_id=1, text="test"), message="rate limit", retry_after=30)
        with patch.object(reminder, "poll_reminders", AsyncMock(side_effect=error)), patch.object(reminder.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)) as sleep:
            with self.assertLogs("reminder", level="WARNING"), self.assertRaises(asyncio.CancelledError):
                await reminder.poll_users(AsyncMock(), interval=15, timezone_name="UTC")
            sleep.assert_awaited_once_with(30)

    async def test_invalid_configuration_fails_at_startup(self):
        for interval in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                await reminder.poll_users(AsyncMock(), interval=interval)
        with self.assertRaises(KeyError):
            await reminder.poll_users(AsyncMock(), timezone_name="Invalid/Timezone")


if __name__ == "__main__":
    unittest.main()
