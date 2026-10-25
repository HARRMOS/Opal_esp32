"""Coach de posture : trop près de l'écran, ou avachi, par rapport à TA position habituelle.

Pas de seuil absolu (la caméra peut être n'importe où) : on apprend ta position normale
(médiane des mesures des 30 dernières minutes) et on compare les 3 dernières minutes.

    proche  le visage paraît 35 % plus grand que d'habitude → tu t'es rapproché de l'écran
    voute   la tête est descendue vers les épaules (cou « écrasé ») de 30 % → dos voûté
"""

from __future__ import annotations

import time
from collections import deque
from statistics import median
from typing import Any

HABIT_S = 30 * 60
RECENT_S = 3 * 60
MIN_HABIT_SAMPLES = 120  # ~10 min de mesures avant de juger
CLOSER = 1.35
SLUMP = 0.70
ADVICE_EVERY_S = 30 * 60

PROMPTS = {
    "proche": "[Opal Box] {name} s'est beaucoup rapproché de son écran depuis quelques "
              "minutes. Conseille-lui de reculer un peu pour ses yeux.",
    "voute": "[Opal Box] {name} est avachi devant son écran depuis quelques minutes. "
             "Rappelle-lui gentiment de se redresser.",
}


class PostureCoach:
    def __init__(self) -> None:
        self._size: deque[tuple[float, float]] = deque()  # (heure, taille du visage)
        self._neck: deque[tuple[float, float]] = deque()  # (heure, hauteur du cou / épaules)
        self._advised: dict[str, float] = {}
        self._last_sample = 0.0

    def feed(self, seen: dict[str, Any], now: float | None = None) -> str | None:
        """Ajoute une mesure (au plus 1 toutes les 5 s) ; rend "proche", "voute" ou None."""
        now = now or time.time()
        if now - self._last_sample < 5:
            return None
        face, arms = seen.get("face"), seen.get("arms")
        if not face or now - face.get("at", 0) > 2:
            return None
        self._last_sample = now
        _, y, w, h = face["box"]
        self._push(self._size, now, w)
        if arms and now - arms.get("at", 0) < 2:
            (lx, ly, lv), (rx, ry, rv) = arms["arms"][0], arms["arms"][1]
            shoulders = abs(lx - rx)
            if lv > 0.6 and rv > 0.6 and shoulders > 0.05:
                # Écart vertical menton → épaules, rapporté à leur largeur.
                self._push(self._neck, now, ((ly + ry) / 2 - (y + h)) / shoulders)
        for kind, series, worse in (("proche", self._size, lambda r: r > CLOSER),
                                    ("voute", self._neck, lambda r: r < SLUMP)):
            ratio = self._ratio(series, now)
            if ratio is not None and worse(ratio) and now - self._advised.get(kind, 0) > ADVICE_EVERY_S:
                self._advised[kind] = now
                return kind
        return None

    @staticmethod
    def _push(series: deque[tuple[float, float]], now: float, value: float) -> None:
        series.append((now, value))
        while series and now - series[0][0] > HABIT_S:
            series.popleft()

    @staticmethod
    def _ratio(series: deque[tuple[float, float]], now: float) -> float | None:
        """Mesure récente / habitude (toutes deux en médianes)."""
        if len(series) < MIN_HABIT_SAMPLES:
            return None
        recent = [v for t, v in series if now - t < RECENT_S]
        habit = [v for t, v in series if now - t >= RECENT_S]
        if len(recent) < 20 or len(habit) < MIN_HABIT_SAMPLES // 2:
            return None
        base = median(habit)
        return median(recent) / base if base > 0 else None
