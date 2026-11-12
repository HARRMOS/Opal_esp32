"""Mot de réveil « Opal » (mode d'écoute « mot », voir opening.py).

Pas de détecteur dédié : en mode « mot », le boîtier envoie le micro en continu (hors parole
d'Opal) ; chaque phrase est découpée par le volume, puis seules ses HEAD_S premières secondes sont
transcrites (Whisper, déjà chargé pour la voix). Si elles commencent par « Opal », la phrase entière
est traitée ; sinon elle est ignorée sans trace (ni réponse, ni texte dans les journaux).

Whisper écrit « Opal » de bien des façons : « Opale », « Au pal », « O pal », « Hopal »… On compare
donc les premiers mots, seuls ou deux par deux, au mot attendu.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

HEAD_S = 2.0             # secondes transcrites pour chercher le mot
_LEAD = {"hey", "he", "eh", "ok", "okay", "dis", "salut", "bonjour", "oh", "ho", "allo", "allô"}
_EXACT = {"opal", "opale", "opalle", "opall", "hopal", "hopale", "oppal", "aupal", "aupale", "ohpal",
          "opaal"}
_WORD = re.compile(r"[\w']+")


def _norm(word: str) -> str:
    word = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in word if c.isalnum() and not unicodedata.combining(c))


def _is_wake(token: str) -> bool:
    if token in _EXACT:
        return True
    return 4 <= len(token) <= 7 and difflib.SequenceMatcher(None, token, "opal").ratio() >= 0.8


def split_wake(text: str) -> tuple[bool, str]:
    """(la phrase commence-t-elle par « Opal » ?, la demande qui suit)."""
    words = list(_WORD.finditer(text or ""))
    i = 0
    while i < len(words) and i < 2 and _norm(words[i].group()) in _LEAD:
        i += 1  # « Hey Opal », « Dis Opal »
    for span in (1, 2):  # « Opal », ou « Au pal » en deux mots
        if i + span > len(words):
            break
        token = "".join(_norm(w.group()) for w in words[i:i + span])
        if _is_wake(token):
            rest = text[words[i + span - 1].end():]
            return True, rest.lstrip(" ,.;:!?-–—…").strip()
    return False, text
