"""Rapport post-incident PDF (reportlab, polices standard). Tout texte libre est échappé."""

import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PARIS = ZoneInfo("Europe/Paris")
MAX_MESSAGE = 2000
MAX_FIELD = 300
MAX_ROWS = 300
STATUS_LABELS = {
    "safe_campus": "En sécurité (campus)",
    "safe_offsite": "En sécurité (hors campus)",
    "danger": "En danger",
}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class ReportInput:
    incident: dict[str, Any]
    data: dict[str, Any]
    speakers: list[str] = field(default_factory=list)
    reminders: list[datetime] = field(default_factory=list)
    actor: str = ""
    retention_days: int = 90
    now: datetime | None = None


def safe(v: Any, limit: int = MAX_FIELD) -> str:
    """Texte brut -> markup reportlab sûr : borné, sans contrôle, hors WinAnsi remplacé, échappé."""
    s = _CTRL.sub("", "" if v is None else str(v)).strip()
    if len(s) > limit:
        s = s[:limit] + "…"
    s = s.encode("cp1252", "replace").decode("cp1252")
    return escape(s).replace("\n", "<br/>")


def _parse(d: Any) -> datetime | None:
    if isinstance(d, datetime):
        return d
    if isinstance(d, str):
        try:
            return datetime.fromisoformat(d)
        except ValueError:
            return None
    return None


def fmt_dt(d: Any) -> str:
    dt = _parse(d)
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(PARIS).strftime("%d/%m/%Y %H:%M:%S")


def safe_filename(incident_id: Any, now: datetime) -> str:
    iid = re.sub(r"[^0-9a-fA-F-]", "", str(incident_id))[:36]
    return f"incident-{iid}-{now.astimezone(PARIS):%Y%m%d}.pdf"


def _delivery_lines(delivery: dict[str, Any] | None) -> tuple[str, str]:
    d = delivery or {}
    tts, teams = d.get("tts"), d.get("teams")

    def line(r: Any) -> str:
        if not isinstance(r, dict):
            return "non renseigné"
        if r.get("ok"):
            return "ignoré (désactivé)" if r.get("skipped") else "envoyé"
        return f"échec ({safe(r.get('error') or '?', 120)})"

    return line(tts), line(teams)


def _timeline(inc: dict[str, Any], rin: ReportInput) -> list[tuple[str, str]]:
    ev: list[tuple[datetime, str]] = []

    def add(d: Any, label: str) -> None:
        dt = _parse(d)
        if dt is not None:
            ev.append((dt if dt.tzinfo else dt.replace(tzinfo=UTC), label))

    by = f" par {safe(inc.get('created_by_email'))}" if inc.get("created_by_email") else ""
    add(inc.get("created_at"), f"Création{by}")
    tts, teams = _delivery_lines(inc.get("delivery"))
    if inc.get("sent_at"):
        add(inc["sent_at"], "Envoi")
        add(inc["sent_at"], f"Résultat enceintes (TTS) : {tts}")
        add(inc["sent_at"], f"Résultat Teams : {teams}")
    for r in rin.reminders:
        add(r, "Relance des non-répondants")
    rows = rin.data.get("respondents") or []
    stamped = [(r, _parse(r.get("received_at"))) for r in rows]
    stamped = [(r, t) for r, t in stamped if t is not None]
    if stamped:
        first = min(stamped, key=lambda x: x[1])
        add(first[1], "Première réponse reçue")
        last = max(stamped, key=lambda x: x[1])
        add(last[1], "Dernière réponse reçue")
        dangers = [x for x in stamped if x[0].get("status") == "danger"]
        if dangers:
            add(min(dangers, key=lambda x: x[1])[1], "Premier signalement « en danger »")
    if inc.get("closed_at"):
        by = f" par {safe(inc.get('closed_by_email'))}" if inc.get("closed_by_email") else ""
        add(inc["closed_at"], f"Clôture{by}")
    ev.sort(key=lambda x: x[0])
    return [(fmt_dt(t), label) for t, label in ev]


