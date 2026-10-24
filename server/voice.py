"""Voix du pont : transcription locale (faster-whisper) et synthèse Edge en PCM."""

from __future__ import annotations

import asyncio
import os

import edge_tts
import miniaudio
import numpy as np

from logs import log

MIC_RATE = 16_000
TTS_RATE = 24_000


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
    """Texte → PCM 16 bits mono 24 kHz, avec la voix Edge d'Opal."""

    def __init__(self) -> None:
        self._voice = os.environ.get("OPAL_TTS_VOICE", "fr-FR-RemyMultilingualNeural")
        self._rate = os.environ.get("OPAL_TTS_RATE", "-10%")
        self._pitch = os.environ.get("OPAL_TTS_PITCH", "+0Hz")

    async def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            return b""
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
