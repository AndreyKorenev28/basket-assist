"""Окружение Alembic: асинхронный движок, batch-режим для SQLite."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from basket_assist.storage.tables import Base

target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(context.config.get_main_option("sqlalchemy.url"))
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=context.config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
