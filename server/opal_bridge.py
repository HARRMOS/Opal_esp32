"""Pont WebSocket entre Opal Box (ESP32-S3 + ESP32-CAM) et Opal sur le VPS.

Le pont ne contient pas de cerveau : il transcrit la voix, envoie le texte à
l'API d'Opal (POST /api/command, la même conversation que sur le PC et le Mac),
puis renvoie la réponse en voix vers le haut-parleur de l'ESP32.

Tant qu'un appareil est connecté, le pont se présente aussi à Opal comme un nœud
(« opal-box », voir node.py) avec les outils yeux, photo et présence. La caméra
(camera.py) fait suivre le visage aux yeux et signale arrivées et départs.

Authentification : chaque ESP32 envoie « Authorization: Bearer <jeton> » pendant
la poignée de main ; le jeton attendu est OPAL_DEVICE_TOKEN.

Protocole du boîtier (yeux + son) :

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
    texte  {"type": "eyes", "mood"?, "look"?: [x, y] | "auto", "blink"?, "wink"?, "pupil"?, "hold_ms"}
    texte  {"type": "gaze", "x", "y"}     visage vu par la caméra (-1..1, vu de face)
    texte  {"type": "presence", "present": bool}

Protocole de la caméra : voir camera.py ({"type": "hello", "device": "camera"}).

Variables d'environnement :
    OPAL_DEVICE_TOKEN   jeton partagé avec les firmwares (obligatoire)
    IRIS_URL            API d'Opal (défaut http://iris:8765, réseau Docker)
    IRIS_TOKEN          jeton distant d'Opal, ou IRIS_TOKEN_FILE (défaut /run/secrets/iris_token)
    IRIS_NODE_TOKEN     jeton des nœuds d'Opal, ou IRIS_NODE_TOKEN_FILE (défaut /run/secrets/iris_node_token)
    OPAL_VIEWER_PASSWORD  mot de passe du direct vidéo (port + 1, /opal/cam)
    CLAUDE_CODE_OAUTH_TOKEN  abonnement Claude pour décrire les photos (vision.py)
    OPAL_GREET / OPAL_GREET_COOLDOWN_S   salutation à l'arrivée (défaut 1 / 3600 s)
    OPAL_CAM_MIRROR     1 si l'image de la caméra est déjà en miroir
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
import time
from http import HTTPStatus
from pathlib import Path

import httpx
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from aiohttp import web

import vision
from camera import CameraHub
from logs import log
from node import OpalNode, eyes_command
from voice import MIC_RATE, TTS_RATE, Speaker, Transcriber
from web import make_app

MAX_UTTERANCE_S = 20
MIN_UTTERANCE_S = 0.3
AUDIO_CHUNK = 4096  # octets par trame binaire envoyée à l'ESP32
MAX_SPOKEN_CHARS = 600
IRIS_TIMEOUT_S = 180
GAZE_MIN_INTERVAL_S = 0.1
GREETING_PROMPT = (
    "[Opal Box] Quelqu'un vient d'apparaître devant ta caméra. "
    "Salue-le en une phrase courte et naturelle, sans poser de question."
)

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
        self._devices: set[ServerConnection] = set()  # boîtiers avec yeux / haut-parleur
        self.cams = CameraHub(self._on_gaze, self._on_presence)
        self.node: OpalNode | None = None
        self._last_gaze = 0.0
        self._last_greeting = 0.0
        self._greet = os.environ.get("OPAL_GREET", "1") == "1"
        self._greet_cooldown = float(os.environ.get("OPAL_GREET_COOLDOWN_S", "3600"))

    def _update_node(self) -> None:
        """Le nœud « opal-box » existe pour Opal tant qu'un appareil est en ligne."""
        if self.node is None:
            return
        if self._devices or self.cams.online:
            self.node.start()
        else:
            self.node.stop()

    async def handle_tool(self, tool: str, args: dict[str, object]) -> tuple[bool, str]:
        """Outils d'Opal Box appelés par Opal (voir node.py)."""
        if tool == "opal_box_yeux":
            cmd, summary = eyes_command(args)
            if not summary:
                return False, "Aucune expression demandée."
            sent = await self.send_to_devices(cmd)
            log("eyes", command=cmd, devices=sent)
            return (True, f"Yeux : {summary}.") if sent else (False, "Les yeux d'Opal Box ne sont pas connectés.")
        if tool == "opal_box_photo":
            question = str(args.get("question") or "Décris ce que tu vois.")
            jpeg = await self.cams.snapshot(flash=bool(args.get("flash")))
            log("photo", bytes=len(jpeg), question=question)
            return True, await vision.describe(jpeg, question)
        if tool == "opal_box_presence":
            return True, self.cams.presence_text()
        return False, f"Outil inconnu : {tool}"

    async def _on_gaze(self, x: float, y: float) -> None:
        now = time.monotonic()
        if now - self._last_gaze >= GAZE_MIN_INTERVAL_S:
            self._last_gaze = now
            await self.send_to_devices({"type": "gaze", "x": round(x, 3), "y": round(y, 3)})

    async def _on_presence(self, present: bool) -> None:
        await self.send_to_devices({"type": "presence", "present": present})
        if self.node is not None:
            event = "personne_arrivee" if present else "personne_partie"
            await self.node.send_event(event, {"lieu": "devant Opal Box"})
        now = time.monotonic()
        if present and self._greet and self._devices and now - self._last_greeting > self._greet_cooldown:
            self._last_greeting = now
            asyncio.create_task(self._greet_visitor())

    async def _greet_visitor(self) -> None:
        try:
            answer = await self._iris.ask(GREETING_PROMPT)
        except Exception as exc:
            log("greeting_failed", error=str(exc))
            return
        log("greeting", text=answer)
        for ws in list(self._devices):
            await self._speak(ws, answer)
            await self._send(ws, type="state", value="idle")

    async def send_to_devices(self, payload: dict[str, object]) -> int:
        """Envoie un message à tous les boîtiers connectés ; retourne leur nombre."""
        message = json.dumps(payload, ensure_ascii=False)
        sent = 0
        for ws in list(self._devices):
            try:
                await ws.send(message)
                sent += 1
            except websockets.ConnectionClosed:
                pass
        return sent

    def check_auth(self, ws: ServerConnection, request: Request) -> Response | None:
        """Refuse la poignée de main si le jeton est absent ou faux."""
        header = request.headers.get("Authorization", "")
        given = header.removeprefix("Bearer ").strip()
        if hmac.compare_digest(given.encode(), self._token.encode()):
            return None
        log("auth_refused", peer=str(ws.remote_address))
        return ws.respond(HTTPStatus.UNAUTHORIZED, "Jeton invalide\n")

    async def handle(self, ws: ServerConnection) -> None:
        """Chaque appareil s'annonce d'abord : la caméra, ou le boîtier (yeux + son)."""
        peer = str(ws.remote_address)
        log("device_connected", peer=peer)
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
        except (TimeoutError, ValueError, websockets.ConnectionClosed):
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            return
        log("device_hello", **{k: v for k, v in hello.items() if k != "type"})
        try:
            if hello.get("device") == "camera":
                await self._handle_camera(ws)
            else:
                await self._handle_box(ws)
        except websockets.ConnectionClosed:
            pass
        finally:
            log("device_disconnected", peer=peer, device=hello.get("device"))

    async def _handle_camera(self, ws: ServerConnection) -> None:
        await self.cams.attach(ws)
        self._update_node()
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    await self.cams.on_binary(message)
        finally:
            await self.cams.detach(ws)
            self._update_node()

    async def _handle_box(self, ws: ServerConnection) -> None:
        self._devices.add(ws)
        self._update_node()
        await self._send(ws, type="state", value="idle")
        if self.cams.online:
            await self._send(ws, type="presence", present=self.cams.present)
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
                if kind == "start":
                    audio.clear()
                    recording = True
                    await self._send(ws, type="state", value="listening")
                elif kind == "stop" and recording:
                    recording = False
                    pcm = bytes(audio)
                    audio.clear()
                    await self._handle_utterance(ws, pcm)
        finally:
            self._devices.discard(ws)
            self._update_node()

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


