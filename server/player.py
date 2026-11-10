"""Lecture d'un MP3 en ligne (sourate…) sur le haut-parleur d'Opal Box.

Le MP3 est téléchargé et décodé au fil de l'eau (miniaudio), puis envoyé au boîtier en PCM
16 bits mono 24 kHz, comme la voix d'Opal, au rythme de la lecture : le boîtier n'a jamais plus
de LEAD_S secondes d'avance, donc « stop » coupe presque aussitôt, et une sourate d'une heure
ne remplit pas sa mémoire.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Iterator

import httpx
import miniaudio
import numpy as np

from logs import log

RATE = 24_000
CHUNK = 4096        # octets par trame (comme la voix)
LEAD_S = 3.0        # avance maximale du boîtier sur la lecture
QUEUE_CHUNKS = 64   # ~5 s décodées d'avance côté pont
DEVICE_VOLUME = 70  # volume fixe du boîtier (SPK_VOLUME du firmware)


class HttpSource(miniaudio.StreamableSource):
    """Source miniaudio qui lit une réponse HTTP en flux (sans tout télécharger)."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buf = bytearray()

    def read(self, num_bytes: int) -> bytes:
        while len(self._buf) < num_bytes:
            try:
                self._buf.extend(next(self._chunks))
            except StopIteration:
                break
        out = bytes(self._buf[:num_bytes])
        del self._buf[:num_bytes]
        return out


def decode_stream(url: str, stop: threading.Event, emit: Callable[[bytes | None], None]) -> None:
    """Télécharge et décode `url` ; appelle emit(pcm) par morceau, puis emit(None)."""
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                source = HttpSource(resp.iter_bytes(32 * 1024))
                pcm = bytearray()
                for frames in miniaudio.stream_any(
                    source, source_format=miniaudio.FileFormat.MP3,
                    output_format=miniaudio.SampleFormat.SIGNED16,
                    nchannels=1, sample_rate=RATE, frames_to_read=CHUNK // 2,
                ):
                    if stop.is_set():
                        return
                    pcm.extend(frames.tobytes())
                    while len(pcm) >= CHUNK:
                        emit(bytes(pcm[:CHUNK]))
                        del pcm[:CHUNK]
                if pcm and not stop.is_set():
                    emit(bytes(pcm))
    except Exception as exc:
        log("player_failed", url=url[:120], error=str(exc)[:200])
    finally:
        emit(None)


class Player:
    """Une lecture à la fois, avec pause et volume ; envoie messages JSON et PCM aux boîtiers."""

    def __init__(self, send_json: Callable[[dict[str, object]], Awaitable[int]],
                 send_pcm: Callable[[bytes], Awaitable[int]],
                 on_change: Callable[[], Awaitable[None]] | None = None) -> None:
        self._send_json = send_json
        self._send_pcm = send_pcm
        self._on_change = on_change
        self._task: asyncio.Task[None] | None = None
        self._stop = threading.Event()
        self._running = asyncio.Event()   # posé = lecture, levé = pause
        self.title = ""
        self.volume = DEVICE_VOLUME     # % de la puissance de l'ampli (le boîtier joue à 70 %)
        self._played_s = 0.0

    @property
    def playing(self) -> bool:
        """Une lecture est en cours (même en pause)."""
        return self._task is not None and not self._task.done()

    @property
    def paused(self) -> bool:
        return self.playing and not self._running.is_set()

    def status(self) -> dict[str, object]:
        return {"playing": self.playing, "paused": self.paused, "volume": self.volume,
                "title": self.title if self.playing else "", "elapsed_s": round(self._played_s)}

    async def play(self, url: str, title: str = "") -> None:
        await self.stop()
        self._stop = threading.Event()
        self._running.set()
        self.title = title
        self._played_s = 0.0
        self._task = asyncio.create_task(self._run(url, self._stop))
        if self._on_change:
            await self._on_change()

    async def stop(self) -> bool:
        """Arrête la lecture en cours ; True s'il y en avait une."""
        task, self._task = self._task, None
        if task is None or task.done():
            return False
        self._stop.set()
        self._running.set()  # une lecture en pause doit pouvoir finir
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def pause(self, *, for_voice: bool = False) -> bool:
        """Met en pause ; True si une lecture vient d'être suspendue.

        Pour la voix (bouton, réponse d'Opal), le boîtier passe tout de suite à l'écoute ou à la
        parole : on ne lui renvoie pas « idle »."""
        if not self.playing or not self._running.is_set():
            return False
        self._running.clear()
        await self._send_json({"type": "audio_end"})  # sinon le boîtier jetterait la voix suivante
        if not for_voice:
            await self._send_json({"type": "state", "value": "idle"})
        if self._on_change:
            await self._on_change()
        return True

    async def resume(self) -> bool:
        if not self.paused:
            return False
        self._running.set()
        if self._on_change:
            await self._on_change()
        return True

    def set_volume(self, volume: int) -> int:
        self.volume = max(0, min(100, int(volume)))
        return self.volume

    def _scaled(self, chunk: bytes) -> bytes:
        if self.volume == DEVICE_VOLUME:
            return chunk
        gain = self.volume / DEVICE_VOLUME
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.int32) * gain
        return np.clip(samples, -32768, 32767).astype("<i2").tobytes()

    async def _run(self, url: str, stop: threading.Event) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(QUEUE_CHUNKS)

        def emit(chunk: bytes | None) -> None:  # fil de décodage : attend s'il a trop d'avance
            if stop.is_set() and chunk is not None:
                return
            try:
                asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            except Exception:  # boucle arrêtée, lecture annulée
                stop.set()

        loop.run_in_executor(None, decode_stream, url, stop, emit)  # s'arrête seul quand stop est posé
        streaming = False  # le boîtier a reçu audio_start (et pas encore audio_end)
        sent_s = 0.0
        t0 = 0.0
        log("player_start", title=self.title, url=url[:120])
        try:
            while (chunk := await queue.get()) is not None:
                if not self._running.is_set():
                    streaming = False  # pause : audio_end déjà envoyé
                    await self._running.wait()
                if not streaming:
                    streaming = True
                    t0 = time.monotonic() - sent_s  # le compte reprend où il s'était arrêté
                    # audio_end d'abord : un boîtier resté en « j'ignore la fin de la réponse
                    # interrompue » (bouton, pont redémarré) se remet à écouter le son.
                    await self._send_json({"type": "audio_end"})
                    await self._send_json({"type": "state", "value": "speaking"})
                    await self._send_json({"type": "audio_start", "rate": RATE})
                # Pas plus de LEAD_S secondes d'avance sur ce que le boîtier a joué.
                ahead = sent_s - (time.monotonic() - t0)
                if ahead > LEAD_S:
                    await asyncio.sleep(ahead - LEAD_S)
                if not await self._send_pcm(self._scaled(chunk)):
                    break  # plus aucun boîtier connecté
                sent_s += len(chunk) / (RATE * 2)
                self._played_s = max(0.0, sent_s - LEAD_S)
            log("player_end", title=self.title, seconds=round(sent_s))
        finally:
            stop.set()
            while not queue.empty():  # débloque le fil de décodage s'il attendait
                queue.get_nowait()
            if streaming:
                await self._send_json({"type": "audio_end"})
                await self._send_json({"type": "state", "value": "idle"})
            if self._on_change:
                await self._on_change()
