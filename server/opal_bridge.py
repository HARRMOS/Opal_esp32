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
    OPAL_PC_MACS        adresses MAC du PC (virgules) à réveiller quand un visage connu arrive
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
import math
import os
import re
import sys
import time
from collections import deque
from datetime import date
from http import HTTPStatus
from pathlib import Path
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
from voice import MIC_RATE, TTS_RATE, Speaker, Transcriber
from web import make_app

MAX_UTTERANCE_S = 20
MIN_UTTERANCE_S = 0.3
AUDIO_CHUNK = 4096  # octets par trame binaire envoyée à l'ESP32
MAX_SPOKEN_CHARS = 600
IRIS_TIMEOUT_S = 180
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
HEARTS_EVERY_S = 180.0  # au plus un cœur de sourire toutes les 3 min
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
# Verrouillage demandé (voix, recette) alors que tu es devant la caméra : ton visage ne
# déverrouille qu'après une absence d'au moins ce temps (sinon ça se rouvre aussitôt).
UNLOCK_AWAY_S = 10.0
# Réveil du PC quand un visage connu arrive : Wake-on-LAN envoyé par la boîte (sur le réseau
# de la maison) aux adresses MAC de OPAL_PC_MACS, puis recette qui rallume l'écran.
PC_MACS = [m.strip() for m in os.environ.get("OPAL_PC_MACS", "").split(",") if m.strip()]
WAKE_RECIPE = os.environ.get("OPAL_WAKE_RECIPE", "Opal réveil écran")
WAKE_COOLDOWN_S = float(os.environ.get("OPAL_WAKE_COOLDOWN_S", "600"))

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
            return "C'est long, je continue en arrière-plan et je te tiens au courant."
        response.raise_for_status()
        return str(response.json().get("answer") or "")

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
        self._hearts_at = -HEARTS_EVERY_S
        self.guard = GuardHub()
        self._pc_locked = False
        self._unlock_armed = True  # faux : verrouillé devant toi, il faut partir puis revenir
        self._stranger_since = 0.0
        self._comforted_at = -COMFORT_COOLDOWN_S

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
        if tool == "opal_box_ecran":
            return await self._screen_tool(args)
        if tool == "opal_box_verrouiller":
            reason = str(args.get("raison") or "Verrouillage demandé")
            n = self.guard.send("lock", reason)
            if not n:
                return False, "Aucun PC avec l'agent de verrouillage n'est en ligne."
            self._pc_locked = True
            self._unlock_armed = False
            await self.send_to_devices({"type": "eyes", "style": "gardien", "hold_ms": 0})
            return True, f"{n} PC verrouillé(s). Déverrouillage quand tu reconnais le propriétaire ou par son code."
        if tool == "opal_box_oublier_visage":
            name = str(args.get("nom") or "").strip()
            if self.cams.faces is None or not self.cams.faces.forget(name):
                return False, f"Je ne connais pas le visage de {name}."
            return True, f"J'ai oublié le visage de {name}."
        return False, f"Outil inconnu : {tool}"

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
        # Cœurs pour un pouce levé, spirales « étourdi » pour un pouce baissé.
        await self.show_scene("coeurs" if approved else "etourdi", 4000 if approved else 2500, "reaction")
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
        now = time.monotonic()
        if expression == "happy" and now - self._hearts_at > HEARTS_EVERY_S:
            self._hearts_at = now
            await self.show_scene("coeurs", 3000, "reaction")
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

    async def _guard_pc(self, stranger: bool, owner_here: bool, now: float) -> None:
        """Inconnu seul assez longtemps → verrouille les PC ; ton retour → déverrouille."""
        if not GUARD_ENABLED:
            return
        if self._pc_locked and not self._unlock_armed and now - self._owner_seen >= UNLOCK_AWAY_S:
            self._unlock_armed = True  # tu es parti : ton retour déverrouillera
        if owner_here:
            self._stranger_since = 0.0
            if self._pc_locked and self._unlock_armed:
                self._pc_locked = False
                self.guard.send("unlock")
                log("guard_unlock", reason="propriétaire reconnu")
                await self.send_to_devices({"type": "eyes", "style": "opale", "hold_ms": 1})
            return
        if stranger and not self._pc_locked:
            self._stranger_since = self._stranger_since or now
            if now - self._stranger_since >= GUARD_LOCK_AFTER_S:
                if self.guard.send("lock", "Inconnu devant Opal pendant ton absence"):
                    self._pc_locked = True
                    self._unlock_armed = True
                    log("guard_lock", seconds=round(now - self._stranger_since))
                    await self.send_to_devices({"type": "eyes", "style": "gardien", "hold_ms": 0})
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
        heard = await self.cams.speak(self._tts, sentences, TTS_RATE)
        log("say", kind=kind, heard_by=heard, text=answer[:200])
        if not heard:
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
                await self._handle_camera(ws, hello)
            else:
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
        await self._send(ws, type="state", value="idle")
        if self.cams.online:
            await self._send(ws, type="presence", present=self.cams.present)
        if self.cams.viewers:
            await self._send(ws, type="recording", on=True)
        if self._asleep:
            await self._send(ws, type="sleep", on=True)
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
    activity = OpalActivity(
        iris_url.replace("http", "ws", 1).rstrip("/") + "/ws",
        iris_token(),
        bridge.note_activity,
        bridge.set_pending_confirmation,
    )
    activity.start()
    rhythm_task = asyncio.create_task(bridge.rhythm_loop())  # noqa: F841 (vit avec le pont)
    guard_task = asyncio.create_task(bridge.guard_loop())  # noqa: F841
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
