from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from database.base import Base


def _nonempty(value: str, field: str, limit: int = 255) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return value


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("length(trim(username)) > 0", name="ck_user_username"),)

    # Telegram IDs can exceed PostgreSQL INTEGER; Telegram supplies the ID.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    medicines: Mapped[list[Medicine]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @validates("username")
    def validate_username(self, key: str, value: str | None) -> str | None:
        return None if value is None else _nonempty(value, key)


class Medicine(Base):
    __tablename__ = "medicines"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_medicine_amount"),
        CheckConstraint("length(trim(name)) > 0", name="ck_medicine_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    amount: Mapped[int] = mapped_column(default=0, server_default="0")

    user: Mapped[User] = relationship(back_populates="medicines")
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="medicine", cascade="all, delete-orphan"
    )

    @validates("name")
    def validate_name(self, key: str, value: str) -> str:
        return _nonempty(value, key)

    @validates("amount")
    def validate_amount(self, key: str, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("amount must be a nonnegative integer")
        return value


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint("length(trim(text)) > 0", name="ck_reminder_text"),
        CheckConstraint(
            "length(remind_time) = 5 AND remind_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'",
            name="ck_reminder_time",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id"), index=True)
    text: Mapped[str] = mapped_column(String(255))
    # Daily clock time; the scheduler determines the applicable timezone/date.
    remind_time: Mapped[str] = mapped_column(String(5), index=True)

    user: Mapped[User] = relationship(back_populates="reminders")
    medicine: Mapped[Medicine] = relationship(back_populates="reminders")
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan"
    )

    @validates("text")
    def validate_text(self, key: str, value: str) -> str:
        return _nonempty(value, key)

    @validates("remind_time")
    def validate_remind_time(self, key: str, value: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
            raise ValueError("remind_time must be HH:MM in 24-hour format")
        return value


class Notification(Base):
    """A single scheduled delivery of a daily reminder."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("reminder_id", "scheduled_for", name="uq_notification_occurrence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reminder: Mapped[Reminder] = relationship(back_populates="notifications")

    @validates("scheduled_for", "sent_at")
    def validate_timestamp(self, key: str, value: datetime | None) -> datetime | None:
        if value is None and key == "sent_at":
            return None
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError(f"{key} must be a timezone-aware datetime")
        return value.astimezone(timezone.utc)
