"""File prioritaire Redis : sorted set, score = niveau * SCALE + séquence (FIFO par niveau)."""

from redis.asyncio import Redis

from app.models import Priority

QUEUE_KEY = "sound:ha:queue"
SEQ_KEY = "sound:ha:seq"
LOCK_KEY = "sound:ha:drain-lock"
SCALE = 10**12


async def push(redis: Redis, announcement_id: str, priority: Priority) -> None:
    seq = await redis.incr(SEQ_KEY)
    await redis.zadd(QUEUE_KEY, {announcement_id: int(priority) * SCALE + seq % SCALE})


async def pop(redis: Redis) -> str | None:
    res = await redis.zpopmin(QUEUE_KEY, 1)
    if not res:
        return None
    member = res[0][0]
    return member.decode() if isinstance(member, bytes) else str(member)
