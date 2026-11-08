"""Tests — ouverture de l'écoute et découpage des phrases (opening.py)."""

import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opening import FOLLOW_S, HOLD_S, Endpointer, Inputs, Opening, rms_db  # noqa: E402


def test_bouton_never_opens() -> None:
    o = Opening("bouton")
    assert o.is_open(Inputs(present=True, identity="Harris", knows_faces=True, looking=True), 0)[0] is False
    o.replied(0)
    assert o.is_open(Inputs(present=True), 1)[0] is False


def test_regard_needs_known_face_looking_then_holds() -> None:
    o = Opening("regard")
    known = Inputs(present=True, identity="Harris", knows_faces=True)
    assert o.is_open(known, 0) == (False, "attend_regard")
    stranger = Inputs(present=True, identity="", knows_faces=True, looking=True)
    assert o.is_open(stranger, 0) == (False, "inconnu")
    looking = Inputs(present=True, identity="Harris", knows_faces=True, looking=True)
    assert o.is_open(looking, 10)[0]
    assert o.is_open(known, 10 + HOLD_S - 0.1)[0]   # détourne le regard : reste ouvert un moment
    assert not o.is_open(known, 10 + HOLD_S + 0.1)[0]


def test_regard_without_enrolled_faces_accepts_anyone() -> None:
    o = Opening("regard")
    assert o.is_open(Inputs(present=True, identity=None, knows_faces=False, looking=True), 0)[0]


def test_presence_and_connu() -> None:
    assert Opening("presence").is_open(Inputs(present=True, identity="", knows_faces=True), 0)[0]
    o = Opening("connu")
    assert not o.is_open(Inputs(present=True, identity="", knows_faces=True), 0)[0]
    assert o.is_open(Inputs(present=True, identity="Harris", knows_faces=True), 0)[0]
    assert not o.is_open(Inputs(present=False, identity="Harris", knows_faces=True), 0)[0]


def test_private_and_busy_close_everything() -> None:
    o = Opening("presence")
    s = Inputs(present=True)
    o.private = True
    assert o.is_open(s, 0) == (False, "privé")
    o.private = False
    assert o.is_open(Inputs(present=True, busy=True), 0) == (False, "occupé")


def test_follow_up_after_reply() -> None:
    o = Opening("regard")
    s = Inputs(present=True, identity="Harris", knows_faces=True, looking=False)
    o.replied(100)
    assert o.is_open(s, 100 + FOLLOW_S - 0.5) == (True, "suite")
    assert not o.is_open(s, 100 + FOLLOW_S + 0.5)[0]


def test_wake_word_mode() -> None:
    o = Opening("mot")
    assert not o.is_open(Inputs(present=True), 0)[0]
    o.wake_word(5)
    assert o.is_open(Inputs(present=False), 6)[0]
    assert not o.is_open(Inputs(present=False), 5 + HOLD_S + 0.1)[0]
    assert not o.set_mode("nimporte")


def _tone(ms: int, amp: int, rate: int = 16000) -> bytes:
    n = rate * ms // 1000
    return struct.pack(f"<{n}h", *(int(amp * math.sin(i / 3)) for i in range(n)))


def test_rms_db() -> None:
    assert rms_db(b"") == -120.0
    assert -10 < rms_db(_tone(100, 20000)) < 0


def test_endpointer_cuts_one_phrase() -> None:
    ep = Endpointer()
    chunk = 32  # ms
    out = []
    for _ in range(30):
        assert ep.feed(_tone(chunk, 40)) is None  # silence de la pièce
    for _ in range(20):
        r = ep.feed(_tone(chunk, 8000))
        assert r is None
    assert ep.talking
    for _ in range(40):
        if (r := ep.feed(_tone(chunk, 40))) is not None:
            out.append(r)
    assert len(out) == 1
    seconds = len(out[0]) / 2 / 16000
    assert 0.6 + 0.8 <= seconds + 0.1 and seconds < 2.0  # parole + fin + pré-enregistrement
    assert not ep.talking


def test_endpointer_ignores_short_click() -> None:
    ep = Endpointer()
    for _ in range(20):
        ep.feed(_tone(32, 40))
    ep.feed(_tone(32, 9000))  # un clic de 32 ms
    for _ in range(40):
        assert ep.feed(_tone(32, 40)) is None
    assert not ep.talking
