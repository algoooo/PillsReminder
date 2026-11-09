"""Async PostgreSQL CRUD. Every call owns its session and transaction.

Set DATABASE_URL to postgresql+asyncpg://user:password@host:5432/database.
Returned models retain scalar attributes after the session closes; relationships
are not loaded. Reminder times are HH:MM; notification timestamps are aware UTC.
Missing rows return None (get/update) or False (delete). Invalid values raise
ValueError; constraint/connection errors propagate after automatic rollback.
"""

import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import cache
from dotenv import load_dotenv

from sqlalchemy import Date, DateTime, Time, cast, func, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.init_db import initialize_tables
from database.models import Medicine, Notification, Reminder, User

load_dotenv()
@cache
def _session_factory() -> async_sessionmaker[AsyncSession]:
    """Initialize lazily so importing the module does not require configuration."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL connection URL")
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+asyncpg")
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError("DATABASE_URL must use PostgreSQL with asyncpg")
    engine = create_async_engine(parsed, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Migrate the supported legacy users table and create missing tables.

    Safe to call on later starts. Existing rows are preserved. Other schema
    changes still require explicit migrations; the database must already exist.
    """
    await initialize_tables(_session_factory().kw["bind"])


async def close_db() -> None:
    """Call after stopping handlers and scheduler tasks during bot shutdown."""
    if _session_factory.cache_info().currsize:
        await _session_factory().kw["bind"].dispose()
        _session_factory.cache_clear()


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")


async def _owned_medicine(session: AsyncSession, medicine_id: int, user_id: int) -> Medicine | None:
    return await session.scalar(select(Medicine).where(
        Medicine.id == medicine_id, Medicine.user_id == user_id
    ))


async def _owned_reminder(session: AsyncSession, reminder_id: int, user_id: int) -> Reminder | None:
    return await session.scalar(select(Reminder).where(
        Reminder.id == reminder_id, Reminder.user_id == user_id
    ))


async def create_user(user_id: int, username: str | None = None) -> User:
    """Register a Telegram user. Duplicate IDs raise IntegrityError."""
    user = User(id=user_id, username=username)
    async with _session_factory().begin() as session:
        session.add(user)
    return user


async def get_user(user_id: int) -> User | None:
    async with _session_factory()() as session:
        return await session.get(User, user_id)


async def update_user(user_id: int, username: str | None) -> User | None:
    """Update or clear the Telegram username."""
    async with _session_factory().begin() as session:
        user = await session.get(User, user_id)
        if user is not None:
            user.username = username
    return user


async def delete_user(user_id: int) -> bool:
    """Delete the user and their medicines and reminders."""
    async with _session_factory().begin() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False
        # AsyncSession.delete awaits relationship loading for ORM cascades.
        await session.delete(user)
    return True


async def create_medicine(user_id: int, name: str, amount: int = 0) -> Medicine:
    medicine = Medicine(user_id=user_id, name=name, amount=amount)
    async with _session_factory().begin() as session:
        if await session.get(User, user_id) is None:
            raise ValueError("User does not exist")
        session.add(medicine)
    return medicine


async def get_medicine(medicine_id: int, *, user_id: int) -> Medicine | None:
    async with _session_factory()() as session:
        return await _owned_medicine(session, medicine_id, user_id)


async def list_medicines(user_id: int) -> list[Medicine]:
    async with _session_factory()() as session:
        return list(await session.scalars(select(Medicine).where(
            Medicine.user_id == user_id
        ).order_by(Medicine.id)))


async def update_medicine(
    medicine_id: int, *, user_id: int, name: str | None = None, amount: int | None = None,
) -> Medicine | None:
    """Update supplied fields. None leaves a field unchanged; zero amount is valid."""
    async with _session_factory().begin() as session:
        medicine = await _owned_medicine(session, medicine_id, user_id)
        if medicine is not None:
            if name is not None:
                medicine.name = name
            if amount is not None:
                medicine.amount = amount
    return medicine


async def delete_medicine(medicine_id: int, *, user_id: int) -> bool:
    """Delete an owned medicine and its reminders."""
    async with _session_factory().begin() as session:
        medicine = await _owned_medicine(session, medicine_id, user_id)
        if medicine is None:
            return False
        await session.delete(medicine)
    return True


