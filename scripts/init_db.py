"""Create the expense-log tables in the configured database.

Usage (from the project root, with the venv active):

    python -m scripts.init_db

Reads DATABASE_URL from the environment or .env and runs
``Base.metadata.create_all`` against it. Safe to re-run: existing tables are
left untouched (new columns on existing tables are NOT added — use a
migration tool such as Alembic once the schema starts changing).
"""

import asyncio
import sys

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.base import Base
from app.db import models  # noqa: F401 - importing registers the tables
from app.db.session import for_asyncpg


async def _create_tables(database_url: str) -> None:
    url, connect_args = for_asyncpg(database_url)
    engine = create_async_engine(url, connect_args=connect_args)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        print("Error: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    tables = ", ".join(Base.metadata.tables)
    print(f"Creating tables ({tables}) if they do not exist...")
    asyncio.run(_create_tables(settings.database_url))
    print("Done.")


if __name__ == "__main__":
    main()
