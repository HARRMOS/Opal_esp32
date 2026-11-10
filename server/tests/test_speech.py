"""Tests — découpe en phrases (speech.py) et réponse dite au fil de la génération."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speech import SpeechFeed, speech_script  # noqa: E402


def test_feed_gives_sentences_as_soon_as_they_end() -> None:
    feed = SpeechFeed()
    assert feed.feed("Il fait douze") == []
    assert feed.feed(" degrés. Prends une ") == ["Il fait douze degrés."]
    assert feed.feed("veste !") == []          # la dernière phrase attend sa fin
    assert feed.flush() == ["Prends une veste !"]
    assert feed.flush() == []


def test_feed_matches_the_one_shot_script() -> None:
    text = "Voici **trois** points. D'abord [le site](https://exemple.fr) ! Ensuite, `rien`. Fin"
    feed = SpeechFeed()
    said: list[str] = []
    for i in range(0, len(text), 5):
        said += feed.feed(text[i : i + 5])
    said += feed.flush()
    assert said == speech_script(text)


def test_feed_keeps_decimals_together_and_bounds_length() -> None:
    feed = SpeechFeed(limit=30)
    assert feed.feed("Ça coûte 3.50 euros. ") == ["Ça coûte 3.50 euros."]
    assert feed.feed("Une deuxième phrase. Une troisième. ") == ["Une deuxième phrase."]
    assert feed.feed("Une quatrième. ") == []  # limite atteinte : Opal s'arrête là


def test_drop_forgets_the_pending_text() -> None:
    feed = SpeechFeed()
    feed.feed("Je cherche ton tr")
    feed.drop()
    assert feed.flush() == []


# ── Pont : la voix part avant la fin de la réponse ──
opal_bridge = pytest.importorskip("opal_bridge")


class _Iris:
    def __init__(self, events: list[dict], gate: asyncio.Event | None = None, fail: bool = False) -> None:
        self._events, self._gate, self._fail = events, gate, fail

    async def ask_stream(self, text: str):  # type: ignore[no-untyped-def]
        for event in self._events:
            if event.get("type") == "answer" and self._gate is not None:
                await self._gate.wait()  # le modèle n'a pas fini d'écrire
            yield event
        if self._fail:
            raise RuntimeError("cerveau injoignable")


class _Tts:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.said.append(text)
        return b"\x00\x00" * 240  # 10 ms de voix


class _Box:
    """Boîtier factice : garde ce que le pont lui envoie."""

    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.audio = 0
        self.first_audio = asyncio.Event()

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            self.audio += len(message)
            self.first_audio.set()
        else:
            self.texts.append(json.loads(message))


def _bridge(iris: _Iris) -> tuple[object, _Tts]:
    # Sans le constructeur : il charge les modèles de la caméra, absents hors du conteneur.
    tts = _Tts()
    bridge = object.__new__(opal_bridge.OpalBridge)
    bridge._iris, bridge._tts, bridge._play_until = iris, tts, 0.0
    return bridge, tts


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_first_sentence_is_spoken_before_the_answer_is_complete() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        answer = "Il fait douze degrés. Prends une veste."
        iris = _Iris(
            [
                {"type": "delta", "text": "Il fait douze degrés. Pre"},
                {"type": "delta", "text": "nds une veste."},
                {"type": "answer", "text": answer, "streamed": True},
            ],
            gate,
        )
        bridge, tts = _bridge(iris)
        box = _Box()
        reply = asyncio.create_task(bridge._reply(box, "météo ?"))
        await asyncio.wait_for(box.first_audio.wait(), 1)  # le modèle écrit encore
        assert tts.said == ["Il fait douze degrés."]
        gate.set()
        assert await reply == answer
        assert tts.said == ["Il fait douze degrés.", "Prends une veste."]
        kinds = [m["type"] for m in box.texts]
        assert kinds == ["state", "audio_start", "audio_end"] and box.audio == 2 * 480

    _run(scenario())


def test_answer_not_streamed_is_spoken_whole() -> None:
    async def scenario() -> None:
        iris = _Iris([{"type": "answer", "text": "C'est fait. La lumière est éteinte.", "streamed": False}])
        bridge, tts = _bridge(iris)
        assert await bridge._reply(_Box(), "éteins") == "C'est fait. La lumière est éteinte."
        assert tts.said == ["C'est fait.", "La lumière est éteinte."]

    _run(scenario())


def test_replaced_answer_drops_the_unfinished_sentence() -> None:
    async def scenario() -> None:
        iris = _Iris(
            [
                {"type": "delta", "text": "Je regarde. Ton train part à"},
                {"type": "answer", "text": "Limite atteinte. Réessaie.", "streamed": False},
            ]
        )
        bridge, tts = _bridge(iris)
        await bridge._reply(_Box(), "train ?")
        assert tts.said == ["Je regarde.", "Limite atteinte.", "Réessaie."]

    _run(scenario())


def test_break_ends_a_sentence_between_two_generations() -> None:
    async def scenario() -> None:
        iris = _Iris(
            [
                {"type": "delta", "text": "Je regarde ton agenda"},
                {"type": "break"},
                {"type": "delta", "text": "Tu as un rendez-vous à neuf heures."},
                {"type": "answer", "text": "Tu as un rendez-vous à neuf heures.", "streamed": True},
            ]
        )
        bridge, tts = _bridge(iris)
        await bridge._reply(_Box(), "agenda ?")
        assert tts.said == ["Je regarde ton agenda", "Tu as un rendez-vous à neuf heures."]

    _run(scenario())


def test_unreachable_brain_says_so() -> None:
    async def scenario() -> None:
        bridge, tts = _bridge(_Iris([], fail=True))
        answer = await bridge._reply(_Box(), "bonjour")
        assert answer == opal_bridge.UNREACHABLE_ANSWER and tts.said == [answer]

    _run(scenario())


def test_playback_end_accounts_for_every_sentence() -> None:
    async def scenario() -> None:
        iris = _Iris([{"type": "answer", "text": "Un. Deux. Trois.", "streamed": False}])
        bridge, _tts = _bridge(iris)
        before = asyncio.get_running_loop().time()
        await bridge._reply(_Box(), "compte")
        import time

        assert bridge._play_until - time.monotonic() > 0.02  # 3 × 10 ms, moins le temps d'envoi
        assert asyncio.get_running_loop().time() - before < 1

    _run(scenario())