async def create_reminder(
    user_id: int, medicine_id: int, text: str, remind_time: str,
) -> Reminder:
    reminder = Reminder(user_id=user_id, medicine_id=medicine_id, text=text, remind_time=remind_time)
    async with _session_factory().begin() as session:
        if await _owned_medicine(session, medicine_id, user_id) is None:
            raise ValueError("Medicine must exist and belong to the user")
        session.add(reminder)
    return reminder


async def get_reminder(reminder_id: int, *, user_id: int) -> Reminder | None:
    async with _session_factory()() as session:
        return await _owned_reminder(session, reminder_id, user_id)


async def list_reminders(user_id: int) -> list[Reminder]:
    async with _session_factory()() as session:
        return list(await session.scalars(select(Reminder).where(
            Reminder.user_id == user_id
        ).order_by(Reminder.remind_time, Reminder.id)))


async def get_due_reminders(current_time: str, *, limit: int = 100) -> list[Reminder]:
    """Read daily reminders matching HH:MM exactly; does not claim delivery.

    Use notification records for dated deliveries, catch-up, and sent state.
    """
    _validate_limit(limit)
    Reminder(remind_time=current_time)
    async with _session_factory()() as session:
        return list(await session.scalars(select(Reminder).where(
            Reminder.remind_time == current_time
        ).order_by(Reminder.id).limit(limit)))


async def update_reminder(
    reminder_id: int, *, user_id: int, text: str | None = None,
    remind_time: str | None = None, medicine_id: int | None = None,
) -> Reminder | None:
    async with _session_factory().begin() as session:
        reminder = await _owned_reminder(session, reminder_id, user_id)
        if reminder is not None:
            if medicine_id is not None:
                if await _owned_medicine(session, medicine_id, user_id) is None:
                    raise ValueError("Medicine must exist and belong to the user")
                reminder.medicine_id = medicine_id
            if text is not None:
                reminder.text = text
            if remind_time is not None:
                reminder.remind_time = remind_time
    return reminder


async def delete_reminder(reminder_id: int, *, user_id: int) -> bool:
    async with _session_factory().begin() as session:
        reminder = await _owned_reminder(session, reminder_id, user_id)
        if reminder is None:
            return False
        await session.delete(reminder)
    return True


async def _owned_notification(
    session: AsyncSession, notification_id: int, user_id: int,
) -> Notification | None:
    return await session.scalar(select(Notification).join(Reminder).where(
        Notification.id == notification_id, Reminder.user_id == user_id
    ))


async def create_notification(
    user_id: int, reminder_id: int, scheduled_for: datetime,
) -> Notification:
    """Queue one occurrence; duplicate (reminder_id, scheduled_for) raises IntegrityError."""
    notification = Notification(reminder_id=reminder_id, scheduled_for=scheduled_for)
    async with _session_factory().begin() as session:
        if await _owned_reminder(session, reminder_id, user_id) is None:
            raise ValueError("Reminder must exist and belong to the user")
        session.add(notification)
    return notification


async def get_notification(notification_id: int, *, user_id: int) -> Notification | None:
    async with _session_factory()() as session:
        return await _owned_notification(session, notification_id, user_id)


async def list_notifications(
    user_id: int, *, reminder_id: int | None = None, pending_only: bool = False,
) -> list[Notification]:
    statement = select(Notification).join(Reminder).where(Reminder.user_id == user_id)
    if reminder_id is not None:
        statement = statement.where(Notification.reminder_id == reminder_id)
    if pending_only:
        statement = statement.where(Notification.checked_at.is_(None))
    async with _session_factory()() as session:
        return list(await session.scalars(statement.order_by(Notification.scheduled_for, Notification.id)))


