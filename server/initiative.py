"""Initiatives d'Opal : ce que la boîte voit lui donne des raisons de parler toute seule.

Toutes les 30 s, on regarde la scène (qui est là, depuis quand, quelle heure) et on décide
s'il y a un moment qui mérite une phrase spontanée :

    pause         assis devant depuis longtemps sans vraie pause → proposer de se lever
    coucher       encore là tard dans la nuit → proposer d'aller dormir (une fois par nuit)
    pensee        de temps en temps, Opal regarde agenda / rappels / mails et ne parle que
                  s'il a quelque chose d'utile (sinon il répond RIEN et se tait)

L'arrivée (salutation, « bon retour », briefing du matin) reste dans opal_bridge.

Opal parle avec sa voix par l'iPhone / le Mac qui sert de caméra (page « Envoyer ma
caméra »), sinon par la boîte quand elle aura son haut-parleur. Au plus une initiative toutes les
OPAL_INITIATIVE_GAP_MIN minutes ; jamais pendant le sommeil de la boîte.

Réglages (minutes) : OPAL_BREAK_AFTER_MIN (55), OPAL_THOUGHT_EVERY_MIN (40, 0 = jamais), OPAL_BEDTIME (00:30).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime

from rhythm import DayRhythm

ABSENCE_RESET_S = 180  # une absence plus courte ne casse pas une « session » devant l'écran
SILENT = {"rien", "none", "rien.", "(rien)"}


def _minutes(name: str, default: float) -> float:
    return float(os.environ.get(name, default)) * 60


GAP_S = _minutes("OPAL_INITIATIVE_GAP_MIN", 10)
BREAK_AFTER_S = _minutes("OPAL_BREAK_AFTER_MIN", 55)
THOUGHT_EVERY_S = _minutes("OPAL_THOUGHT_EVERY_MIN", 40)
BEDTIME = dtime.fromisoformat(os.environ.get("OPAL_BEDTIME", "00:30"))

SPOKEN = (
    " Ta réponse sera dite à voix haute telle quelle : réponds seulement par ce que tu lui "
    "dis, une ou deux phrases courtes et naturelles, sans liste ni lien."
)
PROMPTS = {
    "pause": "[Opal Box] {name} est devant son écran depuis {sitting} sans vraie pause. "
             "Propose-lui gentiment de se lever, boire de l'eau ou s'étirer.",
    "coucher": "[Opal Box] Il est {hour} et {name} est encore devant son écran. "
               "Suggère-lui avec tact d'aller dormir.",
    "pensee": "[Opal Box] {name} est devant toi. Regarde son agenda des 2 prochaines heures, "
              "ses rappels et ses mails importants non lus. S'il y a quelque chose d'utile "
              "à lui signaler MAINTENANT, fais-le. Sinon réponds exactement RIEN, sans rien "
              "prononcer.",
}


@dataclass
class Scene:
    present: bool
    who: str | None  # prénom, "" inconnu, None pas encore identifié
    asleep: bool
    now: float = field(default_factory=time.time)


class Initiative:
    def __init__(self, rhythm: DayRhythm) -> None:
        self._rhythm = rhythm
        self._last_spoke = 0.0
        self._session_start = 0.0  # début de la présence actuelle (absences courtes ignorées)
        self._last_seen = 0.0
        self._last_break = 0.0  # dernière pause proposée ou vraie pause constatée
        self._last_thought = time.time()
        self._bed_night: date | None = None

    def observe(self, scene: Scene) -> str | None:
        """Met à jour l'état et rend le prompt à envoyer à Opal, ou None."""
        now = scene.now
        if scene.present:
            away = now - self._last_seen if self._last_seen else 0.0
            if away > ABSENCE_RESET_S or not self._session_start:
                self._session_start, self._last_break = now, now
            self._last_seen = now
        if not scene.present or not scene.who or scene.asleep:
            return None  # on ne parle qu'à quelqu'un de connu, boîte éveillée
        if now - self._last_spoke < GAP_S:
            return None
        name, local = scene.who, self._rhythm.now()
        prompt = None
        if night := self._bedtime_night(local):
            self._bed_night = night
            prompt = PROMPTS["coucher"].format(name=name, hour=local.strftime("%H h %M"))
        elif now - self._last_break > BREAK_AFTER_S:
            self._last_break = now
            prompt = PROMPTS["pause"].format(name=name, sitting=duration(now - self._session_start))
        elif THOUGHT_EVERY_S and now - self._last_thought > THOUGHT_EVERY_S:
            self._last_thought = now
            prompt = PROMPTS["pensee"].format(name=name)
        if prompt is None:
            return None
        self._last_spoke = now
        return prompt + SPOKEN

    def spoke(self) -> None:
        """Opal vient de parler (salutation, briefing) : l'initiative suivante attend."""
        self._last_spoke = time.time()

    def _bedtime_night(self, local: datetime) -> date | None:
        """La nuit (datée de son début) s'il est l'heure de proposer d'aller dormir, une fois."""
        night = local.date() if local.hour >= 12 else date.fromordinal(local.toordinal() - 1)
        t = local.time()
        if BEDTIME.hour >= 12:  # coucher avant minuit : jusqu'à 5 h le lendemain
            late = t >= BEDTIME or t < dtime(5, 0)
        else:
            late = BEDTIME <= t < dtime(5, 0)
        return night if late and self._bed_night != night else None


def is_silent(answer: str) -> bool:
    return answer.strip().strip("«»\"' ").lower() in SILENT


def duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minutes"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d}" if rest else f"{hours} heure" + ("s" if hours > 1 else "")
