"""Pont WebSocket entre Opal Box (ESP32-S3 + ESP32-CAM) et Opal sur le VPS.

Le pont ne contient pas de cerveau : il transcrit la voix, envoie le texte à
l'API d'Opal (POST /api/command/stream, la même conversation que sur le PC et le Mac),
et dit la réponse phrase par phrase, au fil de sa génération, par le haut-parleur de l'ESP32.

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
    texte  {"type": "audio_start", "rate": 48000}
    binaire PCM 16 bits mono 48 kHz       voix d'Opal, sourates
    texte  {"type": "audio_end"}
    texte  {"type": "error", "message": "..."}
    texte  {"type": "eyes", "mood"?, "look"?: [x, y] | "auto", "blink"?, "wink"?, "pupil"?, "hold_ms"}
    texte  {"type": "gaze", "x", "y"}     visage vu par la caméra (-1..1, vu de face)
    texte  {"type": "presence", "present": bool}
    texte  {"type": "listen", "on": bool}   écoute sans bouton : micro en continu (opening.py)
    texte  {"type": "privacy", "on": bool}  mode privé : ni micro ni caméra, seul le bouton

  En écoute sans bouton, l'ESP32 envoie le micro en continu (hors lecture de la voix) ;
  le pont découpe les phrases au volume.

Protocole de la caméra : voir camera.py ({"type": "hello", "device": "camera"}).

Variables d'environnement :
    OPAL_DEVICE_TOKEN   jeton partagé avec les firmwares (obligatoire)
    IRIS_URL            API d'Opal (défaut http://iris:8765, réseau Docker)
    IRIS_TOKEN          jeton distant d'Opal, ou IRIS_TOKEN_FILE (défaut /run/secrets/iris_token)
    IRIS_NODE_TOKEN     jeton des nœuds d'Opal, ou IRIS_NODE_TOKEN_FILE (défaut /run/secrets/iris_node_token)
    OPAL_PC_MACS        adresses MAC du PC (virgules) à réveiller quand un visage connu arrive
    OPAL_VIEWER_PASSWORD  mot de passe du direct vidéo (port + 1, /opal/cam)
    CLAUDE_CODE_OAUTH_TOKEN  abonnement Claude pour décrire les photos (vision.py)
    OPAL_GREET / OPAL_GREET_COOLDOWN_S   salutation à l'arrivée (défaut 1 / 3600 s)
    OPAL_CAM_MIRROR     1 si l'image de la caméra est déjà en miroir
    OPAL_STT_MODEL      modèle faster-whisper (défaut small)
    OPAL_OPEN_MODE      écoute sans bouton : bouton|presence|connu|regard|mot (défaut regard)
    OPAL_TTS_VOICE / OPAL_TTS_RATE / OPAL_TTS_PITCH   voix Edge
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import math
import os
import re
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from datetime import date
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from aiohttp import web

import vision
import weather
from activity import OpalActivity
import posture
from initiative import SPOKEN, Initiative, Scene, duration, is_silent
from camera import CameraHub
from rhythm import DayRhythm
from logs import log
from node import SCENES, OpalNode, eyes_command
from opening import MODE_TEXT, Endpointer, Inputs, Opening
from player import Player
from speech import SpeechFeed, speech_script
from voice import MIC_RATE, TTS_RATE, Speaker, Transcriber
from web import make_app

MAX_UTTERANCE_S = 20
MIN_UTTERANCE_S = 0.3
AUDIO_CHUNK = 4096  # octets par trame binaire envoyée à l'ESP32
REST_MAX_DEG = 35.0  # position de repos de la tête : même borne que le firmware (head.h)
IRIS_TIMEOUT_S = 180
LONG_ANSWER = "C'est long, je continue en arrière-plan et je te tiens au courant."
UNREACHABLE_ANSWER = "Je n'arrive pas à joindre mon cerveau pour l'instant."
GAZE_MIN_INTERVAL_S = 0.1
NODE_GRACE_S = 30.0
AWAKE_AFTER_ACTIVITY_S = 120.0  # la nuit, reste éveillée 2 min après une activité
GREETING_PROMPT = (
    "[Opal Box] Quelqu'un vient d'apparaître devant ta caméra. "
    "Salue-le en une phrase courte et naturelle, sans poser de question."
)
GREETING_NAMED_PROMPT = (
    "[Opal Box] Ta caméra vient de reconnaître {name}, qui arrive devant toi. "
    "Salue-le en une phrase courte et naturelle, sans poser de question."
)
BACK_PROMPT = (
    "[Opal Box] {name} revient devant toi après {away} d'absence. Accueille-le et, s'il y a "
    "du nouveau d'important pour lui (messages, rappels, agenda proche), dis-le en bref."
)
BACK_AFTER_S = float(os.environ.get("OPAL_BACK_AFTER_MIN", "120")) * 60
# Miroir des expressions : Opal sourit quand tu souris, s'étonne avec toi, s'attriste avec toi.
EXPRESSION_EYES: dict[str, dict[str, object]] = {
    "happy": {"mood": "happy", "blink": True, "hold_ms": 2500},
    "surprised": {"mood": "surprised", "hold_ms": 1800},
    "sad": {"mood": "sad", "look": [0, 0.3], "hold_ms": 3000},
    "fearful": {"mood": "sad", "hold_ms": 2500},
    "angry": {"mood": "sad", "pupil": 0.3, "hold_ms": 2500},
    "disgust": {"mood": "surprised", "hold_ms": 1500},
}
COMFORT_PROMPT = (
    "[Opal Box] {name} a l'air triste depuis quelques minutes devant toi. Dis-lui un mot "
    "gentil et discret, sans insister ni poser de question intrusive."
)
COMFORT_COOLDOWN_S = 3600.0
# Animations des écrans : une animation plus importante n'est pas remplacée par une moindre
# (le minuteur choisi par Opal reste, même si Opal se met à réfléchir).
SCENE_PRIORITY = {"ambient": 0, "auto": 1, "reaction": 2, "manual": 3}
CLOCK_AFTER_S = 180.0  # personne devant la caméra depuis 3 min : horloge de bureau
# Gardien des PC : un inconnu seul devant la caméra pendant GUARD_LOCK_AFTER_S → verrouillage.
GUARD_ENABLED = os.environ.get("OPAL_GUARD_ENABLED", "1") == "1"
GUARD_LOCK_AFTER_S = float(os.environ.get("OPAL_GUARD_LOCK_AFTER_S", "30"))
BRIEFING_PROMPT = (
    "[Opal Box] {name} arrive ce matin. Voici son briefing du jour :\n{text}\n"
    "Dis-lui l'essentiel en trois phrases maximum."
)
# Gestes musique : 👌 lance la playlist, bras croisés en X mettent en pause. Ce sont des
# recettes d'Opal (rejouées sans IA, instantanées) : « Opal musique » ouvre sur le PC la
# 1re vidéo DANS la playlist (lecture immédiate), « Opal pause » appuie sur lecture/pause.
MUSIC_RECIPES = {
    "ok": os.environ.get("OPAL_MUSIC_RECIPE", "Opal musique"),
    "bras_croises": os.environ.get("OPAL_PAUSE_RECIPE", "Opal pause"),
}
MUSIC_REPEAT_S = 60.0  # un 👌 répété dans la minute ne rouvre pas d'onglet
# Garde : un inconnu devant la caméra sans visage connu depuis OWNER_GRACE_S → yeux fâchés.
OWNER_GRACE_S = 20.0
# Réveil du PC quand un visage connu arrive : Wake-on-LAN envoyé par la boîte (sur le réseau
# de la maison) aux adresses MAC de OPAL_PC_MACS, puis recette qui rallume l'écran.
PC_MACS = [m.strip() for m in os.environ.get("OPAL_PC_MACS", "").split(",") if m.strip()]
WAKE_RECIPE = os.environ.get("OPAL_WAKE_RECIPE", "Opal réveil écran")
WAKE_COOLDOWN_S = float(os.environ.get("OPAL_WAKE_COOLDOWN_S", "600"))

# Écoute sans bouton (voir opening.py) : OPAL_OPEN_MODE = bouton|presence|connu|regard|mot.
OPEN_MODE = os.environ.get("OPAL_OPEN_MODE", "regard")
OPENING_TICK_S = 0.25
# Mode privé à la voix (marche au bouton, même en mode privé).
_PRIVATE_ON = re.compile(r"\b(mode priv[ée]|arr[êe]te de m'?(?:[ée]couter|regarder)|ne m'?[ée]coute plus)\b", re.I)
_PRIVATE_OFF = re.compile(
    r"\b((?:sors|quitte|fin)\w* (?:du |le )?mode priv[ée]|tu peux (?:m'?[ée]couter|me regarder|regarder))\b", re.I)
# Phrases que Whisper invente sur du bruit : ignorées quand on écoute sans bouton.
_PHANTOM = re.compile(r"sous-titr|merci d'avoir regard|abonnez|amara\.org|^\W*(merci|musique)\W*$", re.I)

class IrisClient:
    """Appelle l'API d'Opal comme le ferait un raccourci iOS."""

    def __init__(self, url: str, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=IRIS_TIMEOUT_S + 10,
        )

    async def confirm(self, confirm_id: str, approved: bool) -> bool:
        """Répond à une demande de confirmation d'Opal (comme le bouton de son interface)."""
        response = await self._client.post(
            "/api/confirm", json={"id": confirm_id, "approved": approved}
        )
        return response.status_code == HTTPStatus.OK

    async def briefing(self) -> str:
        """Briefing du jour d'Opal (météo, agenda, mails) ; s'affiche aussi dans son chat."""
        response = await self._client.post("/api/briefing")
        response.raise_for_status()
        return str((response.json().get("briefing") or {}).get("text") or "")

    async def run_recipe(self, key: str) -> tuple[bool, str]:
        """Rejoue une recette d'Opal (suite d'outils fixe, sans IA) : (réussie, résumé)."""
        response = await self._client.post(f"/api/recipes/{quote(key)}/run", json={})
        response.raise_for_status()
        body = response.json()
        return bool((body.get("run") or {}).get("ok")), str(body.get("summary") or "")

    async def ask(self, text: str) -> str:
        response = await self._client.post(
            "/api/command", json={"text": text, "timeout": IRIS_TIMEOUT_S}
        )
        if response.status_code == HTTPStatus.ACCEPTED:
            return LONG_ANSWER
        response.raise_for_status()
        return str(response.json().get("answer") or "")

    async def ask_stream(self, text: str) -> AsyncIterator[dict[str, object]]:
        """Comme ``ask``, au fil de la génération : événements ``delta`` (texte à dire),
        ``break`` (une autre prise de parole suit), puis ``answer`` (réponse finale ;
        ``streamed`` dit si les ``delta`` l'ont déjà donnée)."""
        async with self._client.stream(
            "POST", "/api/command/stream", json={"text": text, "timeout": IRIS_TIMEOUT_S}
        ) as response:
            old_brain = response.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED)
            if not old_brain:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("type") == "pending":
                        event = {"type": "answer", "text": LONG_ANSWER, "streamed": False}
                    yield event
        if old_brain:  # cerveau pas encore mis à jour : réponse en une fois
            yield {"type": "answer", "text": await self.ask(text), "streamed": False}

    async def close(self) -> None:
        await self._client.aclose()