def secret(name: str, default_file: str) -> str:
    """Valeur de la variable `name`, sinon contenu du fichier `name`_FILE."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = Path(os.environ.get(f"{name}_FILE", default_file))
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def iris_token() -> str:
    return secret("IRIS_TOKEN", "/run/secrets/iris_token")


def iris_node_token() -> str:
    return secret("IRIS_NODE_TOKEN", "/run/secrets/iris_node_token")


async def run(host: str, port: int, device_token: str) -> None:
    iris_url = os.environ.get("IRIS_URL", "http://iris:8765")
    iris = IrisClient(iris_url, iris_token())
    stt = Transcriber()
    bridge = OpalBridge(iris, stt, Speaker(), device_token)
    if node_token := iris_node_token():
        node_url = iris_url.replace("http", "ws", 1).rstrip("/") + "/node"
        bridge.node = OpalNode(node_url, node_token, bridge.handle_tool)
    else:
        log("node_disabled", reason="IRIS_NODE_TOKEN absent : pas d'outils Opal Box pour Opal")
    await stt.warm_up()

    runner: web.AppRunner | None = None
    if viewer_password := os.environ.get("OPAL_VIEWER_PASSWORD", ""):
        runner = web.AppRunner(make_app(bridge.cams, viewer_password), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port + 1).start()
        log("camera_web_ready", url=f"http://{host}:{port + 1}/opal/cam")
    else:
        log("camera_web_disabled", reason="OPAL_VIEWER_PASSWORD absent")
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
        if runner is not None:
            await runner.cleanup()
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
