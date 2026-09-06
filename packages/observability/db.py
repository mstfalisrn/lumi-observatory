# LUMI — DB session / engine (asyncio)
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from observability.config import settings


def _env_url() -> str:
    # in compose DATABASE_URL comes via env; otherwise config default
    import os
    return os.environ.get("DATABASE_URL") or settings.DATABASE_URL


engine = create_async_engine(_env_url(), pool_pre_ping=True, echo=False)


async_session_factory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_session():
    async with async_session_factory() as session:
        yield session


async def init_models():
    from observability import models
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)