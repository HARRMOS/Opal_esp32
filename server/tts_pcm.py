"""Synthèse vocale vers PCM brut (16 bits mono, little-endian) pour l'ESP32.

Contrairement à `assistant.voice.tts`, rien n'est joué sur le PC : on retourne
les octets audio pour les envoyer au haut-parleur d'Opal.
"""

from __future__ import annotations

import asyncio
import platform
import tempfile
import wave
from pathlib import Path

import httpx

from assistant.config.settings import Settings
from assistant.core.logger import get_logger

logger = get_logger(__name__)

# OpenAI renvoie du PCM 24 kHz ; on aligne les voix natives sur la même fréquence.
SAMPLE_RATE = 24_000


class PcmSpeechEngine:
    """Texte → PCM 24 kHz. OpenAI si clé dispo, sinon voix système (SAPI / say)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        self._api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
        self._use_openai = (
            settings.tts_backend == "openai"
            and bool(self._api_key)
            and settings.llm_provider == "openai"
        )
        self._client = httpx.AsyncClient(timeout=90.0)
        self._system = platform.system()

    async def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            return b""
        if self._use_openai:
            try:
                return await self._openai(text)
            except Exception as exc:
                logger.warning("opal_tts_openai_fallback_native", error=str(exc))
        return await self._native(text)

    async def _openai(self, text: str) -> bytes:
        response = await self._client.post(
            f"{self._base_url}/audio/speech",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._settings.tts_openai_model,
                "input": text,
                "voice": self._settings.tts_openai_voice,
                "response_format": "pcm",
                "speed": self._settings.tts_openai_speed,
            },
        )
        response.raise_for_status()
        return response.content

    async def _native(self, text: str) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out = Path(tmp.name)
        try:
            if self._system == "Windows":
                await self._sapi_to_wav(text, out)
            elif self._system == "Darwin":
                await self._run(
                    "say",
                    "-v",
                    self._settings.tts_voice,
                    "-o",
                    str(out),
                    f"--data-format=LEI16@{SAMPLE_RATE}",
                    text,
                )
            else:
                raise NotImplementedError(f"TTS natif non supporté sur {self._system}")
            with wave.open(str(out), "rb") as wav:
                return wav.readframes(wav.getnframes())
        finally:
            out.unlink(missing_ok=True)

    async def _sapi_to_wav(self, text: str, out: Path) -> None:
        voice = self._settings.tts_voice.replace('"', '`"')
        path = str(out).replace('"', '`"')
        ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$preferred = @("{voice}", "Microsoft Paul", "Microsoft Henri", "Microsoft Julie", "Microsoft Hortense")
foreach ($name in $preferred) {{
  foreach ($v in $s.GetInstalledVoices()) {{
    if ($v.VoiceInfo.Name -eq $name) {{ $s.SelectVoice($name); break }}
  }}
}}
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({SAMPLE_RATE}, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile("{path}", $fmt)
$s.Speak([Console]::In.ReadToEnd())
$s.Dispose()
""".strip()
        await self._run("powershell", "-NoProfile", "-Command", ps, stdin=text)

    @staticmethod
    async def _run(*args: str, stdin: str | None = None) -> None:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(stdin.encode("utf-8") if stdin is not None else None)
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip())

    async def close(self) -> None:
        await self._client.aclose()
