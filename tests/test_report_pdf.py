# ruff: noqa: F811
import io
import re

import pytest
from pypdf import PdfReader

from app.alerts import report_pdf
from app.alerts import router as alerts_router
from app.alerts.roster import LogRoster
from app.main import app
from app.models import AuditLog, Incident, Role
from tests.helpers import audit_actions
from tests.test_ha import ha_api, redis  # noqa: F401
from tests.test_responses import U1, U2, FakeRoster, _answer, _sent_incident, staff_api  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")


def text_of(r) -> str:
    assert r.content.startswith(b"%PDF")
    return re.sub(
        r"\s+", " ", "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    )


async def test_report_pdf_roles(staff_api, make_user, do_login, db) -> None:
    api, _, _ = staff_api
    iid = await _sent_incident(db)
    assert (await api.get(f"/incidents/{iid}/report.pdf")).status_code == 401
    student = await make_user(Role.student)
    assert (await do_login(preferred_username=student.email)).status_code == 403
    assert (await api.get(f"/incidents/{iid}/report.pdf")).status_code == 401


async def test_report_pdf_content_and_audit(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    u = await make_user(Role.staff)
    await login_as(u)
    iid = await _sent_incident(db)
    await _answer(db, iid, U1, "danger", "bloqué étage 2", name="Alice", email="a@epitech.eu")
    r = await api.get(f"/incidents/{iid}/report.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "no-store"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment") and re.search(rf'filename="incident-{iid}-\d{{8}}\.pdf"', cd)
    t = text_of(r)
    for needle in (
        "RÉEL",
        "Chronologie",
        "Message diffusé",
        "Personnes en danger (1)",
        "Alice",
        "a@epitech.eu",
        "bloqué étage 2",
        "1 répondant(s) sur 3",
        "Non-répondants",
        "Bob",
        "Carl",
        "RGPD",
        "par " + u.email,
    ):
        assert needle in t, needle
    assert "Première réponse" in t and "Création" in t
    async with db() as s:
        rows = (
            await s.scalars(
                __import__("sqlalchemy")
                .select(AuditLog)
                .where(AuditLog.action == "incident_report_export")
            )
        ).all()
    assert rows and rows[-1].detail == {"incident_id": str(iid)}
    assert "bloqué" not in str(rows[-1].detail)
    assert "incident_report_export" in await audit_actions(db, u.email)


async def test_report_pdf_markup_injection(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    await login_as(await make_user(Role.admin))
    async with db() as s:
        i = Incident(
            type="autre",
            title="<b>T</b> & <font size=99>",
            message="<i>x</i></para>",
            status="sent",
            is_test=True,
        )
        s.add(i)
        await s.commit()
        iid = i.id
    evil = '<a href="http://evil">clic</a><img src="file:///etc/passwd"/></b></para> 😀'
    await _answer(db, iid, U1, "danger", evil, name="<u>N</u>", email="e@x.eu")
    r = await api.get(f"/incidents/{iid}/report.pdf")
    assert r.status_code == 200
    t = text_of(r)
    assert "<b>T</b> & <font size=99>" in t and "<a href" in t and "<u>N</u>" in t
    assert "TEST (exercice)" in t and "RÉEL" not in t.split("Chronologie")[0]
    assert b"/URI" not in r.content


async def test_report_pdf_roster_down(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    await login_as(await make_user(Role.staff))
    iid = await _sent_incident(db)
    await _answer(db, iid, U1, "safe_campus", None, name="Alice")
    app.dependency_overrides[alerts_router.get_roster] = lambda: FakeRoster(fail=True)
    r = await api.get(f"/incidents/{iid}/report.pdf")
    assert r.status_code == 200
    t = text_of(r)
    assert "Roster indisponible" in t and "1 répondant(s)" in t


async def test_report_pdf_no_response_no_roster(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    await login_as(await make_user(Role.staff))
    iid = await _sent_incident(db)
    app.dependency_overrides[alerts_router.get_roster] = lambda: LogRoster()
    t = text_of(await api.get(f"/incidents/{iid}/report.pdf"))
    assert "0 répondant(s)" in t and "roster non configuré" in t
    assert "Aucune personne n'a signalé" in t
    app.dependency_overrides[alerts_router.get_roster] = lambda: FakeRoster([])
    assert "0 répondant(s)" in text_of(await api.get(f"/incidents/{iid}/report.pdf"))


async def test_report_pdf_unknown_incident_and_page_button(
    staff_api, make_user, login_as, db
) -> None:
    api, _, _ = staff_api
    await login_as(await make_user(Role.staff))
    assert (
        await api.get("/incidents/00000000-0000-0000-0000-000000000000/report.pdf")
    ).status_code == 404
    iid = await _sent_incident(db)
    assert f"/incidents/{iid}/report.pdf" in (await api.get(f"/alerts/{iid}")).text


def test_pdf_helpers_and_pagination() -> None:
    assert "<" not in report_pdf.safe("<b>x</b>").replace("<br/>", "")
    assert report_pdf.safe("a\x00b\n" + "z" * 1000, 10).endswith("…")
    assert report_pdf.fmt_dt("2026-07-01T10:00:00+00:00") == "01/07/2026 12:00:00"
    assert report_pdf.fmt_dt(None) == "—"
    members = [{"aad_id": str(n), "name": f"M{n}", "email": f"{n}@x.eu"} for n in range(400)]
    pdf = report_pdf.build_pdf(
        report_pdf.ReportInput(
            incident={
                "id": "i",
                "title": "t",
                "type": "autre",
                "message": "m" * 5000,
                "status": "sent",
            },
            data={
                "roster": {"configured": True, "error": None},
                "counts": {"responded": 0, "roster_total": 400},
                "respondents": [],
                "non_respondents": members,
            },
        )
    )
    assert len(PdfReader(io.BytesIO(pdf)).pages) > 2


async def test_report_pdf_timeline_includes_auto_reminders(
    staff_api, make_user, login_as, db
) -> None:
    api, _, _ = staff_api
    await login_as(await make_user(Role.staff))
    iid = await _sent_incident(db)
    async with db() as s:
        for action in ("incident_remind", "auto_remind", "auto_remind"):
            s.add(AuditLog(action=action, actor="x", detail={"incident_id": str(iid)}))
        await s.commit()
    t = text_of(await api.get(f"/incidents/{iid}/report.pdf"))
    assert t.count("Relance des non-répondants") == 3
