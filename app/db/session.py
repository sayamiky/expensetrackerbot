"""Async engine/session setup for the expense log."""

from collections.abc import Callable
from functools import lru_cache
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings


class DatabaseError(RuntimeError):
    """Raised when the expense log's database is unavailable or unconfigured."""

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


def for_asyncpg(database_url: str) -> tuple[str, dict]:
    """Adapt a plain ``postgresql://`` URL (as Neon hands it out) for asyncpg.

    - ``sslmode`` and ``channel_binding`` are libpq-only query parameters that
      asyncpg's ``connect()`` rejects, so ``sslmode`` becomes asyncpg's ``ssl``
      connect arg and ``channel_binding`` is dropped (asyncpg negotiates SCRAM
      channel binding on its own whenever TLS is on).
    - Neon's pooled hostname (``...-pooler...``) goes through PgBouncer in
      transaction mode, which breaks asyncpg's prepared statement cache, so the
      cache is disabled.
    """
    parts = urlsplit(database_url)
    scheme = parts.scheme
    if scheme in {"postgres", "postgresql"}:
        scheme = "postgresql+asyncpg"

    query = dict(parse_qsl(parts.query))
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    connect_args: dict = {"statement_cache_size": 0}
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = True

    cleaned = urlunsplit(
        (scheme, parts.netloc, parts.path, "&".join(f"{k}={v}" for k, v in query.items()), parts.fragment)
    )
    return cleaned, connect_args


@lru_cache
def _get_sessionmaker(database_url: str) -> async_sessionmaker[AsyncSession]:
    url, connect_args = for_asyncpg(database_url)
    engine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)
    return async_sessionmaker(engine, expire_on_commit=False)


def get_db_sessionmaker(
    settings: Settings = Depends(get_settings),
) -> Callable[[], AsyncSession]:
    """Return a callable that opens a session; overridable in tests.

    It does not touch the database or fail until the returned callable is
    actually invoked, so a missing DATABASE_URL only surfaces when a handler
    really needs the database.
    """
    if not settings.database_url:

        def _unconfigured() -> AsyncSession:
            raise DatabaseError("DATABASE_URL is not configured on the server.")

        return _unconfigured

    return _get_sessionmaker(settings.database_url)
