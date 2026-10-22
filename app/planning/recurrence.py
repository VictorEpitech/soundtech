"""Calcul des échéances d'une planification dans son fuseau (fonctions pures, DST sûr).

Règles DST : une heure locale inexistante (saut de printemps) est décalée de la durée du saut
(02:30 -> 03:30) ; une heure dupliquée (retour d'automne) ne s'exécute qu'une fois, à sa première
occurrence. Les échéances sont exprimées en UTC.
"""

import re
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
_TZ_RE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")


ClosedFn = Callable[[date], bool]


class Recurrence(Protocol):
    timezone: str
    days: list[int]
    time_of_day: str
    once_date: date | None


def load_tz(name: str) -> ZoneInfo:
    if not _TZ_RE.fullmatch(name):
        raise ValueError(f"fuseau inconnu : {name[:64]!r}")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"fuseau inconnu : {name[:64]!r}") from None


def parse_time(value: str) -> time:
    if not re.fullmatch(TIME_PATTERN, value):
        raise ValueError("heure attendue au format HH:MM")
    return time(int(value[:2]), int(value[3:]))


def local_to_utc(d: date, t: time, tz: ZoneInfo) -> datetime:
    """Heure murale -> UTC ; première occurrence si dupliquée, décalée si inexistante."""
    naive = datetime.combine(d, t)
    return naive.replace(tzinfo=tz, fold=0).astimezone(UTC)


def local_date(instant: datetime, tz: ZoneInfo) -> date:
    return instant.astimezone(tz).date()


def _candidate_dates(rec: Recurrence, first: date, last: date) -> Iterator[date]:
    if rec.once_date is not None:
        if first <= rec.once_date <= last:
            yield rec.once_date
        return
    days = set(rec.days)
    d = first
    while d <= last:
        if d.isoweekday() in days:
            yield d
        d += timedelta(days=1)


def occurrences(rec: Recurrence, start: datetime, end: datetime) -> list[datetime]:
    """Échéances UTC dans l'intervalle (start, end], triées, sans doublon."""
    tz = load_tz(rec.timezone)
    t = parse_time(rec.time_of_day)
    first = local_date(start, tz) - timedelta(days=1)
    last = local_date(end, tz) + timedelta(days=1)
    found = {local_to_utc(d, t, tz) for d in _candidate_dates(rec, first, last)}
    return sorted(x for x in found if start < x <= end)


def next_occurrences(
    rec: Recurrence,
    after: datetime,
    limit: int,
    is_closed: ClosedFn | None = None,
    horizon_days: int = 800,
) -> list[datetime]:
    """Prochaines échéances strictement après `after`, hors jours fermés si `is_closed`."""
    out: list[datetime] = []
    tz = load_tz(rec.timezone)
    cursor = after
    end = after + timedelta(days=horizon_days)
    if rec.once_date is not None:  # une date unique n'est pas soumise à l'horizon
        end = max(end, datetime.combine(rec.once_date, time.max, UTC) + timedelta(days=2))
    step = timedelta(days=60)
    while len(out) < limit and cursor < end:
        chunk_end = min(cursor + step, end)
        for x in occurrences(rec, cursor, chunk_end):
            if is_closed is not None and is_closed(local_date(x, tz)):
                continue
            out.append(x)
            if len(out) >= limit:
                break
        cursor = chunk_end
    return out


def closed_days(ranges: Iterable[tuple[date, date]], holidays: dict[date, str] | None) -> ClosedFn:
    spans = list(ranges)
    hol = holidays or {}

    def check(d: date) -> bool:
        return d in hol or any(a <= d <= b for a, b in spans)

    return check