async def get_due_notifications(
    due_before: datetime, *, limit: int = 100,
    after: tuple[datetime, int] | None = None,
    repeat_after: timedelta = timedelta(minutes=5),
) -> list[Notification]:
    """Read due unchecked occurrences whose last send is outside the repeat interval."""
    _validate_limit(limit)
    validated = Notification(scheduled_for=due_before).scheduled_for
    if repeat_after <= timedelta(0):
        raise ValueError("repeat_after must be positive")
    statement = select(Notification).where(
        Notification.scheduled_for <= validated, Notification.checked_at.is_(None),
        or_(Notification.sent_at.is_(None), Notification.sent_at <= validated - repeat_after),
    )
    if after is not None:
        statement = statement.where(tuple_(Notification.scheduled_for, Notification.id) > after)
    async with _session_factory()() as session:
        return list(await session.scalars(
            statement.order_by(Notification.scheduled_for, Notification.id).limit(limit)
        ))


async def update_notification(
    notification_id: int, *, user_id: int, sent_at: datetime | None,
    scheduled_for: datetime | None = None,
) -> Notification | None:
    """Set/reset the last-send timestamp and optionally reschedule; does not confirm intake."""
    async with _session_factory().begin() as session:
        notification = await _owned_notification(session, notification_id, user_id)
        if notification is not None:
            notification.sent_at = sent_at
            if scheduled_for is not None:
                notification.scheduled_for = scheduled_for
    return notification


async def delete_notification(notification_id: int, *, user_id: int) -> bool:
    async with _session_factory().begin() as session:
        notification = await _owned_notification(session, notification_id, user_id)
        if notification is None:
            return False
        await session.delete(notification)
    return True


async def queue_due_notifications(now: datetime, *, timezone_name: str) -> None:
    """Queue today's due daily occurrences, including times missed before startup.

    Repeated/concurrent calls are safe through the unique occurrence constraint.
    All notification timestamps are UTC; clock strings use timezone_name.
    """
    now = Notification(scheduled_for=now).scheduled_for
    local_now = now.astimezone(ZoneInfo(timezone_name))
    local_timestamp = cast(
        literal(local_now.date(), type_=Date) + cast(Reminder.remind_time, Time), DateTime
    )
    scheduled_for = func.timezone(timezone_name, local_timestamp)
    statement = insert(Notification).from_select(
        ["reminder_id", "scheduled_for"],
        select(Reminder.id, scheduled_for).where(
            Reminder.remind_time <= local_now.strftime("%H:%M"), scheduled_for <= now
        ),
    ).on_conflict_do_nothing(constraint="uq_notification_occurrence")
    async with _session_factory().begin() as session:
        await session.execute(statement)


@asynccontextmanager
async def notification_delivery(
    notification_id: int, *, due_before: datetime,
    repeat_after: timedelta = timedelta(minutes=5),
) -> AsyncIterator[tuple[Notification, Reminder, Medicine] | None]:
    """Lock one pending delivery until send/ack completes; skip other workers' locks.

    The caller sets notification.sent_at only after sending successfully.
    Exceptions roll back the transaction and leave the delivery pending.
    """
    validated = Notification(scheduled_for=due_before).scheduled_for
    if repeat_after <= timedelta(0):
        raise ValueError("repeat_after must be positive")
    async with _session_factory().begin() as session:
        row = (await session.execute(
            select(Notification, Reminder, Medicine)
            .join(Reminder, Notification.reminder_id == Reminder.id)
            .join(Medicine, Reminder.medicine_id == Medicine.id)
            .where(
                Notification.id == notification_id,
                Notification.scheduled_for <= validated,
                Notification.checked_at.is_(None),
                or_(Notification.sent_at.is_(None), Notification.sent_at <= validated - repeat_after),
            )
            .with_for_update(of=Notification, skip_locked=True)
        )).one_or_none()
        yield None if row is None else tuple(row)


async def confirm_notification(
    notification_id: int, *, user_id: int, checked_at: datetime | None = None,
) -> Notification | None:
    """Confirm this user's occurrence once; serialize with any in-flight send."""
    checked_at = Notification(checked_at=checked_at or datetime.now(timezone.utc)).checked_at
    async with _session_factory().begin() as session:
        notification = await session.scalar(
            select(Notification).join(Reminder).where(
                Notification.id == notification_id, Reminder.user_id == user_id
            ).with_for_update(of=Notification)
        )
        if notification is not None and notification.checked_at is None:
            notification.checked_at = checked_at
    return notification
