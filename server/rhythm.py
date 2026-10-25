"""Rythme de la journée d'Opal Box : nuit (sommeil) et matin (briefing).

Plages réglables (heure locale, OPAL_TZ, défaut Europe/Paris) :
    OPAL_NIGHT    défaut 23:00-07:00  la boîte dort si personne n'est là
    OPAL_MORNING  défaut 05:00-12:00  première arrivée du jour = briefing
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


def _parse(span: str) -> tuple[time, time]:
    start, end = span.split("-")
    return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())


def _inside(now: time, span: tuple[time, time]) -> bool:
    start, end = span
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # plage qui passe minuit


class DayRhythm:
    def __init__(self) -> None:
        self.tz = ZoneInfo(os.environ.get("OPAL_TZ", "Europe/Paris"))
        self.night = _parse(os.environ.get("OPAL_NIGHT", "23:00-07:00"))
        self.morning = _parse(os.environ.get("OPAL_MORNING", "05:00-12:00"))

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def is_night(self, now: datetime | None = None) -> bool:
        return _inside((now or self.now()).time(), self.night)

    def is_morning(self, now: datetime | None = None) -> bool:
        return _inside((now or self.now()).time(), self.morning)

    def today(self) -> date:
        return self.now().date()