def build_pdf(rin: ReportInput) -> bytes:
    inc, data = rin.incident, rin.data
    now = rin.now or datetime.now(UTC)
    is_test = bool(inc.get("is_test"))
    ss = getSampleStyleSheet()
    body = ParagraphStyle(
        "b", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13
    )
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10, textColor=colors.grey)
    h1 = ParagraphStyle(
        "h1", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18, alignment=0
    )
    h2 = ParagraphStyle(
        "h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12, spaceBefore=10
    )
    banner_col = colors.HexColor("#E65100") if is_test else colors.HexColor("#B71C1C")
    banner = ParagraphStyle(
        "bn", parent=body, fontName="Helvetica-Bold", fontSize=13, textColor=colors.white
    )

    def P(t: str, st: ParagraphStyle = body) -> Paragraph:
        return Paragraph(t, st)

    def grid(rows: list[list[Paragraph]], widths: list[float], head: bool = True) -> Table:
        t = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
        style = [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
        if head:
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")))
        t.setStyle(TableStyle(style))
        return t

    W = A4[0] - 40 * mm
    el: list[Any] = []
    el.append(P(safe(inc.get("title"), 255), h1))
    mode = "TEST (exercice)" if is_test else "RÉEL"
    bt = Table([[P(f"{mode} — statut : {safe(inc.get('status'))}", banner)]], colWidths=[W])
    bt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), banner_col)]))
    el += [
        bt,
        Spacer(1, 4),
        P(f"Type : {safe(inc.get('type'))} — Identifiant : {safe(inc.get('id'))}"),
    ]

    el.append(P("Chronologie (heure de Paris)", h2))
    tl = _timeline(inc, rin)
    el.append(
        grid(
            [[P("Horodatage"), P("Événement")]] + [[P(a), P(b)] for a, b in tl],
            [40 * mm, W - 40 * mm],
        )
    )

    el.append(P("Message diffusé", h2))
    el.append(P(safe(inc.get("message"), MAX_MESSAGE)))

    tts, teams = _delivery_lines(inc.get("delivery"))
    el.append(P("Diffusion", h2))
    spk = ", ".join(safe(s, 80) for s in rin.speakers[:50]) or "non renseignées"
    el.append(
        P(f"Enceintes ciblées : {spk}<br/>Résultat enceintes : {tts}<br/>Résultat Teams : {teams}")
    )

    counts = data.get("counts") or {}
    roster = data.get("roster") or {}
    responded = int(counts.get("responded", 0))
    total = int(counts.get("roster_total", 0))
    roster_ok = bool(roster.get("configured")) and not roster.get("error")
    el.append(P("Taux de réponse", h2))
    if roster_ok and total:
        pct = round(100 * responded / total)
        el.append(P(f"{responded} répondant(s) sur {total} attendus ({pct} %)."))
    elif roster.get("error"):
        el.append(P(f"Roster indisponible : {responded} répondant(s) (total attendu inconnu)."))
    else:
        el.append(P(f"{responded} répondant(s) (roster non configuré : total attendu inconnu)."))

    el.append(P("Répartition par statut", h2))
    rows = [[P("Statut"), P("Nombre")]]
    for k, lab in STATUS_LABELS.items():
        rows.append([P(lab), P(str(int(counts.get(k, 0))))])
    el.append(grid(rows, [W - 30 * mm, 30 * mm]))

    dangers = [r for r in data.get("respondents") or [] if r.get("status") == "danger"]
    el.append(P(f"Personnes en danger ({len(dangers)})", h2))
    if dangers:
        rows = [[P("Nom"), P("Email"), P("Reçu le"), P("Précision")]]
        for r in dangers[:MAX_ROWS]:
            rows.append(
                [
                    P(safe(r.get("name"), 100)),
                    P(safe(r.get("email"), 120)),
                    P(fmt_dt(r.get("received_at"))),
                    P(safe(r.get("text"), 500)),
                ]
            )
        el.append(grid(rows, [34 * mm, 42 * mm, 28 * mm, W - 104 * mm]))
        if len(dangers) > MAX_ROWS:
            el.append(P(f"… et {len(dangers) - MAX_ROWS} autre(s) (voir l'export CSV).", small))
    else:
        el.append(P("Aucune personne n'a signalé être en danger."))

    el.append(P("Non-répondants", h2))
    if roster.get("error"):
        el.append(P("Roster indisponible : liste des non-répondants non calculable."))
    elif not roster.get("configured"):
        el.append(P("Roster non configuré : liste des non-répondants non disponible."))
    else:
        miss = data.get("non_respondents") or []
        if miss:
            rows = [[P("Nom"), P("Email")]]
            for m in miss[:MAX_ROWS]:
                rows.append([P(safe(m.get("name"), 100)), P(safe(m.get("email"), 120))])
            el.append(grid(rows, [W / 2, W / 2]))
            if len(miss) > MAX_ROWS:
                el.append(P(f"… et {len(miss) - MAX_ROWS} autre(s) (voir l'export CSV).", small))
        else:
            el.append(P("Tous les membres attendus ont répondu."))

    gen = f"Généré le {fmt_dt(now)} (Paris) par {safe(rin.actor, 120)}"
    conf = (
        "CONFIDENTIEL — données personnelles sensibles (RGPD). Diffusion restreinte ; "
        f"les réponses sont conservées {int(rin.retention_days)} jours (RESPONSE_RETENTION_DAYS)."
    )

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(20 * mm, 14 * mm, gen.encode("cp1252", "replace").decode("cp1252"))
        canvas.drawString(20 * mm, 10 * mm, conf)
        canvas.drawRightString(A4[0] - 20 * mm, 14 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=22 * mm,
        title=f"Rapport incident {str(inc.get('id'))[:36]}",
        author="Sound",
    )
    doc.build(el, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
