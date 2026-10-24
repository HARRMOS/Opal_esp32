"""Pont WebSocket entre Opal (ESP32-S3) et le cerveau Iris.

Authentification : l'ESP32 envoie l'en-tête « Authorization: Bearer <jeton> »
pendant la poignée de main ; le jeton attendu vient de OPAL_DEVICE_TOKEN
(ou --token). Sur le VPS, le pont écoute en local derrière Caddy (wss://).

Protocole (un client = un boîtier Opal) :

  ESP32 → PC
    texte  {"type": "hello", "device": "opal", "mic_rate": 16000}
    texte  {"type": "start"}              début d'une prise de parole (bouton appuyé)
    binaire PCM 16 bits mono 16 kHz       audio micro, par morceaux
    texte  {"type": "stop"}               fin de la prise de parole (bouton relâché)

  PC → ESP32
    texte  {"type": "state", "value": "listening|thinking|speaking|idle"}
    texte  {"type": "transcript", "text": "..."}
    texte  {"type": "reply", "text": "..."}
    texte  {"type": "audio_start", "rate": 24000}
    binaire PCM 16 bits mono 24 kHz       voix d'Iris
    texte  {"type": "audio_end"}
    texte  {"type": "error", "message": "..."}

Lancement (depuis le venv d'Iris, avec le .env d'Iris) :
    OPAL_DEVICE_TOKEN=... python server/opal_bridge.py --iris-dir ../Iris
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
from http import HTTPStatus
import json
import os
import sys
import tempfile
import wave
from pathlib import Path
from uuid import uuid4

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from assistant.brain import Brain, create_llm_client
from assistant.browser import close_browser_manager
from assistant.config.settings import get_settings
from assistant.core.context import AgentContext
from assistant.core.logger import get_logger, setup_logging
from assistant.core.types import AgentState
from assistant.memory import create_memory_store
from assistant.tools.bootstrap import register_all_tools_async, shutdown_tools
from assistant.voice.sentences import split_sentences
from assistant.voice.service import create_voice_service
from assistant.voice.speech_text import prepare_text_for_speech

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tts_pcm import SAMPLE_RATE as TTS_RATE, PcmSpeechEngine  # noqa: E402

logger = get_logger(__name__)

MIC_RATE = 16_000
MAX_UTTERANCE_S = 20
MIN_UTTERANCE_S = 0.3
AUDIO_CHUNK = 4096  # octets par trame binaire envoyée à l'ESP32


class OpalBridge:
    def __init__(self, brain: Brain, tts: PcmSpeechEngine, token: str) -> None:
        self._brain = brain
        self._tts = tts
        self._token = token
        self._voice = create_voice_service()
        self._settings = get_settings()
        # Le cerveau garde un historique unique : une requête à la fois.
        self._lock = asyncio.Lock()

    def check_auth(self, ws: ServerConnection, request: Request) -> Response | None:
        """Refuse la poignée de main si le jeton est absent ou faux."""
        header = request.headers.get("Authorization", "")
        given = header.removeprefix("Bearer ").strip()
        if hmac.compare_digest(given.encode(), self._token.encode()):
            return None
        logger.warning("opal_auth_refused", peer=str(ws.remote_address))
        return ws.respond(HTTPStatus.UNAUTHORIZED, "Jeton invalide\n")

    async def handle(self, ws: ServerConnection) -> None:
        peer = ws.remote_address
        logger.info("opal_connected", peer=str(peer))
        audio = bytearray()
        recording = False
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    if recording and len(audio) < MAX_UTTERANCE_S * MIC_RATE * 2:
                        audio.extend(message)
                    continue

                msg = json.loads(message)
                kind = msg.get("type")
                if kind == "hello":
                    logger.info("opal_hello", **{k: v for k, v in msg.items() if k != "type"})
                    await self._send(ws, type="state", value="idle")
                elif kind == "start":
                    audio.clear()
                    recording = True
                    await self._send(ws, type="state", value="listening")
                elif kind == "stop" and recording:
                    recording = False
                    pcm = bytes(audio)
                    audio.clear()
                    await self._handle_utterance(ws, pcm)
        except websockets.ConnectionClosed:
            pass
        finally:
            logger.info("opal_disconnected", peer=str(peer))

    async def _handle_utterance(self, ws: ServerConnection, pcm: bytes) -> None:
        if len(pcm) < MIN_UTTERANCE_S * MIC_RATE * 2:
            await self._send(ws, type="state", value="idle")
            return

        await self._send(ws, type="state", value="thinking")
        try:
            text = await self._voice.transcribe_file(self._write_wav(pcm))
        except Exception as exc:
            logger.warning("opal_stt_failed", error=str(exc))
            await self._send(ws, type="error", message="Je n'ai rien compris.")
            await self._send(ws, type="state", value="idle")
            return

        await self._send(ws, type="transcript", text=text)
        logger.info("opal_transcript", text=text)

        async with self._lock:
            try:
                answer = await self._brain.process(text)
            except Exception as exc:
                logger.exception("opal_brain_failed")
                answer = f"Désolé, une erreur est survenue : {exc}"

        await self._send(ws, type="reply", text=answer)
        await self._speak(ws, answer)
        await self._send(ws, type="state", value="idle")

    async def _speak(self, ws: ServerConnection, text: str) -> None:
        script = prepare_text_for_speech(text, max_chars=self._settings.tts_max_speech_chars)
        sentences = split_sentences(script) or [script]
        await self._send(ws, type="state", value="speaking")
        await self._send(ws, type="audio_start", rate=TTS_RATE)
        # Synthèse de la phrase i+1 pendant l'envoi de la phrase i.
        pending = asyncio.create_task(self._tts.synthesize(sentences[0]))
        for i in range(len(sentences)):
            try:
                pcm = await pending
            except Exception as exc:
                logger.warning("opal_tts_failed", error=str(exc))
                break
            if i + 1 < len(sentences):
                pending = asyncio.create_task(self._tts.synthesize(sentences[i + 1]))
            for off in range(0, len(pcm), AUDIO_CHUNK):
                await ws.send(pcm[off : off + AUDIO_CHUNK])
        await self._send(ws, type="audio_end")

    def _write_wav(self, pcm: bytes) -> Path:
        audio_dir = self._voice.audio_dir
        audio_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".wav", prefix="opal_", dir=audio_dir, delete=False
        ) as tmp:
            path = Path(tmp.name)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(MIC_RATE)
            wav.writeframes(pcm)
        return path

    @staticmethod
    async def _send(ws: ServerConnection, **payload: object) -> None:
        await ws.send(json.dumps(payload, ensure_ascii=False))


async def run(host: str, port: int, token: str) -> None:
    settings = get_settings()
    setup_logging(settings)

    ctx = AgentContext(settings=settings, session_id=str(uuid4()), state=AgentState.IDLE)
    memory = create_memory_store(settings)
    ctx.memory = memory
    tts = PcmSpeechEngine(settings)

    async with create_llm_client(settings) as llm:
        await register_all_tools_async(ctx, settings, memory, ctx.session_id, llm=llm)
        bridge = OpalBridge(Brain(ctx, llm), tts, token)
        await bridge._voice.warm_up_stt()
        try:
            async with serve(
                bridge.handle,
                host,
                port,
                process_request=bridge.check_auth,
                max_size=None,
                ping_interval=20,
            ):
                print(f"  Opal bridge prêt sur ws://{host}:{port}  (Ctrl+C pour quitter)")
                await asyncio.Future()
        finally:
            await tts.close()
            await shutdown_tools(ctx)
            await close_browser_manager()
            memory.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Pont WebSocket Opal ESP32 ↔ Iris")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="127.0.0.1 derrière Caddy (VPS) ; 0.0.0.0 pour un usage direct en réseau local",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--iris-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "Iris",
        help="Dossier d'Iris (pour charger son .env et ses données)",
    )
    parser.add_argument("--token", default=os.environ.get("OPAL_DEVICE_TOKEN", ""))
    args = parser.parse_args()
    if len(args.token) < 16:
        sys.exit(
            "OPAL_DEVICE_TOKEN manquant ou trop court (16 caractères min). Génère-en un :\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    # Iris lit son .env et data/ relativement au dossier courant.
    os.chdir(args.iris_dir)
    try:
        asyncio.run(run(args.host, args.port, args.token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
