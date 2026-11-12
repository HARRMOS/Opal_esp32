"""Voix du pont : transcription locale (faster-whisper) et synthèse Edge en PCM."""

from __future__ import annotations

import asyncio
import os

import edge_tts
import miniaudio
import numpy as np

from logs import log
from player import RATE as AUDIO_RATE

MIC_RATE = 16_000
TTS_RATE = AUDIO_RATE  # même cadence que les sourates : le boîtier n'en change jamais


class Transcriber:
    """Parole → texte, en local sur le VPS (rien ne part vers un service tiers)."""

    def __init__(self) -> None:
        self._model_name = os.environ.get("OPAL_STT_MODEL", "small")
        self._language = os.environ.get("OPAL_STT_LANGUAGE", "fr")
        self._model = None
        self._lock = asyncio.Lock()

    def _load(self):  # type: ignore[no-untyped-def]
        if self._model is None:
            from faster_whisper import WhisperModel

            log("stt_loading", model=self._model_name)
            self._model = WhisperModel(
                self._model_name,
                device="cpu",
                compute_type="int8",
                download_root=os.environ.get("OPAL_MODELS_DIR"),
            )
            log("stt_ready", model=self._model_name)
        return self._model

    async def warm_up(self) -> None:
        await asyncio.to_thread(self._load)

    def _run(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        segments, _ = self._load().transcribe(
            audio, language=self._language, beam_size=1, vad_filter=True
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, pcm: bytes) -> str:
        async with self._lock:  # un seul calcul à la fois, le modèle est partagé
            return await asyncio.to_thread(self._run, pcm)


class Speaker:
    """Texte → PCM 16 bits mono (TTS_RATE).

    La voix d'Opal vient du cerveau (Google Chirp 3 HD, mots arabes prononcés en arabe) ; s'il ne
    répond pas, la voix Edge (Gérard) prend le relais."""

    def __init__(self) -> None:
        self._voice = os.environ.get("OPAL_TTS_VOICE", "fr-BE-GerardNeural")
        self._rate = os.environ.get("OPAL_TTS_RATE", "-6%")
        self._pitch = os.environ.get("OPAL_TTS_PITCH", "-6Hz")
        self.remote = None  # async (texte) -> PCM : la voix du cerveau (posée au démarrage)

    async def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            return b""
        if self.remote is not None:
            try:
                if pcm := await self.remote(text):
                    return pcm
            except Exception as exc:
                log("tts_remote_failed", error=str(exc)[:160])
        mp3 = bytearray()
        tts = edge_tts.Communicate(text, self._voice, rate=self._rate, pitch=self._pitch)
        async for chunk in tts.stream():
            if chunk["type"] == "audio":
                mp3.extend(chunk["data"])
        decoded = miniaudio.decode(
            bytes(mp3),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=TTS_RATE,
        )
        return decoded.samples.tobytes()
