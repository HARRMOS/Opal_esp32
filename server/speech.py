"""Texte d'Opal → phrases à prononcer, en une fois ou au fil de la génération."""

from __future__ import annotations

import re

MAX_SPOKEN_CHARS = 600

_MARKDOWN = re.compile(r"```[\s\S]*?```|`([^`]*)`|\*\*([^*]+)\*\*|^#{1,6}\s+|^\s*[-•*]\s+", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def speech_script(text: str) -> list[str]:
    """Réponse écrite → phrases à prononcer (sans markdown ni URLs, longueur bornée)."""
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _MARKDOWN.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_SPOKEN_CHARS:
        cut = text[:MAX_SPOKEN_CHARS]
        end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        text = cut[: end + 1] if end > 80 else cut + "…"
    return [s for s in _SENTENCE.split(text) if s.strip()]


class SpeechFeed:
    """Réponse reçue par fragments → phrases, rendues dès qu'elles sont complètes.

    La longueur totale reste bornée comme pour une réponse dite en une fois."""

    def __init__(self, limit: int = MAX_SPOKEN_CHARS) -> None:
        self._pending = ""
        self._left = limit

    def feed(self, text: str) -> list[str]:
        """Ajoute un fragment ; rend les phrases terminées (la dernière attend sa fin)."""
        self._pending += text
        parts = _SENTENCE.split(self._pending)
        self._pending = parts.pop()
        return self._clean(parts)

    def flush(self) -> list[str]:
        """Fin de la prise de parole : rend ce qui restait en attente."""
        rest, self._pending = self._pending, ""
        return self._clean([rest])

    def drop(self) -> None:
        """Abandonne le texte en attente (réponse remplacée par une autre)."""
        self._pending = ""

    def _clean(self, parts: list[str]) -> list[str]:
        out: list[str] = []
        for part in parts:
            for sentence in speech_script(part):
                if self._left <= 0:
                    return out
                out.append(sentence)
                self._left -= len(sentence)
        return out
