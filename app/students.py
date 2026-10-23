"""Annuaire étudiants (admin) : liste, import CSV en 2 temps, suppressions."""

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.deps import get_session, require_role
from app.models import Role, Student, User

router = APIRouter(prefix="/admin/students", tags=["admin"])
admin_only = require_role(Role.admin)

MAX_IMPORT_BYTES = 1_000_000
DELETE_ALL_WORD = "SUPPRIMER"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FIELD_ALIASES = {
    "email": {"email", "e-mail", "mail", "courriel", "adresse email", "adresse mail"},
    "last_name": {"nom", "name", "last name", "lastname", "last_name", "surname", "nom de famille"},
    "first_name": {"prenom", "first name", "firstname", "first_name", "given name"},
    "promo": {"promo", "promotion"},
    "cursus": {"cursus", "parcours", "formation"},
}
_HEADER_TO_FIELD = {a: f for f, aliases in _FIELD_ALIASES.items() for a in aliases}


def _norm_header(h: str | None) -> str:
    s = unicodedata.normalize("NFKD", (h or "").strip().lower().lstrip("﻿"))
    return "".join(c for c in s if not unicodedata.combining(c))


@dataclass
class ParsedCsv:
    rows: dict[str, dict[str, str]] = field(default_factory=dict)  # email -> champs
    errors: list[dict[str, Any]] = field(default_factory=list)


def parse_csv(raw: str) -> ParsedCsv:
    """Séparateur `,` ou `;` détecté sur l'en-tête ; colonne email obligatoire."""
    raw = raw.lstrip("﻿")
    head = raw.split("\n", 1)[0]
    delim = ";" if head.count(";") > head.count(",") else ","
    reader = csv.reader(io.StringIO(raw), delimiter=delim)
    try:
        header = next(reader)
    except StopIteration:
        raise HTTPException(400, "empty csv") from None
    cols = {i: _HEADER_TO_FIELD.get(_norm_header(h)) for i, h in enumerate(header)}
    if "email" not in cols.values():
        raise HTTPException(400, "missing 'email' column")
    out = ParsedCsv()
    for line, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        rec = {"email": "", "last_name": "", "first_name": "", "promo": "", "cursus": ""}
        for i, c in enumerate(cells):
            f = cols.get(i)
            if f:
                rec[f] = c.strip()
        rec["email"] = rec["email"].lower()
        if len(rec["email"]) > 320 or not _EMAIL_RE.match(rec["email"]):
            out.errors.append({"line": line, "error": "email invalide"})
        elif any(len(rec[k]) > n for k, n in (("promo", 100), ("cursus", 100))) or any(
            len(rec[k]) > 255 for k in ("last_name", "first_name")
        ):
            out.errors.append({"line": line, "error": "valeur trop longue"})
        elif rec["email"] in out.rows:
            out.errors.append({"line": line, "error": "doublon dans le fichier"})
        else:
            out.rows[rec["email"]] = rec
    return out


class CsvIn(BaseModel):
    csv: str = Field(max_length=MAX_IMPORT_BYTES)


class ApplyIn(CsvIn):
    delete: list[str] = Field(default_factory=list, max_length=100_000)


class DeleteIn(BaseModel):
    scope: Literal["all", "promo", "cursus"]
    value: str = Field(default="", max_length=100)
    confirm: str = Field(default="", max_length=50)


@dataclass
class Plan:
    parsed: ParsedCsv
    existing: dict[str, Student]
    adds: list[str]
    updates: list[str]
    unchanged: int
    missing: list[Student]


def _check_size(raw: str) -> None:
    if len(raw.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "csv too large")


async def build_plan(session: AsyncSession, raw: str) -> Plan:
    _check_size(raw)
    parsed = parse_csv(raw)
    existing = {s.email: s for s in await session.scalars(select(Student))}
    adds, updates, unchanged = [], [], 0
    for email, r in parsed.rows.items():
        cur = existing.get(email)
        if cur is None:
            adds.append(email)
        elif any(getattr(cur, k) != r[k] for k in ("last_name", "first_name", "promo", "cursus")):
            updates.append(email)
        else:
            unchanged += 1
    missing = sorted(
        (s for e, s in existing.items() if e not in parsed.rows),
        key=lambda s: (s.promo, s.last_name, s.email),
    )
    return Plan(parsed, existing, adds, updates, unchanged, missing)


async def apply_import(session: AsyncSession, raw: str, to_delete: list[str]) -> tuple[Plan, int]:
    """Upsert par email puis suppression des cases cochées (limitées aux absents du fichier)."""
    plan = await build_plan(session, raw)
    for email in plan.adds:
        session.add(Student(**plan.parsed.rows[email]))
    for email in plan.updates:
        for k, v in plan.parsed.rows[email].items():
            setattr(plan.existing[email], k, v)
    absent = {s.email for s in plan.missing}
    wanted = {e.strip().lower() for e in to_delete} & absent
    if wanted:
        await session.execute(delete(Student).where(Student.email.in_(wanted)))
    return plan, len(wanted)


def filter_clause(q: str = "", promo: str = "", cursus: str = "") -> list[Any]:
    cl: list[Any] = []
    if promo:
        cl.append(Student.promo == promo)
    if cursus:
        cl.append(Student.cursus == cursus)
    if q.strip():
        like = f"%{q.strip().lower().replace('%', '').replace('_', '')}%"
        cl.append(
            or_(
                func.lower(Student.email).like(like),
                func.lower(Student.last_name).like(like),
                func.lower(Student.first_name).like(like),
            )
        )
    return cl


@router.post("/import/preview")
async def import_preview(
    body: CsvIn,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(admin_only),
) -> dict[str, Any]:
    p = await build_plan(session, body.csv)
    return {
        "adds": len(p.adds),
        "updates": len(p.updates),
        "unchanged": p.unchanged,
        "errors": p.parsed.errors,
        "missing": [s.email for s in p.missing],
    }


@router.post("/import/apply")
async def import_apply(
    request: Request,
    body: ApplyIn,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(admin_only),
) -> dict[str, Any]:
    plan, deleted = await apply_import(session, body.csv, body.delete)
    audit.record(
        session,
        "students_import",
        request=request,
        user_id=actor.id,
        actor=actor.email,
        detail={
            "added": len(plan.adds),
            "updated": len(plan.updates),
            "deleted": deleted,
            "errors": len(plan.parsed.errors),
        },
    )
    return {
        "added": len(plan.adds),
        "updated": len(plan.updates),
        "deleted": deleted,
        "errors": len(plan.parsed.errors),
    }


@router.post("/delete")
async def delete_students(
    request: Request,
    body: DeleteIn,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(admin_only),
) -> dict[str, Any]:
    cl: list[Any] = []
    if body.scope == "all":
        if body.confirm.strip().upper() != DELETE_ALL_WORD:
            raise HTTPException(422, f"type {DELETE_ALL_WORD} to confirm")
    else:
        if not body.value.strip():
            raise HTTPException(422, "value required")
        col = Student.promo if body.scope == "promo" else Student.cursus
        cl.append(col == body.value.strip())
    res = await session.execute(delete(Student).where(*cl))
    n = res.rowcount or 0
    audit.record(
        session,
        "students_delete",
        request=request,
        user_id=actor.id,
        actor=actor.email,
        detail={"scope": body.scope, "value": body.value.strip() or None, "count": n},
    )
    return {"deleted": n}
