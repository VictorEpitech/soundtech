import logging

from sqlalchemy import select

from app.auth import audit
from app.config import Settings
from app.db import SessionLocal
from app.models import Role, User

log = logging.getLogger("sound.bootstrap")


async def ensure_bootstrap_admin(settings: Settings) -> None:
    email = settings.bootstrap_admin_email.strip().lower()
    if not email:
        return
    async with SessionLocal() as session:
        exists = await session.scalar(select(User.id).where(User.email == email))
        if exists is not None:
            return
        session.add(User(email=email, role=Role.admin.value, is_active=True))
        audit.record(session, "bootstrap_admin", actor=email)
        await session.commit()
        log.info("bootstrap admin created")
