"""Worker control-flow checks; Telegram and database calls are mocked."""

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_check_button_saves_confirmation_and_removes_keyboard(self):
        from aiogram import types
        from bot.routes import profile
        message = Mock(spec=types.Message)
        message.text = "Time to take Tablet"
        message.edit_text = AsyncMock()
        callback = SimpleNamespace(data="profile_logged_42", from_user=SimpleNamespace(id=123), message=message, answer=AsyncMock())
        notification = SimpleNamespace(checked_at=datetime(2026, 11, 9, 12, tzinfo=timezone.utc))
        with patch.object(profile.crud, "confirm_notification", AsyncMock(return_value=notification)) as confirm:
            await profile.intake_logged(callback)
        confirm.assert_awaited_once_with(42, user_id=123)
        self.assertIsNone(message.edit_text.call_args.kwargs["reply_markup"])
        self.assertIn("Checked at", message.edit_text.call_args.kwargs["text"])
        callback.answer.assert_awaited_once()

    async def test_invalid_or_old_check_does_not_confirm_a_different_dose(self):
        from bot.routes import profile
        callback = SimpleNamespace(data="profile_logged", from_user=SimpleNamespace(id=123), message=None, answer=AsyncMock())
        with patch.object(profile.crud, "confirm_notification", AsyncMock(return_value=None)) as confirm:
            await profile.intake_logged(callback)
            confirm.assert_not_awaited()
            callback.data = "profile_logged_42"
            await profile.intake_logged(callback)
            confirm.assert_awaited_once_with(42, user_id=123)

    async def test_invalid_configuration_fails_at_startup(self):
        for interval in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                await reminder.poll_users(AsyncMock(), interval=interval)
        with self.assertRaises(KeyError):
            await reminder.poll_users(AsyncMock(), timezone_name="Invalid/Timezone")


if __name__ == "__main__":
    unittest.main()
