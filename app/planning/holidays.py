"""Jours fériés français métropolitains, calculés (aucune table, aucune dépendance)."""

from datetime import date, timedelta


def easter(year: int) -> date:
    """Dimanche de Pâques (algorithme grégorien anonyme, Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def fr_holidays(year: int) -> dict[date, str]:
    p = easter(year)
    return {
        date(year, 1, 1): "Jour de l'an",
        p + timedelta(days=1): "Lundi de Pâques",
        date(year, 5, 1): "Fête du travail",
        date(year, 5, 8): "Victoire 1945",
        p + timedelta(days=39): "Ascension",
        p + timedelta(days=50): "Lundi de Pentecôte",
        date(year, 7, 14): "Fête nationale",
        date(year, 8, 15): "Assomption",
        date(year, 11, 1): "Toussaint",
        date(year, 11, 11): "Armistice 1918",
        date(year, 12, 25): "Noël",
    }


def holiday_label(d: date) -> str | None:
    return fr_holidays(d.year).get(d)
