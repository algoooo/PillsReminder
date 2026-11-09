"""Integration tests: TEST_DATABASE_URL must point at a disposable PostgreSQL DB."""

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from database import crud
from database.base import Base


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to a disposable PostgreSQL database")
class CrudTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {"DATABASE_URL": os.environ["TEST_DATABASE_URL"]})
        self.environment.start()
        await crud.init_db()
        self.user = await crud.create_user(5_000_000_000, " Alice ")
        self.other = await crud.create_user(5_000_000_001)
        self.medicine = await crud.create_medicine(self.user.id, " Tablet ", 3)
        self.now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        self.clock = "12:00"

    async def asyncTearDown(self):
        try:
            async with crud._session_factory().kw["bind"].begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
        finally:
            await crud.close_db()
            self.environment.stop()

    async def reminder(self, **fields):
        return await crud.create_reminder(
            self.user.id, self.medicine.id,
            fields.get("text", "Take tablet"), fields.get("remind_time", self.clock),
        )

    async def test_legacy_users_migration_preserves_data(self):
        engine = crud._session_factory().kw["bind"]
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.execute(text("""
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    username VARCHAR,
                    first_name VARCHAR,
                    registered_at TIMESTAMP NOT NULL
                )
            """))
            await connection.execute(text("""
                INSERT INTO users VALUES (123, 'Legacy', 'Original', '2020-01-01')
            """))
        await crud.init_db()
        await crud.init_db()
        self.assertEqual((await crud.get_user(123)).username, "Legacy")
        user = await crud.create_user(5_000_000_000)
        medicine = await crud.create_medicine(user.id, "Tablet")
        reminder = await crud.create_reminder(user.id, medicine.id, "Take", self.clock)
        self.assertEqual(reminder.user_id, user.id)
        async with engine.connect() as connection:
            row = (await connection.execute(text(
                "SELECT first_name, registered_at FROM users WHERE id = 123"
            ))).one()
            self.assertEqual(tuple(row), ("Original", datetime(2020, 1, 1)))
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns("users"))
            self.assertEqual(str(next(c for c in columns if c["name"] == "id")["type"]), "BIGINT")
            self.assertIsNotNone(await connection.scalar(text(
                "SELECT registered_at FROM users WHERE id = 5000000000"
            )))

    async def test_initialization_preserves_existing_data(self):
        await crud.init_db()
        await crud.init_db()
        self.assertEqual((await crud.get_user(self.user.id)).username, "Alice")
        medicines = await crud.list_medicines(self.user.id)
        self.assertEqual([medicine.id for medicine in medicines], [self.medicine.id])

    async def test_round_trip_and_detached_results(self):
        self.assertEqual(self.user.username, "Alice")
        self.assertIsNone(self.other.username)
        self.assertEqual((await crud.get_user(self.user.id)).id, 5_000_000_000)
        self.assertEqual((await crud.get_medicine(self.medicine.id, user_id=self.user.id)).name, "Tablet")
        self.assertEqual(len(await crud.list_medicines(self.user.id)), 1)
        self.assertIsNone((await crud.update_user(self.user.id, None)).username)
        updated = await crud.update_medicine(self.medicine.id, user_id=self.user.id, amount=0, name="New")
        self.assertEqual((updated.name, updated.amount), ("New", 0))
        reminder = await self.reminder()
        second = await crud.create_medicine(self.user.id, "Other")
        updated = await crud.update_reminder(
            reminder.id, user_id=self.user.id, medicine_id=second.id,
            text="Updated", remind_time="13:00",
        )
        loaded = await crud.get_reminder(reminder.id, user_id=self.user.id)
        self.assertEqual((loaded.text, loaded.medicine_id, loaded.remind_time),
                         ("Updated", second.id, "13:00"))
        self.assertEqual(updated.id, loaded.id)
        self.assertEqual(len(await crud.list_reminders(self.user.id)), 1)
        self.assertTrue(await crud.delete_reminder(reminder.id, user_id=self.user.id))
        self.assertIsNone(await crud.get_reminder(reminder.id, user_id=self.user.id))

    async def test_ownership_and_missing_rows(self):
        reminder = await self.reminder()
        foreign = await crud.create_medicine(self.other.id, "Foreign")
        self.assertIsNone(await crud.get_medicine(self.medicine.id, user_id=self.other.id))
        self.assertIsNone(await crud.update_medicine(self.medicine.id, user_id=self.other.id, amount=99))
        self.assertFalse(await crud.delete_medicine(self.medicine.id, user_id=self.other.id))
        self.assertIsNone(await crud.get_reminder(reminder.id, user_id=self.other.id))
        self.assertIsNone(await crud.update_reminder(reminder.id, user_id=self.other.id, text="Wrong"))
        self.assertFalse(await crud.delete_reminder(reminder.id, user_id=self.other.id))
        with self.assertRaises(ValueError):
            await crud.create_reminder(self.user.id, foreign.id, "Wrong", self.clock)
        with self.assertRaises(ValueError):
            await crud.update_reminder(reminder.id, user_id=self.user.id, medicine_id=foreign.id)
        with self.assertRaises(ValueError):
            await crud.create_medicine(999, "Missing")
        self.assertIsNone(await crud.get_user(999))
        self.assertIsNone(await crud.update_user(999, "Missing"))
        self.assertFalse(await crud.delete_user(999))
        self.assertIsNone(await crud.update_medicine(999, user_id=self.user.id))
        self.assertIsNone(await crud.update_reminder(999, user_id=self.user.id))
        self.assertFalse(await crud.delete_medicine(999, user_id=self.user.id))
        self.assertFalse(await crud.delete_reminder(999, user_id=self.user.id))

    async def test_validation_and_rollback(self):
        with self.assertRaises(ValueError):
            await crud.update_medicine(self.medicine.id, user_id=self.user.id, name="Partial", amount=-1)
        medicine = await crud.get_medicine(self.medicine.id, user_id=self.user.id)
        self.assertEqual((medicine.name, medicine.amount), ("Tablet", 3))
        with self.assertRaises(ValueError):
            await crud.create_user(123, " ")
        with self.assertRaises(ValueError):
            await crud.create_medicine(self.user.id, "Invalid", True)
        with self.assertRaises(ValueError):
            await self.reminder(remind_time=self.now.replace(tzinfo=timezone.utc))
        with self.assertRaises(IntegrityError):
            await crud.create_user(self.user.id, "Duplicate")
        self.assertEqual((await crud.get_user(self.user.id)).username, "Alice")
        self.assertEqual(len(await crud.list_medicines(self.user.id)), 1)

    async def test_cascades(self):
        reminder = await self.reminder()
        self.assertTrue(await crud.delete_medicine(self.medicine.id, user_id=self.user.id))
        self.assertIsNone(await crud.get_reminder(reminder.id, user_id=self.user.id))
        medicine = await crud.create_medicine(self.user.id, "New")
        reminder = await crud.create_reminder(self.user.id, medicine.id, "Take", self.clock)
        self.assertTrue(await crud.delete_user(self.user.id))
        self.assertIsNone(await crud.get_user(self.user.id))
        self.assertIsNone(await crud.get_reminder(reminder.id, user_id=self.user.id))
        self.assertEqual(await crud.list_medicines(self.user.id), [])
        self.assertIsNotNone(await crud.get_user(self.other.id))

    async def test_due_reminders(self):
        late = await self.reminder(remind_time="13:00")
        early = await self.reminder(remind_time="00:00")
        equal = await self.reminder()
        self.assertEqual([r.id for r in await crud.get_due_reminders("12:00")], [equal.id])
        self.assertEqual([r.id for r in await crud.get_due_reminders("00:00", limit=1)], [early.id])
        self.assertEqual(len(await crud.list_reminders(self.user.id)), 3)
        with self.assertRaises(ValueError):
            await crud.get_due_reminders("12:00", limit=0)
        for invalid in ("24:00", "12:60", "9:00", "09:0", "09:00:00", "09:00\n", "", None, self.now):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await self.reminder(remind_time=invalid)
        self.assertIsNotNone(await crud.get_reminder(late.id, user_id=self.user.id))

    async def test_reminder_time_migration(self):
        reminder = await self.reminder(remind_time="09:05")
        engine = crud._session_factory().kw["bind"]
        for sql_type in ("TIMESTAMP", "TIMESTAMP WITH TIME ZONE"):
            async with engine.begin() as connection:
                await connection.execute(text("ALTER TABLE reminders DROP CONSTRAINT ck_reminder_time"))
                await connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                await connection.execute(text(
                    f"ALTER TABLE reminders ALTER COLUMN remind_time TYPE {sql_type} "
                    f"USING ('2026-11-09 ' || remind_time)::{sql_type}"
                ))
            await crud.init_db()
            await crud.init_db()
            loaded = await crud.get_reminder(reminder.id, user_id=self.user.id)
            self.assertEqual(loaded.remind_time, "09:05")
            async with engine.connect() as connection:
                columns = await connection.run_sync(lambda sync: inspect(sync).get_columns("reminders"))
                self.assertEqual(str(next(c for c in columns if c["name"] == "remind_time")["type"]), "VARCHAR(5)")

    async def test_notification_crud(self):
        reminder = await self.reminder()
        notification = await crud.create_notification(self.user.id, reminder.id, self.now)
        self.assertIsNone(notification.sent_at)
        self.assertEqual(notification.scheduled_for, self.now)
        loaded = await crud.get_notification(notification.id, user_id=self.user.id)
        self.assertEqual(loaded.reminder_id, reminder.id)
        self.assertEqual([n.id for n in await crud.list_notifications(self.user.id)], [notification.id])
        self.assertEqual(len(await crud.list_notifications(self.user.id, reminder_id=reminder.id, pending_only=True)), 1)
        updated = await crud.update_notification(notification.id, user_id=self.user.id, sent_at=self.now)
        self.assertEqual(updated.sent_at, self.now)
        self.assertEqual(await crud.list_notifications(self.user.id, pending_only=True), [])
        self.assertEqual(await crud.get_due_notifications(self.now), [])
        updated = await crud.update_notification(notification.id, user_id=self.user.id, sent_at=None, scheduled_for=self.now + timedelta(days=1))
        self.assertIsNone(updated.sent_at)
        self.assertEqual(updated.scheduled_for, self.now + timedelta(days=1))
        self.assertTrue(await crud.delete_notification(notification.id, user_id=self.user.id))
        self.assertIsNone(await crud.get_notification(notification.id, user_id=self.user.id))
        self.assertFalse(await crud.delete_notification(notification.id, user_id=self.user.id))

    async def test_notification_ownership_validation_and_duplicates(self):
        reminder = await self.reminder()
        notification = await crud.create_notification(self.user.id, reminder.id, self.now)
        self.assertIsNone(await crud.get_notification(notification.id, user_id=self.other.id))
        self.assertEqual(await crud.list_notifications(self.other.id), [])
        self.assertIsNone(await crud.update_notification(notification.id, user_id=self.other.id, sent_at=self.now))
        self.assertFalse(await crud.delete_notification(notification.id, user_id=self.other.id))
        self.assertIsNone(await crud.update_notification(999, user_id=self.user.id, sent_at=self.now))
        for user_id, reminder_id in ((self.other.id, reminder.id), (self.user.id, 999)):
            with self.assertRaises(ValueError):
                await crud.create_notification(user_id, reminder_id, self.now)
        with self.assertRaises(ValueError):
            await crud.create_notification(self.user.id, reminder.id, self.now.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            await crud.update_notification(notification.id, user_id=self.user.id, sent_at=self.now.replace(tzinfo=None))
        with self.assertRaises(IntegrityError):
            await crud.create_notification(self.user.id, reminder.id, self.now.astimezone(timezone(timedelta(hours=3))))
        self.assertIsNone((await crud.get_notification(notification.id, user_id=self.user.id)).sent_at)
        self.assertEqual(len(await crud.list_notifications(self.user.id)), 1)

    async def test_due_notifications(self):
        reminder = await self.reminder()
        early = await crud.create_notification(self.user.id, reminder.id, self.now - timedelta(hours=1))
        equal = await crud.create_notification(self.user.id, reminder.id, self.now)
        late = await crud.create_notification(self.user.id, reminder.id, self.now + timedelta(hours=1))
        self.assertEqual([n.id for n in await crud.get_due_notifications(self.now)], [early.id, equal.id])
        self.assertEqual([n.id for n in await crud.get_due_notifications(self.now, limit=1)], [early.id])
        await crud.update_notification(early.id, user_id=self.user.id, sent_at=self.now)
        self.assertEqual([n.id for n in await crud.get_due_notifications(self.now)], [equal.id])
        with self.assertRaises(ValueError):
            await crud.get_due_notifications(self.now, limit=0)
        with self.assertRaises(ValueError):
            await crud.get_due_notifications(self.now.replace(tzinfo=None))
        self.assertIsNotNone(await crud.get_notification(late.id, user_id=self.user.id))

    async def test_notification_cascades(self):
        for index, parent in enumerate(("reminder", "medicine", "user")):
            user = await crud.create_user(8_000_000_000 + index)
            medicine = await crud.create_medicine(user.id, "Tablet")
            reminder = await crud.create_reminder(user.id, medicine.id, "Take", "00:00")
            notification = await crud.create_notification(user.id, reminder.id, self.now)
            if parent == "reminder":
                await crud.delete_reminder(reminder.id, user_id=user.id)
            elif parent == "medicine":
                await crud.delete_medicine(medicine.id, user_id=user.id)
            else:
                await crud.delete_user(user.id)
            self.assertNotIn(notification.id, [n.id for n in await crud.get_due_notifications(self.now)])

    async def test_scheduler_daily_queue_timezone_and_deduplication(self):
        await self.reminder(remind_time="15:00")
        await self.reminder(remind_time="15:01")
        await asyncio.gather(*[
            crud.queue_due_notifications(self.now, timezone_name="Europe/Moscow") for _ in range(3)
        ])
        queued = await crud.list_notifications(self.user.id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].scheduled_for, self.now)
        await crud.queue_due_notifications(self.now + timedelta(days=1), timezone_name="Europe/Moscow")
        self.assertEqual(len(await crud.list_notifications(self.user.id)), 2)

    async def test_scheduler_send_once_and_daily_recurrence(self):
        from reminder import poll_reminders
        reminder = await self.reminder(remind_time="15:00")
        bot = AsyncMock()
        self.assertEqual(await poll_reminders(bot, now=self.now, timezone_name="Europe/Moscow"), 1)
        self.assertEqual(await poll_reminders(bot, now=self.now, timezone_name="Europe/Moscow"), 0)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.kwargs["chat_id"], self.user.id)
        self.assertIn("Tablet", bot.send_message.call_args.kwargs["text"])
        self.assertIn(reminder.text, bot.send_message.call_args.kwargs["text"])
        self.assertEqual(await poll_reminders(bot, now=self.now + timedelta(days=1), timezone_name="Europe/Moscow"), 1)
        self.assertEqual(len(await crud.list_notifications(self.user.id)), 2)

    async def test_scheduler_retries_failed_send_after_restart(self):
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import SendMessage
        from reminder import send_notifications
        reminder = await self.reminder()
        notification = await crud.create_notification(self.user.id, reminder.id, self.now)
        bot = AsyncMock()
        bot.send_message.side_effect = TelegramNetworkError(
            method=SendMessage(chat_id=self.user.id, text="test"), message="offline"
        )
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(await send_notifications(bot, now=self.now), 0)
        self.assertIsNone((await crud.get_notification(notification.id, user_id=self.user.id)).sent_at)
        await crud.close_db()  # Simulate a restarted process with no in-memory sent state.
        bot.send_message.side_effect = None
        self.assertEqual(await send_notifications(bot, now=self.now), 1)
        self.assertIsNotNone((await crud.get_notification(notification.id, user_id=self.user.id)).sent_at)

    async def test_scheduler_concurrent_delivery_locks(self):
        from reminder import send_notifications
        reminder = await self.reminder()
        await crud.create_notification(self.user.id, reminder.id, self.now)
        bot = AsyncMock()
        async def send(**kwargs):
            await asyncio.sleep(0.02)
        bot.send_message.side_effect = send
        counts = await asyncio.gather(*[send_notifications(bot, now=self.now) for _ in range(2)])
        self.assertEqual(sum(counts), 1)
        bot.send_message.assert_awaited_once()

    async def test_scheduler_failed_first_row_does_not_starve_later_batches(self):
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import SendMessage
        from reminder import send_notifications
        reminder = await self.reminder()
        notifications = []
        for index in range(101):
            notifications.append(await crud.create_notification(
                self.user.id, reminder.id, self.now - timedelta(minutes=1) + timedelta(microseconds=index)
            ))
        bot = AsyncMock()
        bot.send_message.side_effect = [TelegramNetworkError(
            method=SendMessage(chat_id=self.user.id, text="test"), message="offline"
        )] + [None] * 100
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(await send_notifications(bot, now=self.now), 100)
        self.assertEqual(bot.send_message.await_count, 101)
        self.assertEqual([n.id for n in await crud.get_due_notifications(self.now)], [notifications[0].id])

    async def test_concurrent_calls_use_independent_sessions(self):
        medicines = await asyncio.gather(*[
            crud.create_medicine(self.user.id, f"Medicine {i}") for i in range(8)
        ])
        self.assertEqual(len({m.id for m in medicines}), 8)
        self.assertEqual(len(await crud.list_medicines(self.user.id)), 9)


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_and_invalid_url(self):
        await crud.close_db()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                await crud.get_user(1)
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite+aiosqlite:///:memory:"}):
            with self.assertRaises(ValueError):
                await crud.get_user(1)
        await crud.close_db()


if __name__ == "__main__":
    unittest.main()