class GuardHub:
    """File de commandes verrouiller / déverrouiller vers les agents de PC (attente longue).

    L'agent de chaque machine fait un GET /opal/cam/guard/poll qui attend ici jusqu'à ce
    qu'une commande arrive (ou 25 s), puis la rejoue. L'enforcement est dans l'agent ;
    le pont ne décide que du moment, à partir de la caméra (jamais l'IA)."""

    def __init__(self) -> None:
        self._pending: dict[str, list[dict[str, object]]] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self.locked: dict[str, bool] = {}
        self.seen: dict[str, float] = {}

    async def poll(self, machine: str, timeout: float = 25) -> dict[str, object] | None:
        self.seen[machine] = time.time()
        queue = self._pending.setdefault(machine, [])
        if not queue:
            event = self._waiters.setdefault(machine, asyncio.Event())
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                return None
        return queue.pop(0) if queue else None

    def note_state(self, machine: str, locked: bool) -> None:
        self.seen[machine] = time.time()
        self.locked[machine] = locked

    def online(self) -> list[str]:
        now = time.time()
        return [m for m, t in self.seen.items() if now - t < 60]

    def send(self, cmd: str, reason: str = "") -> int:
        """Envoie la commande à toutes les machines vues récemment ; retourne leur nombre."""
        targets = self.online()
        for machine in targets:
            self._pending.setdefault(machine, []).append({"cmd": cmd, "reason": reason})
            if machine in self._waiters:
                self._waiters[machine].set()
        log("guard_command", cmd=cmd, machines=len(targets))
        return len(targets)


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
        self.cams.on_recording = self._on_recording
        self.cams.on_gesture = self._on_gesture
        self.cams.on_expression = self._on_expression
        self.cams.on_eye_contact = self._on_eye_contact
        self.node: OpalNode | None = None
        self._node_stop: asyncio.TimerHandle | None = None
        self._last_gaze = 0.0
        self._last_greeting = 0.0
        self._greet = os.environ.get("OPAL_GREET", "1") == "1"
        self._greet_cooldown = float(os.environ.get("OPAL_GREET_COOLDOWN_S", "3600"))
        self._public_url = os.environ.get("OPAL_PUBLIC_URL", "https://iris.inoma.pro").rstrip("/")
        self.pending_confirmation: str | None = None  # validable d'un pouce levé
        self.rhythm = DayRhythm()
        self._asleep = False
        self._last_activity = 0.0
        self._briefing_day: date | None = None
        self._music_busy = False
        self._music_at: dict[str, float] = {}
        self._owner_seen = 0.0  # dernier visage connu reconnu (monotonic)
        self._owner_here = False
        self._guarding = False
        self._woke_pc_at = -WAKE_COOLDOWN_S
        self.initiative = Initiative(self.rhythm)
        self.posture = posture.PostureCoach()
        self._initiative_check = 0.0
        self._saying = False
        self._left_at = 0.0  # départ de la dernière personne (monotonic)
        self._sad_seen: deque[float] = deque(maxlen=6)
        self._scene: tuple[str | None, float] = (None, 0.0)  # (genre, fin) de l'animation affichée
        self._clock_on = False
        self.guard = GuardHub()
        self._pc_locked = False
        self._unlock_after_absence = False
        self._stranger_since = 0.0
        self._comforted_at = -COMFORT_COOLDOWN_S
        self.opening = Opening(OPEN_MODE)
        self._listening = False  # écoute sans bouton ouverte (micro en continu)
        self._busy = False       # Opal réfléchit ou parle : on n'écoute pas sa propre voix
        self._play_until = 0.0  # fin de lecture de la voix déjà envoyée (monotonic)
        # Sourate (ou autre MP3) jouée sur le haut-parleur ; on n'écoute pas pendant la lecture.
        self.player = Player(self.send_to_devices, self._send_audio, self._update_listening)
        self._music_held = False  # sourate mise en pause pour la voix : elle reprend après
        self.head_rest: dict[str, float] | None = None  # position de repos annoncée par le boîtier

    def set_pending_confirmation(self, confirm_id: str | None) -> None:
        self.pending_confirmation = confirm_id

    def _update_node(self) -> None:
        """Le nœud « opal-box » existe pour Opal tant qu'un appareil est en ligne.

        Une coupure brève (reconnexion WiFi) ne le retire pas : on attend NODE_GRACE_S.
        """
        if self.node is None:
            return
        if self._node_stop is not None:
            self._node_stop.cancel()
            self._node_stop = None
        if self._devices or self.cams.online:
            self.node.start()
        else:
            self._node_stop = asyncio.get_running_loop().call_later(NODE_GRACE_S, self.node.stop)

    async def handle_tool(self, tool: str, args: dict[str, object]) -> tuple[Any, ...]:
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
            # Les yeux deviennent un objectif pendant la visée, puis déclic + flash.
            await self.send_to_devices({"type": "camera_fx", "phase": "aim"})
            try:
                jpeg, full = await self.cams.snapshot(flash=bool(args.get("flash")))
            except Exception:
                await self.send_to_devices({"type": "camera_fx", "phase": "done"})
                raise
            await self.send_to_devices({"type": "camera_fx", "phase": "shoot"})
            asyncio.get_running_loop().call_later(
                0.9,
                lambda: asyncio.ensure_future(
                    self.send_to_devices({"type": "camera_fx", "phase": "done"})
                ),
            )
            photo_id = self.cams.keep_photo(jpeg)
            log("photo", bytes=len(jpeg), full=full, id=photo_id)
            description = await vision.describe(jpeg, question)
            url = f"{self._public_url}/opal/cam/photos/{photo_id}.jpg"
            note = "" if full else " (image du flux, la photo haute définition n'est pas arrivée)"
            return True, (
                f"{description}\n\nPhoto{note} : {url}\n"
                "Donne ce lien à l'utilisateur s'il veut voir la photo."
            )
        if tool == "opal_box_presence":
            return True, self.cams.presence_text()
        if tool == "opal_box_memoriser_visage":
            name = str(args.get("nom") or "").strip()[:40]
            if not name:
                return False, "Il me faut le prénom de la personne."
            await self.send_to_devices({"type": "eyes", "mood": "surprised", "look": [0, 0],
                                        "hold_ms": 8000})
            count = await self.cams.learn_face(name)
            await self.send_to_devices({"type": "eyes", "mood": "happy", "blink": True,
                                        "hold_ms": 3000})
            return True, f"Visage de {name} mémorisé ({count} images). Je le reconnaîtrai."
        if tool == "opal_box_tete":
            cmd: dict[str, object] = {"type": "head"}
            looks = {"gauche": -0.8, "droite": 0.8, "centre": "center", "libre": "auto"}
            if (direction := args.get("direction")) in looks:
                cmd["look"] = looks[str(direction)]
            if (geste := args.get("geste")) in ("non", "content"):
                cmd["gesture"] = "shake" if geste == "non" else "wiggle"
            if len(cmd) == 1:
                return False, "Indique une direction ou un geste."
            sent = await self.send_to_devices(cmd)
            return (True, "Tête : c'est fait.") if sent else (False, "Opal Box n'est pas connectée.")
        if tool == "opal_box_repos":
            return await self._rest_tool(args)
        if tool == "opal_box_ecran":
            return await self._screen_tool(args)
        if tool == "opal_box_lecture":
            return await self._play_tool(args)
        if tool == "opal_box_verrouiller":
            n = await self.lock_pcs(str(args.get("raison") or "Verrouillage demandé"))
            if not n:
                return False, "Aucun PC avec l'agent de verrouillage n'est en ligne."
            return True, (f"{n} PC verrouillé(s). Déverrouillage par son code, ou quand ta caméra "
                          "le reconnaît à son retour (s'il est devant toi maintenant, il doit "
                          "d'abord s'absenter).")
        if tool == "opal_box_ecoute":
            if (prive := args.get("prive")) is not None:
                await self.set_private(bool(prive))
            if (mode := args.get("mode")) and not self.opening.set_mode(str(mode)):
                return False, f"Mode inconnu : {mode}."
            if mode:
                log("opening_mode", mode=self.opening.mode)
            if self.opening.private:
                return True, "Mode privé : je n'écoute et ne regarde plus, seul le bouton marche."
            return True, f"J'écoute {MODE_TEXT[self.opening.mode]} (et toujours avec le bouton)."
        if tool == "opal_box_oublier_visage":
            name = str(args.get("nom") or "").strip()
            if self.cams.faces is None or not self.cams.faces.forget(name):
                return False, f"Je ne connais pas le visage de {name}."
            return True, f"J'ai oublié le visage de {name}."
        return False, f"Outil inconnu : {tool}"

    async def _play_tool(self, args: dict[str, object]) -> tuple[bool, str, dict[str, object]]:
        """Outil opal_box_lecture : jouer, pause, reprendre, volume, stop, état."""
        action = str(args.get("action") or "jouer")
        player = self.player
        if action != "etat":
            self._music_held = False  # une demande explicite l'emporte sur la reprise auto
        if action == "jouer":
            url = str(args.get("url") or "")
            if not url.startswith("https://"):
                return False, "Il me faut l'adresse https du fichier audio.", {}
            if not self._devices:
                return False, "Opal Box n'est pas connectée.", {}
            if args.get("volume") is not None:
                player.set_volume(int(float(str(args["volume"]))))
            await player.play(url, str(args.get("titre") or "").strip()[:80])
            text = f"Lecture sur le haut-parleur d'Opal Box : {player.title or url}."
        elif action == "stop":
            text = "Lecture arrêtée." if await player.stop() else "Rien n'était en lecture."
        elif action == "pause":
            text = "En pause." if await player.pause() else "Rien à mettre en pause."
        elif action == "reprendre":
            text = "Ça reprend." if await player.resume() else "Rien n'était en pause."
        elif action == "basculer":  # bouton ⏯ de l'écran : le robot sait s'il est en pause
            if player.paused:
                text = "Ça reprend." if await player.resume() else "Rien n'était en pause."
            else:
                text = "En pause." if await player.pause() else "Rien en lecture."
        elif action == "volume":
            if args.get("volume") is not None:
                player.set_volume(int(float(str(args["volume"]))))
            elif args.get("ecart") is not None:
                player.set_volume(player.volume + int(float(str(args["ecart"]))))
            text = f"Volume à {player.volume} %."
        elif action == "etat":
            text = (f"{'En pause' if player.paused else 'En lecture'} : {player.title}, volume "
                    f"{player.volume} %." if player.playing else "Rien en lecture.")
        else:
            return False, f"Action inconnue : {action}.", {}
        return True, text, player.status()

    async def _rest_tool(self, args: dict[str, object]) -> tuple[bool, str, dict[str, object]]:
        """Outil opal_box_repos : position de repos de la tête (degrés), retenue par le boîtier."""
        if not self._devices:
            return False, "Opal Box n'est pas connectée.", {}
        if self.head_rest is None:
            return False, "Le programme du robot est trop ancien pour régler la position de repos.", {}
        rest = dict(self.head_rest)
        changed = False
        for key, name in (("tilt", "hauteur"), ("pan", "gauche_droite")):
            if args.get(name) is not None:
                rest[key] = max(-REST_MAX_DEG, min(REST_MAX_DEG, round(float(str(args[name])), 1)))
                changed = True
        if changed:
            await self.send_to_devices({"type": "head", "rest": rest})
            self.head_rest = rest
        text = (f"Tête au repos : {rest['tilt']:+g}° en hauteur, {rest['pan']:+g}° gauche-droite"
                f"{' (réglé)' if changed else ''}.")
        return True, text, {"hauteur": rest["tilt"], "gauche_droite": rest["pan"],
                            "max": REST_MAX_DEG}

    async def _screen_tool(self, args: dict[str, object]) -> tuple[bool, str]:
        """Outil opal_box_ecran : animation plein écran et/ou style des yeux."""
        if not self._devices:
            return False, "Les écrans d'Opal Box ne sont pas connectés."
        done = []
        if (style := args.get("style")) in ("opale", "gardien"):
            await self.send_to_devices({"type": "eyes", "style": style, "hold_ms": 1})
            done.append(f"style {style}")
        name = str(args.get("animation") or "")
        ms = int(float(str(args.get("duree_s", 10))) * 1000)
        if name == "yeux":
            self._scene = (None, 0.0)
            await self.send_to_devices({"type": "scene", "name": "aucune"})
            done.append("retour aux yeux")
        elif name == "meteo":
            if not await self.show_weather(ms):
                return False, "Météo indisponible pour le moment."
            done.append("météo")
        elif name in SCENES:
            data: dict[str, object] = {}
            if name == "minuteur":
                data["minutes"] = float(str(args.get("minutes") or 25))
            if args.get("texte"):
                data["label"] = weather.ascii_text(str(args["texte"]))[:23]
            if args.get("nombre") is not None:
                data["count"] = int(str(args["nombre"]))
            await self.show_scene(name, ms, "manual", **data)
            done.append(f"{name} {data['minutes']:g} min" if name == "minuteur" else name)
        if not done:
            return False, "Indique une animation ou un style."
        return True, "Écrans : " + ", ".join(done) + "."

    async def _on_gaze(self, x: float, y: float) -> None:
        now = time.monotonic()
        if now - self._last_gaze >= GAZE_MIN_INTERVAL_S:
            self._last_gaze = now
            await self.send_to_devices({"type": "gaze", "x": round(x, 3), "y": round(y, 3)})

    async def _on_gesture(self, gesture: str) -> None:
        """Main ouverte : coucou. Pouce levé / baissé : répond à la confirmation en attente."""
        self._last_activity = time.monotonic()
        await self.update_sleep()
        if self.node is not None:
            await self.node.send_event("geste", {"geste": gesture.replace("_", " ")})
        if gesture == "main_ouverte":
            await self.send_to_devices({"type": "eyes", "mood": "happy", "wink": 1, "look": [0, 0],
                                        "hold_ms": 2500})
            await self.send_to_devices({"type": "head", "gesture": "wiggle"})
            return
        if gesture in MUSIC_RECIPES:
            play = gesture == "ok"
            await self.send_to_devices(
                {"type": "eyes", "mood": "happy", "wink": 1, "look": [0, 0], "hold_ms": 3000}
                if play else {"type": "eyes", "mood": "neutral", "blink": True, "look": [0, 0],
                              "hold_ms": 2000}
            )
            if play:
                await self.send_to_devices({"type": "head", "gesture": "wiggle"})
            else:
                await self.show_scene("aucune", kind="reaction")
            asyncio.create_task(self._music(gesture))
            return
        approved = gesture == "pouce_haut"
        if self.pending_confirmation:
            ok = await self._iris.confirm(self.pending_confirmation, approved)
            log("gesture_confirm", approved=approved, ok=ok)
            if ok:
                self.pending_confirmation = None
        # Réaction bien visible : grand sourire des yeux, ou tristesse, pendant 4 s.
        if approved:
            reaction = {"type": "eyes", "mood": "happy", "look": [0, 0], "pupil": 0.95,
                        "wink": 1, "hold_ms": 4000}
        else:
            reaction = {"type": "eyes", "mood": "sad", "look": [0, 0.6], "pupil": 0.15,
                        "blink": True, "hold_ms": 4000}
        sent = await self.send_to_devices(reaction)
        # Pouce baissé : spirales « étourdi » (pouce levé : le grand sourire suffit).
        if not approved:
            await self.show_scene("etourdi", 2500, "reaction")
        # Le cou suit : balancement content, ou « non » de la tête.
        await self.send_to_devices({"type": "head", "gesture": "wiggle" if approved else "shake"})
        log("gesture_reaction", gesture=gesture, devices=sent)

    async def _music(self, gesture: str) -> None:
        """Une demande à la fois ; un 👌 répété dans la minute est ignoré (pas de 2e onglet)."""
        now = time.monotonic()
        if self._music_busy or (
            gesture == "ok" and now - self._music_at.get("ok", -MUSIC_REPEAT_S) < MUSIC_REPEAT_S
        ):
            log("music_skipped", gesture=gesture)
            return
        self._music_busy, self._music_at[gesture] = True, now
        try:
            await self._ask_music(gesture)
        finally:
            self._music_busy = False

    async def _ask_music(self, gesture: str) -> None:
        try:
            ok, summary = await self._iris.run_recipe(MUSIC_RECIPES[gesture])
        except Exception as exc:
            ok, summary = False, str(exc)
        log("music", gesture=gesture, ok=ok, text=summary[:200])
        if ok and gesture == "ok":
            await self.show_scene("musique", 10000, "reaction")
        if not ok:
            await self.send_to_devices({"type": "eyes", "mood": "sad", "hold_ms": 2500})

    async def _on_expression(self, expression: str) -> None:
        if self._guarding or self._asleep or (eyes := EXPRESSION_EYES.get(expression)) is None:
            return
        await self.send_to_devices({"type": "eyes", **eyes})
        if expression not in ("sad", "fearful"):
            return
        # Triste plusieurs fois en quelques minutes : Opal dit un mot gentil (1 fois/heure).
        now = time.monotonic()
        self._sad_seen.append(now)
        recent = [t for t in self._sad_seen if now - t < 240]
        who = self.cams.identity
        if len(recent) >= 3 and who and now - self._comforted_at > COMFORT_COOLDOWN_S:
            self._comforted_at = now
            asyncio.create_task(self.say(COMFORT_PROMPT.format(name=who), "comfort"))

    async def _on_eye_contact(self) -> None:
        """Tu regardes Opal dans les yeux : pupilles qui s'ouvrent, petit sourire, clignement."""
        if self._guarding or self._asleep:
            return
        await self.send_to_devices({"type": "eyes", "mood": "happy", "look": [0, 0], "pupil": 0.9,
                                    "blink": True, "hold_ms": 1500})

    async def _on_recording(self, on: bool) -> None:
        log("live_view", watched=on)
        await self.send_to_devices({"type": "recording", "on": on})

    async def _on_presence(self, present: bool) -> None:
        await self.send_to_devices({"type": "presence", "present": present})
        knows_faces = bool(self.cams.faces and self.cams.faces.people)
        who = None
        if present and knows_faces:
            # Jusqu'à 3 s pour être sûr : un visage connu gagne dès qu'il est reconnu,
            # « inconnu » seulement après plusieurs analyses concordantes.
            for _ in range(6):
                who = self.cams.identity
                if who or len(self.cams.votes) >= 4:
                    break
                await asyncio.sleep(0.5)
        stranger = present and knows_faces and who == "" and len(self.cams.votes) >= 3
        if self.node is not None:
            event = "personne_arrivee" if present else "personne_partie"
            data: dict[str, object] = {"lieu": "devant Opal Box"}
            if present and knows_faces:
                data["personne"] = who or "inconnu"
            if stranger and self.cams.frame is not None:
                photo_id = self.cams.keep_photo(self.cams.frame)
                data["photo"] = f"{self._public_url}/opal/cam/photos/{photo_id}.jpg"
                event = "inconnu_devant_opal_box"
            await self.node.send_event(event, data)
        await self.update_sleep()
        if not present:
            self._left_at = time.monotonic()
            return
        away = time.monotonic() - self._left_at if self._left_at else 0.0
        log("arrival", person=who if knows_faces else "?")
        if stranger:  # regard méfiant, pas de salutation
            await self.send_to_devices({"type": "eyes", "mood": "angry", "look": [0, 0],
                                        "hold_ms": 5000})
            return
        if who:
            await self.send_to_devices({"type": "eyes", "mood": "happy", "blink": True,
                                        "hold_ms": 3000})
        # Première arrivée du matin : le briefing remplace la salutation.
        if self.rhythm.is_morning() and self._briefing_day != self.rhythm.today():
            self._briefing_day = self.rhythm.today()
            asyncio.create_task(self._morning_briefing(who))
            return
        now = time.monotonic()
        if self._greet and now - self._last_greeting > self._greet_cooldown:
            self._last_greeting = now
            asyncio.create_task(self._greet_visitor(who, away))

    async def _morning_briefing(self, who: str | None) -> None:
        self.initiative.spoke()
        asyncio.create_task(self.show_weather(12000, "reaction"))
        try:
            text = await self._iris.briefing()
        except Exception as exc:
            log("briefing_failed", error=str(exc))
            return
        log("briefing", chars=len(text))
        await self.say(BRIEFING_PROMPT.format(name=who or "l'utilisateur", text=text[:3000]),
                       "briefing")

    async def note_activity(self, cmd: dict[str, object]) -> int:
        """Expression venue de l'activité d'Opal : réveille la boîte si elle dormait."""
        self._last_activity = time.monotonic()
        await self.update_sleep()
        if cmd.get("type") == "scene":
            return await self.show_scene(str(cmd.get("name")), int(cmd.get("ms") or 0), "auto")
        return await self.send_to_devices(cmd)

    async def show_scene(self, name: str, ms: int = 8000, kind: str = "manual", **data: object) -> int:
        """Animation plein écran sur la boîte (« aucune » = retour aux yeux), selon sa priorité."""
        now = time.monotonic()
        current, until = self._scene
        if current is not None and now < until and SCENE_PRIORITY[kind] < SCENE_PRIORITY[current]:
            return 0
        if name == "aucune":
            if current is None and kind == "auto":
                return 0  # rien d'affiché : pas besoin de déranger les yeux
            self._scene = (None, 0.0)
        else:
            if name == "minuteur" and data.get("minutes"):
                until = now + float(str(data["minutes"])) * 60
            else:
                until = now + ms / 1000 if ms else math.inf
            self._scene = (kind, until)
        self._clock_on = self._clock_on and name == "horloge"
        return await self.send_to_devices({"type": "scene", "name": name, "ms": ms, **data})

    async def set_private(self, on: bool) -> None:
        if on == self.opening.private:
            return
        self.opening.private = on
        await self.cams.set_private(on)
        log("private", on=on)
        await self.send_to_devices({"type": "privacy", "on": on})
        if self.node is not None:
            await self.node.send_event("mode_prive_active" if on else "mode_prive_quitte",
                                       {"lieu": "Opal Box"})
        await self._update_listening()

    async def opening_loop(self) -> None:
        """Ouvre ou ferme l'écoute sans bouton selon la présence, le visage et le regard."""
        while True:
            await asyncio.sleep(OPENING_TICK_S)
            try:
                await self._update_listening()
            except Exception as exc:  # la boucle ne doit jamais mourir
                log("opening_failed", error=repr(exc))

    async def _update_listening(self) -> None:
        state = Inputs(
            present=self.cams.present,
            identity=self.cams.identity,
            knows_faces=bool(self.cams.faces and self.cams.faces.people),
            looking=self.cams.looking,
            busy=self._busy or self._saying or self._asleep or self._guarding or self.player.playing,
        )
        listening, why = self.opening.is_open(state, time.monotonic())
        if listening == self._listening:
            return
        self._listening = listening
        log("listening", on=listening, why=why)
        await self.send_to_devices({"type": "listen", "on": listening})

    async def update_sleep(self) -> None:
        """La nuit, sans personne devant la caméra ni activité récente : la boîte dort."""
        asleep = (
            self.rhythm.is_night()
            and not self.cams.present
            and time.monotonic() - self._last_activity > AWAKE_AFTER_ACTIVITY_S
        )
        if asleep != self._asleep:
            self._asleep = asleep
            log("sleep", asleep=asleep)
            await self.send_to_devices({"type": "sleep", "on": asleep})

    async def guard_loop(self) -> None:
        """Chaque seconde : visage connu → réveil du PC à son arrivée ; inconnu seul → yeux fâchés."""
        while True:
            await asyncio.sleep(1.0)
            try:
                await self._guard_tick()
            except Exception as exc:
                log("guard_failed", error=str(exc))

    async def _guard_tick(self) -> None:
        now = time.monotonic()
        cams = self.cams
        who = cams.identity if cams.present else None
        if who:
            self._owner_seen = now
            if not self._owner_here:
                self._owner_here = True
                asyncio.create_task(self._wake_pc(who))
        elif not cams.present:
            self._owner_here = False
        # Horloge de bureau : personne depuis 3 min (caméra active, boîte éveillée).
        idle_clock = bool(cams.online and not cams.present and not self._asleep
                          and self._left_at and now - self._left_at > CLOCK_AFTER_S)
        if idle_clock and not self._clock_on:
            if await self.show_scene("horloge", 0, "ambient"):
                self._clock_on = True
        elif not idle_clock and self._clock_on:
            self._clock_on = False
            await self.show_scene("aucune", kind="ambient")
        knows_faces = bool(cams.faces and cams.faces.people)
        stranger = (
            cams.present and knows_faces and who == "" and len(cams.votes) >= 3
            and now - self._owner_seen > OWNER_GRACE_S
        )
        if stranger != self._guarding:
            self._guarding = stranger
            log("guard", stranger=stranger)
        await self._guard_pc(stranger, bool(who), now)
        # Tenu 4 s, renouvelé toutes les secondes : fâché tant que l'inconnu est là.
        if stranger:
            await self.send_to_devices({"type": "eyes", "mood": "angry", "hold_ms": 4000})
        # Toutes les 5 s : y a-t-il une raison de parler spontanément ?
        if now - self._initiative_check >= 5:
            self._initiative_check = now
            prompt = self.initiative.observe(Scene(cams.present, who, self._asleep))
            if prompt:
                asyncio.create_task(self.say(prompt, "initiative"))
        # Posture : comparée à ta position habituelle (mesure toutes les 5 s).
        if who and not self._asleep and (advice := self.posture.feed(cams.seen)):
            log("posture", advice=advice)
            await self.send_to_devices({"type": "eyes", "mood": "surprised", "hold_ms": 1500})
            asyncio.create_task(self.say(posture.PROMPTS[advice].format(name=who), "posture"))

    async def show_weather(self, ms: int = 10000, kind: str = "manual") -> int:
        try:
            data = await weather.today()
        except Exception as exc:
            log("weather_failed", error=str(exc))
            return 0
        return await self.show_scene("meteo", ms, kind, **data)

    async def lock_pcs(self, reason: str) -> int:
        """Verrouille les PC ; retourne le nombre de machines qui ont reçu l'ordre.

        Si le propriétaire est devant la caméra à ce moment (« verrouille mon PC » en
        partant), son visage ne déverrouillera qu'après une vraie absence puis un retour :
        sinon le verrouillage serait annulé dans la seconde."""
        n = self.guard.send("lock", reason)
        if n:
            self._pc_locked = True
            self._unlock_after_absence = bool(self.cams.present and self.cams.identity)
            log("guard_lock", reason=reason, wait_absence=self._unlock_after_absence)
            await self.send_to_devices({"type": "eyes", "style": "gardien", "hold_ms": 0})
        return n

    async def _guard_pc(self, stranger: bool, owner_here: bool, now: float) -> None:
        """Inconnu seul assez longtemps → verrouille les PC ; ton retour → déverrouille."""
        if not GUARD_ENABLED:
            return
        if self._unlock_after_absence and not self.cams.present:
            self._unlock_after_absence = False  # parti : ton prochain retour déverrouillera
        if owner_here:
            self._stranger_since = 0.0
            if self._pc_locked and not self._unlock_after_absence:
                self._pc_locked = False
                self.guard.send("unlock")
                log("guard_unlock", reason="propriétaire reconnu")
                await self.send_to_devices({"type": "eyes", "style": "opale", "hold_ms": 1})
            return
        if stranger and not self._pc_locked:
            self._stranger_since = self._stranger_since or now
            if now - self._stranger_since >= GUARD_LOCK_AFTER_S:
                await self.lock_pcs("Inconnu devant Opal pendant ton absence")
        elif not stranger:
            self._stranger_since = 0.0

    async def _wake_pc(self, who: str) -> None:
        now = time.monotonic()
        if now - self._woke_pc_at < WAKE_COOLDOWN_S:
            return
        self._woke_pc_at = now
        if PC_MACS:
            sent = await self.send_to_devices({"type": "wol", "macs": PC_MACS})
            log("wake_pc", person=who, wol_boxes=sent)
        # PC allumé mais écran éteint : une touche le rallume (échoue sans gêne s'il dort).
        await asyncio.sleep(8 if PC_MACS else 0)
        try:
            ok, summary = await self._iris.run_recipe(WAKE_RECIPE)
        except Exception as exc:
            ok, summary = False, str(exc)
        log("wake_screen", ok=ok, text=summary[:160])

    async def rhythm_loop(self) -> None:
        while True:
            await self.update_sleep()
            await asyncio.sleep(20)

    async def _greet_visitor(self, name: str | None = None, away: float = 0.0) -> None:
        if name and away > BACK_AFTER_S:
            prompt = BACK_PROMPT.format(name=name, away=duration(away))
        else:
            prompt = GREETING_NAMED_PROMPT.format(name=name) if name else GREETING_PROMPT
        await self.say(prompt, "greeting")

    async def say(self, prompt: str, kind: str) -> None:
        """Opal parle de lui-même, à voix haute par l'iPhone / le Mac caméra, sinon la boîte.

        Une seule prise de parole à la fois ; « RIEN » = Opal a choisi de se taire."""
        if self._saying:
            log("say_skipped", kind=kind)
            return
        self._saying = True
        self.initiative.spoke()
        try:
            if SPOKEN not in prompt:
                prompt += SPOKEN
            answer = await self._iris.ask(prompt)
        except Exception as exc:
            log("say_failed", kind=kind, error=str(exc))
            return
        finally:
            self._saying = False
        if is_silent(answer):
            log("say_silent", kind=kind)
            return
        sentences = speech_script(answer)
        self._busy = True
        await self._update_listening()
        try:
            heard = await self.cams.speak(self._tts, sentences, TTS_RATE)
            log("say", kind=kind, heard_by=heard, text=answer[:200])
            if not heard:
                for ws in list(self._devices):
                    await self._speak(ws, answer)
                    await self._send(ws, type="state", value="idle")
                await self._playback_done()
        finally:
            self._busy = False
            self.opening.replied(time.monotonic())

    async def _send_audio(self, pcm: bytes) -> int:
        """Envoie du PCM à tous les boîtiers ; retourne leur nombre."""
        sent = 0
        for ws in list(self._devices):
            try:
                await ws.send(pcm)
                sent += 1
            except websockets.ConnectionClosed:
                pass
        return sent

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
                await self._handle_camera(ws, hello)
            else:
                if isinstance(rest := hello.get("head_rest"), dict):
                    self.head_rest = {"pan": float(rest.get("pan", 0)), "tilt": float(rest.get("tilt", 0))}
                await self._handle_box(ws)
        except websockets.ConnectionClosed:
            pass
        finally:
            log(
                "device_disconnected",
                peer=peer,
                device=hello.get("device"),
                code=ws.close_code,
                reason=ws.close_reason,
            )

    async def _handle_camera(self, ws: ServerConnection, hello: dict) -> None:
        await self.cams.attach(ws, hello)
        self._update_node()
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    await self.cams.on_binary(message, ws)
        finally:
            await self.cams.detach(ws)
            self._update_node()

    async def _handle_box(self, ws: ServerConnection) -> None:
        self._devices.add(ws)
        self._update_node()
        await self._send(ws, type="audio_end")  # remet à zéro un boîtier qui jetait le son
        await self._send(ws, type="state", value="idle")
        if self.cams.online:
            await self._send(ws, type="presence", present=self.cams.present)
        if self.cams.viewers:
            await self._send(ws, type="recording", on=True)
        if self._asleep:
            await self._send(ws, type="sleep", on=True)
        if self.opening.private:
            await self._send(ws, type="privacy", on=True)
        if self._listening:
            await self._send(ws, type="listen", on=True)
        audio = bytearray()
        recording = False
        ep = Endpointer(MIC_RATE, MAX_UTTERANCE_S)
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    if recording:
                        if len(audio) < MAX_UTTERANCE_S * MIC_RATE * 2:
                            audio.extend(message)
                    elif self._listening and not self._busy:
                        if (phrase := ep.feed(message)) is not None:
                            self.opening.heard(time.monotonic())
                            asyncio.create_task(self._handle_utterance(ws, phrase, handsfree=True))
                    else:
                        ep.reset()  # micro de fin d'écoute ou voix d'Opal : on jette
                    continue

                msg = json.loads(message)
                kind = msg.get("type")
                if kind == "start":
                    await self._hold_music()  # la sourate se tait pendant qu'on parle
                    ep.reset()
                    audio.clear()
                    recording = True
                    await self._send(ws, type="state", value="listening")
                elif kind == "stats":  # mesures du son reçu par le boîtier (clics, débit, WiFi)
                    log("box_audio", **{k: v for k, v in msg.items() if k != "type"})
                elif kind == "stop" and recording:
                    recording = False
                    pcm = bytes(audio)
                    audio.clear()
                    asyncio.create_task(self._handle_utterance(ws, pcm))
        finally:
            self._devices.discard(ws)
            self._update_node()

    async def _handle_utterance(self, ws: ServerConnection, pcm: bytes, handsfree: bool = False) -> None:
        if len(pcm) < MIN_UTTERANCE_S * MIC_RATE * 2:
            await self._send(ws, type="state", value="idle")
            asyncio.create_task(self._release_music())
            return
        if self._busy:  # une autre phrase est en cours : sans bouton on l'ignore, au bouton on attend
            if handsfree:
                return
            for _ in range(int(IRIS_TIMEOUT_S * 4)):
                await asyncio.sleep(0.25)
                if not self._busy:
                    break
        self._busy = True
        await self._update_listening()
        try:
            await self._answer(ws, pcm, handsfree)
        finally:
            self._busy = False
            self.opening.replied(time.monotonic())
            await self._send(ws, type="state", value="idle")
            asyncio.create_task(self._release_music())

    async def _answer(self, ws: ServerConnection, pcm: bytes, handsfree: bool) -> None:
        await self._send(ws, type="state", value="thinking")
        text = ""
        try:
            text = await self._stt.transcribe(pcm)
        except Exception as exc:
            log("stt_failed", error=str(exc))
        if handsfree and text and _PHANTOM.search(text):
            log("transcript_phantom", text=text)
            text = ""
        if not text:
            if not handsfree:
                await self._send(ws, type="error", message="Je n'ai rien compris.")
            return

        await self._send(ws, type="transcript", text=text)
        log("transcript", text=text, handsfree=handsfree)
        if _PRIVATE_OFF.search(text) and self.opening.private:
            await self.set_private(False)
            answer = "D'accord, je t'écoute et je te regarde à nouveau."
            await self._speak(ws, answer)
        elif _PRIVATE_ON.search(text) and not _PRIVATE_OFF.search(text):
            await self.set_private(True)
            answer = "Mode privé. Je n'écoute plus et je ne regarde plus. Appuie sur le bouton pour me reparler."
            await self._speak(ws, answer)
        else:
            answer = await self._reply(ws, text)

        await self._send(ws, type="reply", text=answer)
        # La voix est envoyée plus vite qu'elle n'est jouée : on attend la fin de la lecture
        # avant de rouvrir l'écoute, sinon le micro entend Opal.
        await self._playback_done()

    async def _reply(self, ws: ServerConnection, text: str) -> str:
        """Demande à Opal et dit sa réponse au fil de la génération ; retourne le texte final."""
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        speaker = asyncio.create_task(self._speak_queue(ws, sentences))
        feed = SpeechFeed()
        answer = ""
        asked_at = time.monotonic()
        first_at = 0.0
        try:
            async for event in self._iris.ask_stream(text):
                kind = event.get("type")
                if kind == "delta":
                    ready = feed.feed(str(event.get("text") or ""))
                elif kind == "break":
                    ready = feed.flush()
                elif kind == "answer":
                    answer = str(event.get("text") or "")
                    if event.get("streamed"):
                        ready = feed.flush()
                    else:  # réponse différente de ce qui a été dit en route : on la dit en entier
                        feed.drop()
                        ready = speech_script(answer)
                else:
                    continue
                if ready and not first_at:
                    first_at = time.monotonic()
                for sentence in ready:
                    sentences.put_nowait(sentence)
        except Exception as exc:
            log("iris_failed", error=str(exc))
            answer = UNREACHABLE_ANSWER
            for sentence in speech_script(answer):
                sentences.put_nowait(sentence)
        finally:
            sentences.put_nowait(None)
        await speaker
        log(
            "reply",
            first_sentence_s=round((first_at or time.monotonic()) - asked_at, 2),
            total_s=round(time.monotonic() - asked_at, 2),
        )
        return answer

    async def _speak(self, ws: ServerConnection, text: str) -> float:
        """Envoie la voix ; retourne sa durée de lecture en secondes."""
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        for sentence in speech_script(text):
            sentences.put_nowait(sentence)
        sentences.put_nowait(None)
        return await self._speak_queue(ws, sentences)

    async def _speak_queue(self, ws: ServerConnection, sentences: asyncio.Queue[str | None]) -> float:
        """Dit les phrases à mesure qu'elles arrivent (``None`` = fin) ; retourne la durée dite.

        Chaque phrase part en synthèse dès son arrivée, pendant l'envoi des précédentes."""
        voices: asyncio.Queue[asyncio.Task[bytes] | None] = asyncio.Queue()

        async def synthesize_all() -> None:
            while (sentence := await sentences.get()) is not None:
                voices.put_nowait(asyncio.create_task(self._tts.synthesize(sentence)))
            voices.put_nowait(None)

        feeder = asyncio.create_task(synthesize_all())
        sent = 0
        try:
            while (voice := await voices.get()) is not None:
                try:
                    pcm = await voice
                except Exception as exc:
                    log("tts_failed", error=str(exc))
                    continue
                if not pcm:
                    continue
                if not sent:
                    await self._hold_music()  # Opal parle : la sourate attend
                    await self._send(ws, type="state", value="speaking")
                    await self._send(ws, type="audio_start", rate=TTS_RATE)
                for off in range(0, len(pcm), AUDIO_CHUNK):
                    await ws.send(pcm[off : off + AUDIO_CHUNK])
                sent += len(pcm)
                # Le boîtier joue chaque phrase à la suite de la précédente, ou tout de suite
                # s'il avait fini (pause pendant qu'Opal réfléchit encore).
                self._play_until = max(self._play_until, time.monotonic()) + len(pcm) / (TTS_RATE * 2)
        finally:
            feeder.cancel()
        if sent:
            await self._send(ws, type="audio_end")
        if self._music_held:  # toute prise de parole d'Opal (réponse, accueil, rappel…)
            asyncio.create_task(self._release_music())
        return sent / (TTS_RATE * 2)

    async def _hold_music(self) -> None:
        if await self.player.pause(for_voice=True):
            self._music_held = True

    async def _release_music(self) -> None:
        """Après la réponse d'Opal (voix jouée en entier), la sourate mise en attente reprend."""
        if not self._music_held:
            return
        for _ in range(240):  # Opal réfléchit encore, ou une autre phrase a suivi
            await self._playback_done()
            if not self._busy and time.monotonic() >= self._play_until:
                break
        if self._music_held:  # ni arrêtée ni mise en pause entre-temps par une demande
            self._music_held = False
            await self.player.resume()

    async def _playback_done(self) -> None:
        """Attend que le boîtier ait fini de jouer la voix envoyée."""
        await asyncio.sleep(max(0.0, self._play_until - time.monotonic()) + 0.4)

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
    activity = OpalActivity(
        iris_url.replace("http", "ws", 1).rstrip("/") + "/ws",
        iris_token(),
        bridge.note_activity,
        bridge.set_pending_confirmation,
    )
    activity.start()
    rhythm_task = asyncio.create_task(bridge.rhythm_loop())  # noqa: F841 (vit avec le pont)
    guard_task = asyncio.create_task(bridge.guard_loop())  # noqa: F841
    opening_task = asyncio.create_task(bridge.opening_loop())  # noqa: F841
    if node_token := iris_node_token():
        node_url = iris_url.replace("http", "ws", 1).rstrip("/") + "/node"
        bridge.node = OpalNode(node_url, node_token, bridge.handle_tool)
    else:
        log("node_disabled", reason="IRIS_NODE_TOKEN absent : pas d'outils Opal Box pour Opal")
    await stt.warm_up()

    runner: web.AppRunner | None = None
    guard_token = secret("OPAL_GUARD_TOKEN", "/run/secrets/opal_guard_token")
    if viewer_password := os.environ.get("OPAL_VIEWER_PASSWORD", ""):
        runner = web.AppRunner(make_app(bridge.cams, viewer_password, iris_token(), bridge._update_node,
                 bridge._screen_tool, bridge.guard, guard_token), access_log=None)
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
