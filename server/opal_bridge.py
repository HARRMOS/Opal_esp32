"""Pont WebSocket entre Opal Box (ESP32-S3) et Opal sur le VPS.

Le pont ne contient pas de cerveau : il transcrit la voix, envoie le texte à
l'API d'Opal (POST /api/command, la même conversation que sur le PC et le Mac),
puis renvoie la réponse en voix vers le haut-parleur de l'ESP32.

Authentification : l'ESP32 envoie « Authorization: Bearer <jeton> » pendant la
poignée de main ; le jeton attendu est OPAL_DEVICE_TOKEN.

Protocole (un client = un boîtier) :

  ESP32 → VPS
    texte  {"type": "hello", "device": "opal", "mic_rate": 16000}
    texte  {"type": "start"}              début d'une prise de parole (bouton appuyé)
    binaire PCM 16 bits mono 16 kHz       audio micro, par morceaux
    texte  {"type": "stop"}               fin de la prise de parole (bouton relâché)

  VPS → ESP32
    texte  {"type": "state", "value": "listening|thinking|speaking|idle"}
    texte  {"type": "transcript", "text": "..."}
    texte  {"type": "reply", "text": "..."}
    texte  {"type": "audio_start", "rate": 24000}
    binaire PCM 16 bits mono 24 kHz       voix d'Opal
    texte  {"type": "audio_end"}
    texte  {"type": "error", "message": "..."}

Variables d'environnement :
    OPAL_DEVICE_TOKEN   jeton partagé avec le firmware (obligatoire)
    IRIS_URL            API d'Opal (défaut http://iris:8765, réseau Docker)
    IRIS_TOKEN          jeton distant d'Opal, ou IRIS_TOKEN_FILE (défaut /run/secrets/iris_token)
    OPAL_STT_MODEL      modèle faster-whisper (défaut small)
    OPAL_TTS_VOICE / OPAL_TTS_RATE / OPAL_TTS_PITCH   voix Edge
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import sys
from http import HTTPStatus
from pathlib import Path

import httpx
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from logs import log
from voice import MIC_RATE, TTS_RATE, Speaker, Transcriber

MAX_UTTERANCE_S = 20
MIN_UTTERANCE_S = 0.3
AUDIO_CHUNK = 4096  # octets par trame binaire envoyée à l'ESP32
MAX_SPOKEN_CHARS = 600
IRIS_TIMEOUT_S = 180

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


class IrisClient:
    """Appelle l'API d'Opal comme le ferait un raccourci iOS."""

    def __init__(self, url: str, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=IRIS_TIMEOUT_S + 10,
        )

    async def ask(self, text: str) -> str:
        response = await self._client.post(
            "/api/command", json={"text": text, "timeout": IRIS_TIMEOUT_S}
        )
        if response.status_code == HTTPStatus.ACCEPTED:
            return "C'est long, je continue en arrière-plan et je te tiens au courant."
        response.raise_for_status()
        return str(response.json().get("answer") or "")

    async def close(self) -> None:
        await self._client.aclose()


class OpalBridge:
    def __init__(
        self, iris: IrisClient, stt: Transcriber, tts: Speaker, device_token: str
    ) -> None:
        self._iris = iris
        self._stt = stt
        self._tts = tts
        self._token = device_token

    def check_auth(self, ws: ServerConnection, request: Request) -> Response | None:
        """Refuse la poignée de main si le jeton est absent ou faux."""
        header = request.headers.get("Authorization", "")
        given = header.removeprefix("Bearer ").strip()
        if hmac.compare_digest(given.encode(), self._token.encode()):
            return None
        log("auth_refused", peer=str(ws.remote_address))
        return ws.respond(HTTPStatus.UNAUTHORIZED, "Jeton invalide\n")

    async def handle(self, ws: ServerConnection) -> None:
        peer = str(ws.remote_address)
        log("device_connected", peer=peer)
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
                    log("device_hello", **{k: v for k, v in msg.items() if k != "type"})
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
            log("device_disconnected", peer=peer)

    async def _handle_utterance(self, ws: ServerConnection, pcm: bytes) -> None:
        if len(pcm) < MIN_UTTERANCE_S * MIC_RATE * 2:
            await self._send(ws, type="state", value="idle")
            return

        await self._send(ws, type="state", value="thinking")
        text = ""
        try:
            text = await self._stt.transcribe(pcm)
        except Exception as exc:
            log("stt_failed", error=str(exc))
        if not text:
            await self._send(ws, type="error", message="Je n'ai rien compris.")
            await self._send(ws, type="state", value="idle")
            return

        await self._send(ws, type="transcript", text=text)
        log("transcript", text=text)
        try:
            answer = await self._iris.ask(text)
        except Exception as exc:
            log("iris_failed", error=str(exc))
            answer = "Je n'arrive pas à joindre mon cerveau pour l'instant."

        await self._send(ws, type="reply", text=answer)
        await self._speak(ws, answer)
        await self._send(ws, type="state", value="idle")

    async def _speak(self, ws: ServerConnection, text: str) -> None:
        sentences = speech_script(text)
        if not sentences:
            return
        await self._send(ws, type="state", value="speaking")
        await self._send(ws, type="audio_start", rate=TTS_RATE)
        # Synthèse de la phrase i+1 pendant l'envoi de la phrase i.
        pending = asyncio.create_task(self._tts.synthesize(sentences[0]))
        for i in range(len(sentences)):
            try:
                pcm = await pending
            except Exception as exc:
                log("tts_failed", error=str(exc))
                break
            if i + 1 < len(sentences):
                pending = asyncio.create_task(self._tts.synthesize(sentences[i + 1]))
            for off in range(0, len(pcm), AUDIO_CHUNK):
                await ws.send(pcm[off : off + AUDIO_CHUNK])
        await self._send(ws, type="audio_end")

    @staticmethod
    async def _send(ws: ServerConnection, **payload: object) -> None:
        await ws.send(json.dumps(payload, ensure_ascii=False))


def iris_token() -> str:
    token = os.environ.get("IRIS_TOKEN", "").strip()
    if token:
        return token
    path = Path(os.environ.get("IRIS_TOKEN_FILE", "/run/secrets/iris_token"))
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


async def run(host: str, port: int, device_token: str) -> None:
    iris = IrisClient(os.environ.get("IRIS_URL", "http://iris:8765"), iris_token())
    stt = Transcriber()
    bridge = OpalBridge(iris, stt, Speaker(), device_token)
    await stt.warm_up()
    try:
        async with serve(
            bridge.handle,
            host,
            port,
            process_request=bridge.check_auth,
            max_size=None,
            ping_interval=20,
        ):
            log("bridge_ready", url=f"ws://{host}:{port}")
            await asyncio.Future()
    finally:
        await iris.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Pont WebSocket Opal Box ↔ Opal")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token", default=os.environ.get("OPAL_DEVICE_TOKEN", ""))
    args = parser.parse_args()
    if len(args.token) < 16:
        sys.exit(
            "OPAL_DEVICE_TOKEN manquant ou trop court (16 caractères min). Génère-en un :\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if not iris_token():
        sys.exit("Jeton d'Opal introuvable : définir IRIS_TOKEN ou monter IRIS_TOKEN_FILE.")
    try:
        asyncio.run(run(args.host, args.port, args.token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
