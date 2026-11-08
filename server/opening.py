"""Ouverture de l'écoute d'Opal Box (inspiré de Cornelius) et mode privé.

Le bouton marche toujours. En plus, selon le mode, Opal écoute seule (micro en continu,
découpage des phrases par le volume) :

  bouton     jamais d'écoute automatique
  presence   dès que quelqu'un est devant la caméra
  connu      dès qu'un visage CONNU est devant la caméra
  regard     un visage connu qui regarde Opal (ou s'il n'y a aucun visage enregistré, n'importe qui)
  mot        quand on dit « Opal » (mot de réveil, voir wake.py)

Après une réponse, l'écoute reste ouverte FOLLOW_S secondes pour enchaîner sans rouvrir
(sauf en mode bouton). Pendant qu'Opal réfléchit ou parle, on n'écoute pas (sa propre voix).
Mode privé : rien n'est écouté ni analysé ; seul le bouton reste possible.
"""

from __future__ import annotations

import math
import struct
from collections import deque
from dataclasses import dataclass

MODES = ("bouton", "presence", "connu", "regard", "mot")
MODE_TEXT = {
    "bouton": "seulement quand tu appuies sur le bouton",
    "presence": "dès que quelqu'un est devant moi",
    "connu": "dès qu'un visage que je connais est devant moi",
    "regard": "quand tu me regardes",
    "mot": "quand tu dis « Opal »",
}
HOLD_S = 6.0     # l'écoute ouverte par un regard ou le mot reste ouverte ce temps
FOLLOW_S = 8.0   # après une réponse, pour enchaîner


@dataclass
class Inputs:
    present: bool = False
    identity: str | None = None   # nom, "" = inconnu, None = pas encore sûr
    knows_faces: bool = False     # au moins un visage enregistré
    looking: bool = False         # visage tourné vers la caméra, maintenant
    busy: bool = False            # Opal réfléchit ou parle


class Opening:
    def __init__(self, mode: str = "regard") -> None:
        self.mode = mode if mode in MODES else "regard"
        self.private = False
        self._opened_until = 0.0
        self._follow_until = 0.0

    def set_mode(self, mode: str) -> bool:
        if mode not in MODES:
            return False
        self.mode = mode
        self._opened_until = 0.0
        return True

    def wake_word(self, now: float) -> None:
        self._opened_until = max(self._opened_until, now + HOLD_S)

    def replied(self, now: float) -> None:
        """Opal vient de finir de répondre : on laisse la porte ouverte un moment."""
        if self.mode != "bouton":
            self._follow_until = now + FOLLOW_S

    def heard(self, now: float) -> None:
        """Une phrase vient d'être prise : prolonge l'ouverture le temps de la réponse."""
        self._opened_until = max(self._opened_until, now + HOLD_S)

    def is_open(self, s: Inputs, now: float) -> tuple[bool, str]:
        """(écoute ouverte ?, raison)."""
        if self.private:
            return False, "privé"
        if s.busy:
            return False, "occupé"
        if self.mode == "bouton":
            return False, "bouton"
        if now < self._follow_until and s.present:
            return True, "suite"
        if self.mode == "mot":
            return (now < self._opened_until), "mot"
        if not s.present:
            return False, "personne"
        known = bool(s.identity) or not s.knows_faces
        if self.mode == "presence":
            return True, "presence"
        if self.mode == "connu":
            return known, "connu" if known else "inconnu"
        # regard
        if known and s.looking:
            self._opened_until = now + HOLD_S
            return True, "regard"
        if known and now < self._opened_until:
            return True, "regard"
        return False, "attend_regard" if known else "inconnu"


def rms_db(pcm: bytes) -> float:
    """Volume d'un morceau PCM 16 bits mono, en dB par rapport au maximum."""
    n = len(pcm) // 2
    if n == 0:
        return -120.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    mean = sum(s * s for s in samples) / n
    return 10 * math.log10(mean / (32768.0**2)) if mean > 0 else -120.0


class Endpointer:
    """Découpe une phrase dans un flux micro continu, par le volume.

    Le bruit de fond est suivi lentement ; la parole commence quand le volume dépasse le fond
    de START_DB pendant START_MS, et finit après END_MS de calme. On garde PREROLL_MS avant le
    début pour ne pas couper la première syllabe.
    """

    START_DB = 12.0
    START_MS = 150
    END_MS = 800
    PREROLL_MS = 300
    MIN_FLOOR = -70.0

    def __init__(self, rate: int = 16000, max_s: float = 20.0) -> None:
        self.rate = rate
        self.max_bytes = int(max_s * rate * 2)
        self.floor = -55.0
        self.reset()

    def reset(self) -> None:
        self._pre: deque[bytes] = deque()
        self._pre_bytes = 0
        self._speech = bytearray()
        self._talking = False
        self._loud_ms = 0.0
        self._quiet_ms = 0.0

    @property
    def talking(self) -> bool:
        return self._talking

    def feed(self, pcm: bytes) -> bytes | None:
        """Ajoute un morceau ; renvoie la phrase complète quand elle se termine."""
        ms = len(pcm) / 2 / self.rate * 1000
        db = rms_db(pcm)
        loud = db > self.floor + self.START_DB
        if not self._talking:
            if not loud:  # fond qui bouge lentement (ventilateur, pièce)
                self.floor = max(self.MIN_FLOOR, 0.95 * self.floor + 0.05 * db)
            self._pre.append(pcm)
            self._pre_bytes += len(pcm)
            while self._pre_bytes > self.PREROLL_MS * self.rate * 2 / 1000 + len(pcm):
                self._pre_bytes -= len(self._pre.popleft())
            self._loud_ms = self._loud_ms + ms if loud else 0.0
            if self._loud_ms >= self.START_MS:
                self._talking = True
                self._quiet_ms = 0.0
                self._speech = bytearray(b"".join(self._pre))
            return None
        self._speech.extend(pcm)
        self._quiet_ms = 0.0 if loud else self._quiet_ms + ms
        if self._quiet_ms >= self.END_MS or len(self._speech) >= self.max_bytes:
            out = bytes(self._speech)
            self.reset()
            return out
        return None
