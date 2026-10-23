import uuid
from typing import Any

from sqlalchemy import delete, select

from app.models import Announcement, AuditLog, Incident, Response, Role
from tests.helpers import audit_actions


async def _ann(db: Any, author: Any) -> uuid.UUID:
    async with db() as s:
        a = Announcement(
            author_id=author.id,
            author_email=author.email,
            text="msg",
            priority=1,
            targets=[],
            status="done",
        )
        s.add(a)
        await s.commit()
        return a.id


async def _incident(db: Any, status: str) -> uuid.UUID:
    async with db() as s:
        i = Incident(type="autre", title="t", message="m", status=status)
        s.add(i)
        await s.flush()
        s.add(Response(incident_id=i.id, user_aad_id="x", status="danger"))
        await s.commit()
        return i.id


async def test_staff_sees_only_own(client, make_user, login_as, db):
    me = await make_user(Role.staff)
    other = await make_user(Role.staff)
    mine = await _ann(db, me)
    theirs = await _ann(db, other)
    await login_as(me)
    r = await client.get("/announcements", params={"limit": 200})
    ids = {a["id"] for a in r.json()}
    assert str(mine) in ids and str(theirs) not in ids
    assert r.headers["X-Total-Count"] == str(len(ids))
    assert (await client.get(f"/announcements/{theirs}")).status_code == 404
    assert (await client.get(f"/announcements/{mine}")).status_code == 200
    h = await client.get("/ui/announce/history")
    assert str(theirs) not in h.text


async def test_admin_sees_all(client, make_user, login_as, db):
    admin = await make_user(Role.admin)
    other = await make_user(Role.staff)
    theirs = await _ann(db, other)
    await login_as(admin)
    assert (await client.get(f"/announcements/{theirs}")).status_code == 200


async def test_staff_cannot_delete(client, make_user, login_as, db):
    me = await make_user(Role.staff)
    mine = await _ann(db, me)
    inc = await _incident(db, "closed")
    await login_as(me)
    assert (await client.delete(f"/announcements/{mine}")).status_code == 403
    assert (await client.delete(f"/incidents/{inc}")).status_code == 403
    async with db() as s:
        assert await s.get(Announcement, mine) is not None
        assert await s.get(Incident, inc) is not None


async def test_admin_deletes_announcement(client, make_user, login_as, db):
    admin = await make_user(Role.admin)
    a = await _ann(db, admin)
    await login_as(admin)
    assert (await client.delete(f"/announcements/{a}")).status_code == 204
    assert (await client.delete(f"/announcements/{a}")).status_code == 404
    assert "announcement_delete" in await audit_actions(db, admin.email)
    async with db() as s:
        assert await s.get(Announcement, a) is None
        log = await s.scalar(select(AuditLog).where(AuditLog.action == "announcement_delete"))
        assert log.detail["announcement_id"] == str(a)


async def test_admin_deletes_closed_incident_cascade(client, make_user, login_as, db):
    admin = await make_user(Role.admin)
    inc = await _incident(db, "closed")
    await login_as(admin)
    assert (await client.delete(f"/incidents/{inc}")).status_code == 204
    assert "incident_delete" in await audit_actions(db, admin.email)
    async with db() as s:
        assert await s.get(Incident, inc) is None
        assert await s.scalar(select(Response).where(Response.incident_id == inc)) is None


async def test_open_incident_refused(client, make_user, login_as, db):
    admin = await make_user(Role.admin)
    await login_as(admin)
    for st in ("draft", "sent", "failed"):
        inc = await _incident(db, st)
        assert (await client.delete(f"/incidents/{inc}")).status_code == 409
        async with db() as s:
            assert await s.get(Incident, inc) is not None
    async with db() as s:  # ne pas laisser d'incident ouvert aux autres tests
        await s.execute(delete(Response).where(Response.user_aad_id == "x"))
        await s.execute(delete(Incident).where(Incident.title == "t"))
        await s.commit()
