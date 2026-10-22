from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.planning.holidays import easter, fr_holidays
from app.planning.recurrence import (
    closed_days,
    load_tz,
    next_occurrences,
    occurrences,
)


def rec(days=(1, 2, 3, 4, 5, 6, 7), at="08:00", tz="Europe/Paris", once=None):
    return SimpleNamespace(timezone=tz, days=list(days), time_of_day=at, once_date=once)


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def between(r, start: datetime, end: datetime) -> list[datetime]:
    return occurrences(r, start, end)


# --- fériés -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "paques", "ascension", "pentecote"),
    [
        (2026, date(2026, 4, 6), date(2026, 5, 14), date(2026, 5, 25)),
        (2027, date(2027, 3, 29), date(2027, 5, 6), date(2027, 5, 17)),
        (2028, date(2028, 4, 17), date(2028, 5, 25), date(2028, 6, 5)),
    ],
)
def test_movable_holidays(year, paques, ascension, pentecote) -> None:
    h = fr_holidays(year)
    assert h[paques] == "Lundi de Pâques"
    assert h[ascension] == "Ascension"
    assert h[pentecote] == "Lundi de Pentecôte"
    assert len(h) == 11


def test_easter_and_fixed_holidays() -> None:
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2027) == date(2027, 3, 28)
    assert easter(2028) == date(2028, 4, 16)
    h = fr_holidays(2026)
    for md in [(1, 1), (5, 1), (5, 8), (7, 14), (8, 15), (11, 1), (11, 11), (12, 25)]:
        assert date(2026, *md) in h


# --- échéances ----------------------------------------------------------------


def test_weekly_in_timezone_winter_and_summer() -> None:
    r = rec(days=[1], at="08:00")  # lundis
    assert between(r, utc(2026, 1, 1), utc(2026, 1, 8)) == [utc(2026, 1, 5, 7)]  # CET = UTC+1
    assert between(r, utc(2026, 7, 1), utc(2026, 7, 8)) == [utc(2026, 7, 6, 6)]  # CEST = UTC+2


def test_midnight_crosses_utc_date() -> None:
    r = rec(days=[4], at="00:00")  # jeudi 15 janvier 2026 00:00 Paris
    assert between(r, utc(2026, 1, 10), utc(2026, 1, 20)) == [utc(2026, 1, 14, 23)]


def test_other_timezone() -> None:
    r = rec(at="09:00", tz="America/New_York", days=[1])
    assert between(r, utc(2026, 1, 1), utc(2026, 1, 8)) == [utc(2026, 1, 5, 14)]


def test_dst_spring_gap_is_shifted_once() -> None:
    r = rec(at="02:30")  # 29 mars 2026 : 02:30 n'existe pas à Paris
    got = between(r, utc(2026, 3, 28, 12), utc(2026, 3, 30, 12))
    assert got == [utc(2026, 3, 29, 1, 30), utc(2026, 3, 30, 0, 30)]


def test_dst_autumn_duplicate_runs_once() -> None:
    r = rec(at="02:30")  # 25 octobre 2026 : 02:30 existe deux fois
    got = between(r, utc(2026, 10, 24, 12), utc(2026, 10, 26, 12))
    assert got == [utc(2026, 10, 25, 0, 30), utc(2026, 10, 26, 1, 30)]
    assert len(got) == 2


def test_interval_is_half_open() -> None:
    r = rec(days=[1], at="08:00")
    at = utc(2026, 1, 5, 7)
    assert between(r, at - timedelta(seconds=1), at) == [at]
    assert between(r, at, at + timedelta(minutes=1)) == []


def test_once_date_only_once() -> None:
    r = rec(days=[], once=date(2026, 6, 1), at="10:15")
    assert between(r, utc(2026, 5, 1), utc(2026, 7, 1)) == [utc(2026, 6, 1, 8, 15)]
    assert between(r, utc(2026, 6, 2), utc(2026, 8, 1)) == []


def test_next_occurrences_skips_closed_days() -> None:
    r = rec(days=[1, 2, 3, 4, 5], at="08:00")
    closed = closed_days([(date(2026, 1, 6), date(2026, 1, 7))], {date(2026, 1, 8): "x"})
    got = next_occurrences(r, utc(2026, 1, 5, 8), 3, closed)
    assert [x.astimezone(load_tz("Europe/Paris")).date() for x in got] == [
        date(2026, 1, 9),
        date(2026, 1, 12),
        date(2026, 1, 13),
    ]


def test_next_occurrences_horizon_and_empty() -> None:
    assert next_occurrences(rec(days=[], once=date(2020, 1, 1)), utc(2026, 1, 1), 10) == []
    far = next_occurrences(rec(days=[1]), utc(2026, 1, 1), 10)
    assert len(far) == 10


def test_invalid_timezone_rejected() -> None:
    for bad in ["Mars/Phobos", "../etc/passwd", "", "Europe/Paris\n"]:
        with pytest.raises(ValueError):
            load_tz(bad)
