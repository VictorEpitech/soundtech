import asyncio
import time
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.alerts import responses
from app.models import Incident, Response

pytestmark = pytest.mark.usefixtures("migrated_db")

N = 500
USERS = 120
CONCURRENCY = 50
MAX_SECONDS = 30.0


async def test_concurrent_upserts_unique_and_fast(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as s:
        inc = Incident(id=uuid.uuid4(), type="autre", title="load", message="m", status="sent")
        s.add(inc)
        await s.commit()
    users = [str(uuid.UUID(int=i + 1)) for i in range(USERS)]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(i: int) -> None:
        async with sem, db() as s:
            await responses.record(
                s,
                inc.id,
                aad_id=users[i % USERS],
                name=f"u{i}",
                email=None,
                status=responses.STATUSES[i % len(responses.STATUSES)],
                text=None,
            )
            await s.commit()

    t0 = time.monotonic()
    results = await asyncio.gather(*(one(i) for i in range(N)), return_exceptions=True)
    elapsed = time.monotonic() - t0

    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors[:3]
    assert elapsed < MAX_SECONDS
    async with db() as s:
        total = await s.scalar(
            select(func.count()).select_from(Response).where(Response.incident_id == inc.id)
        )
        distinct = await s.scalar(
            select(func.count(func.distinct(Response.user_aad_id))).where(
                Response.incident_id == inc.id
            )
        )
    assert total == distinct == USERS
