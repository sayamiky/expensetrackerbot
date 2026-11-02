"""Shared test fixtures."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401 - importing registers the tables
from app.db.base import Base


@pytest.fixture
def db_sessionmaker():
    """An async_sessionmaker backed by a fresh in-memory SQLite database.

    StaticPool keeps a single connection alive for the engine's lifetime, so
    every session sees the same in-memory database.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async def _create_tables() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_tables())

    yield async_sessionmaker(engine, expire_on_commit=False)

    asyncio.run(engine.dispose())
