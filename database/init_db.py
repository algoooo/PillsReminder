"""Table initialization and migration of the known legacy Telegram users table."""

from sqlalchemy import DateTime, Integer, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from database.base import Base
from database import models  # Register all tables in Base.metadata.


def _migrate_legacy_users(connection: Connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table("users"):
        return
    columns = {column["name"]: column for column in inspector.get_columns("users")}
    if "id" in columns:
        return
    primary_key = inspector.get_pk_constraint("users")["constrained_columns"]
    if "telegram_id" not in columns or primary_key != ["telegram_id"]:
        raise RuntimeError(
            "Existing users table has no id column and is not the supported legacy "
            "table with telegram_id as its primary key; migrate its schema explicitly."
        )
    if not isinstance(columns["telegram_id"]["type"], Integer):
        raise RuntimeError("Legacy users.telegram_id must be an integer column")
    registered_at = columns.get("registered_at")
    if registered_at is not None and not isinstance(registered_at["type"], DateTime):
        raise RuntimeError("Legacy users.registered_at must be a timestamp column")

    # Qualify the table actually resolved by the connection's search path.
    schema = connection.scalar(text("SELECT current_schema()"))
    quote = connection.dialect.identifier_preparer.quote
    table = f"{quote(schema)}.{quote('users')}"
    connection.execute(text(f"ALTER TABLE {table} RENAME COLUMN telegram_id TO id"))
    connection.execute(text(f"ALTER TABLE {table} ALTER COLUMN id TYPE BIGINT"))
    if registered_at is not None and registered_at["default"] is None:
        connection.execute(text(
            f"ALTER TABLE {table} ALTER COLUMN registered_at SET DEFAULT CURRENT_TIMESTAMP"
        ))


def _migrate_reminder_time(connection: Connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table("reminders"):
        return
    columns = {c["name"]: c for c in inspector.get_columns("reminders")}
    column_type = columns["remind_time"]["type"]
    if isinstance(column_type, DateTime):
        schema = connection.scalar(text(
            "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = to_regclass('reminders')"
        ))
        quote = connection.dialect.identifier_preparer.quote
        table = f"{quote(schema)}.{quote('reminders')}"
        expression = "remind_time AT TIME ZONE 'UTC'" if column_type.timezone else "remind_time"
        connection.execute(text(
            f"ALTER TABLE {table} ALTER COLUMN remind_time TYPE VARCHAR(5) "
            f"USING to_char({expression}, 'HH24:MI')"
        ))
        connection.execute(text(
            f"ALTER TABLE {table} ADD CONSTRAINT ck_reminder_time CHECK "
            "(length(remind_time) = 5 AND remind_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$')"
        ))


async def initialize_tables(engine: AsyncEngine) -> None:
    """Preserve legacy user data and create missing tables in one transaction."""
    async with engine.begin() as connection:
        await connection.run_sync(_migrate_legacy_users)
        await connection.run_sync(_migrate_reminder_time)
        await connection.run_sync(Base.metadata.create_all)
