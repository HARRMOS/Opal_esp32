"""Caméra d'Opal Box : dernière image, suivi du visage, présence, photos à la demande.

L'ESP32-CAM envoie des JPEG en trames binaires, préfixés d'un octet :
  0x01 image du flux (suivi / direct)    0x02 photo demandée (haute définition)
Le pont lui répond en texte :
  {"type": "cam", "fps": 6, "size": "qvga"|"vga"}    cadence et taille du flux
  {"type": "snapshot", "flash": false}                 prendre une photo
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any

import cv2
import numpy as np

from faces import FaceBook
from gestures import GestureDetector
from logs import log

FRAME, SNAPSHOT = 1, 2
PRESENCE_ARRIVE_HITS = 3     # visages vus sur les 5 dernières analyses pour « arrivée »
PRESENCE_LEAVE_S = 45.0      # plus de visage depuis ce délai = « départ »
GAZE_HOLD_S = 1.2
MAX_PHOTOS = 50
GESTURE_COOLDOWN_S = 4.0


# Petites images, plusieurs analyses par seconde : un seul thread OpenCV coûte bien moins
# cher (sinon chaque calcul est éparpillé sur tous les cœurs du VPS partagé).
cv2.setNumThreads(1)

_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


def enhance(jpeg: bytes, width: int = 640) -> bytes:
    """Rend l'image du direct plus lisible : agrandie proprement, contraste local
    équilibré (CLAHE sur la luminance), légère accentuation des contours."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    h, w = img.shape[:2]
    if w < width:
        img = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    lab[:, :, 0] = _CLAHE.apply(lab[:, :, 0])
    img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    blur = cv2.GaussianBlur(img, (0, 0), 1.2)
    img = cv2.addWeighted(img, 1.5, blur, -0.5, 0)
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return out.tobytes() if ok else jpeg


class FaceTracker:
    """Plus grand visage de l'image (détecteur YuNet d'OpenCV, quelques ms sur CPU)."""

    def __init__(self) -> None:
        model = os.environ.get("OPAL_FACE_MODEL", "/app/face_detection_yunet.onnx")
        self._detector = cv2.FaceDetectorYN.create(model, "", (320, 240), 0.7)

    def find(self, jpeg: bytes) -> Face | None:
        """Plus grand visage : position normalisée 0..1, plus de quoi le reconnaître."""
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        if w > 320:
            img = cv2.resize(img, (320, int(h * 320 / w)))
            h, w = img.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(img)
        if faces is None or len(faces) == 0:
            return None
        row = max(faces, key=lambda f: f[2] * f[3])
        x, y, fw, fh = row[:4]
        return Face(float(x + fw / 2) / w, float(y + fh / 2) / h, float(fw) / w, row, img)


@dataclass
class Face:
    cx: float
    cy: float
    size: float
    row: np.ndarray  # ligne YuNet (boîte + points du visage), pour l'alignement SFace
    img: np.ndarray


class CameraHub:
    def __init__(
        self,
        on_gaze: Callable[[float, float], Awaitable[None]],
        on_presence: Callable[[bool], Awaitable[None]],
    ) -> None:
        self._on_gaze = on_gaze
        self._on_presence = on_presence
        self._tracker = FaceTracker()
        self._mirror = os.environ.get("OPAL_CAM_MIRROR", "0") == "1"
        self.ws: Any = None
        self.soft_jpeg = False
        self.source_name = ""
        self._sources: list[tuple[Any, dict[str, Any]]] = []  # (connexion, hello), la dernière active
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self.new_frame = asyncio.Condition()
        self.viewers = 0
        self.viewers_hd = 0
        self.on_recording: Callable[[bool], Awaitable[None]] | None = None
        self._watched = False
        self._enhanced: bytes | None = None
        self._enhanced_at = 0.0
        self._snapshot: asyncio.Future[bytes] | None = None
        self._analyzing = False
        self._hits: deque[bool] = deque(maxlen=5)
        self.present = False
        self.present_since = 0.0
        self.last_face = 0.0
        self._watchdog: asyncio.Task[None] | None = None
        self.photos: OrderedDict[str, bytes] = OrderedDict()  # photos prises pour Opal
        # Reconnaissance des visages (facultative : sans le modèle SFace, seulement la présence).
        try:
            self.faces: FaceBook | None = FaceBook()
        except cv2.error as exc:
            log("faces_disabled", error=str(exc)[:120])
            self.faces = None
        self._votes: deque[str] = deque(maxlen=5)  # derniers noms reconnus ("" = inconnu)
        self._last_identify = 0.0
        self._enrolling: dict[str, Any] | None = None
        # Gestes de la main (facultatif : sans les modèles, la caméra s'en passe).
        try:
            self.gestures: GestureDetector | None = GestureDetector()
        except cv2.error as exc:
            log("gestures_disabled", error=str(exc)[:120])
            self.gestures = None
        self.on_gesture: Callable[[str], Awaitable[None]] | None = None
        self._last_gesture: str | None = None
        self._gesture_at = 0.0
        self._gesture_check = 0.0

    @property
    def online(self) -> bool:
        return self.ws is not None

    @property
    def votes(self) -> tuple[str, ...]:
        return tuple(self._votes)

    @property
    def identity(self) -> str | None:
        """Qui est devant la caméra : un nom, "" pour un inconnu, None si on ne sait pas."""
        if not self._votes:
            return None
        return Counter(self._votes).most_common(1)[0][0]

    async def learn_face(self, name: str, samples: int = 6, timeout: float = 15.0) -> int:
        """Mémorise le visage présent devant la caméra sous ce nom."""
        if self.faces is None:
            raise RuntimeError("La reconnaissance des visages n'est pas installée.")
        if self.ws is None:
            raise RuntimeError("La caméra d'Opal Box n'est pas connectée.")
        done = asyncio.get_running_loop().create_future()
        self._enrolling = {"name": name, "embs": [], "want": samples, "done": done, "last": 0.0}
        try:
            embs = await asyncio.wait_for(done, timeout)
        except TimeoutError:
            embs = self._enrolling["embs"] if self._enrolling else []
        finally:
            self._enrolling = None
        if len(embs) < 2:
            raise RuntimeError("Je ne vois pas assez bien ton visage : mets-toi face à la caméra.")
        self.faces.remember(name, embs)
        self._votes.clear()
        return len(embs)

    def _identify(self, face: Face) -> tuple[str, float] | None:
        """(nom ou "" si inconnu, score), calculé au plus toutes les 1,5 s."""
        if self.faces is None:
            return None
        now = time.time()
        enrolling = self._enrolling
        # À l'arrivée, on identifie souvent (2 fois/s) ; ensuite toutes les 1,5 s suffisent.
        interval = 0.5 if not self.present or len(self._votes) < 3 else 1.5
        if enrolling is None and now - self._last_identify < interval:
            return None
        self._last_identify = now
        emb = self.faces.embedding(face.img, face.row)
        if enrolling is not None and now - enrolling["last"] > 0.3:
            enrolling["last"] = now
            enrolling["embs"].append(emb)
        name, score = self.faces.identify(emb)
        return name or "", score

    # ── Connexion de la caméra ──
    async def attach(self, ws: Any, hello: dict[str, Any] | None = None) -> None:
        """Nouvelle source d'images. La plus récente est active (ex. l'iPhone ouvert sur
        la page « envoyer » passe devant l'ESP32-CAM) ; à son départ la précédente reprend."""
        self._sources = [(w, h) for w, h in self._sources if w is not ws] + [(ws, hello or {})]
        await self._activate(*self._preferred_source())
        if ws is not self.ws:  # source en retrait : 1 image/s suffit, inutile de charger son WiFi
            try:
                await ws.send(json.dumps({"type": "cam", "fps": 1, "size": "qvga"}))
            except Exception:
                pass
        if not self.present:
            self.present_since = time.time()  # « personne depuis » compte depuis la connexion
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._presence_watchdog())

    def _preferred_source(self) -> tuple[Any, dict[str, Any]]:
        """Une page « envoyer » ouverte (iPhone, Mac) passe toujours avant l'ESP32-CAM,
        même si celle-ci se reconnecte après ; sinon la source la plus récente."""
        browsers = [s for s in self._sources if s[1].get("sensor") == "navigateur"]
        return (browsers or self._sources)[-1]

    async def _activate(self, ws: Any, hello: dict[str, Any]) -> None:
        if ws is self.ws:
            return
        self.ws = ws
        # Un capteur sans JPEG matériel (GC2145) compresse sur l'ESP32 : au-delà de
        # 2 à 3 images/s, son WiFi décroche. Un OV2640 ou un téléphone tiennent bien plus.
        self.soft_jpeg = bool(hello.get("soft_jpeg"))
        self.source_name = str(hello.get("sensor") or hello.get("device") or "caméra")
        log("camera_source", source=self.source_name)
        await self.apply_mode()

    async def detach(self, ws: Any) -> None:
        self._sources = [(w, h) for w, h in self._sources if w is not ws]
        if self.ws is not ws:
            return
        if self._snapshot and not self._snapshot.done():
            self._snapshot.set_exception(RuntimeError("Caméra déconnectée."))
        if self._sources:
            await self._activate(*self._preferred_source())
            return
        self.ws = None
        if self.present:
            await self._set_present(False)

    async def apply_mode(self) -> None:
        """Flux léger pour le suivi ; plus riche quand quelqu'un regarde le direct.

        Le direct a deux modes : « fluide » (petite image, plus d'images/s) et « net »
        (640x480). Avec un GC2145, le mode net plafonne vers 1 image/s : l'ESP32 met
        près d'une seconde à compresser chaque image.
        """
        if self.soft_jpeg:
            track = {"type": "cam", "fps": 2, "size": "qvga"}
            view = {"type": "cam", "fps": 3, "size": "qvga"}
            view_hd = {"type": "cam", "fps": 2, "size": "vga"}
        else:
            track = {"type": "cam", "fps": 4, "size": "qvga"}
            view = view_hd = {"type": "cam", "fps": 10, "size": "vga"}
        if self.source_name == "navigateur":
            # iPhone / Mac : 8 analyses/s pour un suivi de la tête bien fluide.
            track = {"type": "cam", "fps": 8, "size": "qvga"}
            view = {"type": "cam", "fps": 12, "size": "vga"}
        else:  # ESP32-CAM : réglages manuels possibles (alimentation dédiée…)
            if fps := os.environ.get("OPAL_CAM_FPS"):
                track["fps"] = int(fps)
            if fps := os.environ.get("OPAL_CAM_VIEW_FPS"):
                view["fps"] = int(fps)
        mode = view_hd if self.viewers_hd else view if self.viewers else track
        await self._send(mode)
        watched = self.viewers > 0
        if watched != self._watched and self.on_recording is not None:
            self._watched = watched
            await self.on_recording(watched)  # voyant rouge sur les yeux d'Opal Box

    def enhanced(self) -> bytes | None:
        """Dernière image améliorée pour le direct (calculée une fois par image)."""
        if self.frame is None:
            return None
        if self._enhanced_at != self.frame_at:
            self._enhanced = enhance(self.frame)
            self._enhanced_at = self.frame_at
        return self._enhanced

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps(payload))
            except Exception:
                pass

    # ── Images reçues ──
    async def on_binary(self, data: bytes, ws: Any = None) -> None:
        if len(data) < 2 or (ws is not None and ws is not self.ws):
            return  # source en retrait (une autre caméra a pris la main)
        kind, jpeg = data[0], data[1:]
        if kind == SNAPSHOT:
            if self._snapshot and not self._snapshot.done():
                self._snapshot.set_result(jpeg)
            return
        self.frame, self.frame_at = jpeg, time.time()
        async with self.new_frame:
            self.new_frame.notify_all()
        if not self._analyzing:  # on saute les images si l'analyse précédente n'est pas finie
            self._analyzing = True
            asyncio.create_task(self._analyze(jpeg))

    def _detect(self, jpeg: bytes) -> tuple[Face | None, tuple[str, float] | None, str | None]:
        face = self._tracker.find(jpeg)
        who = self._identify(face) if face is not None else None
        gesture = None
        # Un geste suppose quelqu'un devant la caméra ; au plus 2 analyses par seconde.
        now = time.time()
        if self.gestures is not None and self.present and now - self._gesture_check > 0.5:
            self._gesture_check = now
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                gesture = self.gestures.detect(img)
        return face, who, gesture

    async def _on_gesture_seen(self, gesture: str | None) -> None:
        """Geste confirmé s'il est vu deux fois de suite ; 4 s de pause entre deux gestes."""
        previous, self._last_gesture = self._last_gesture, gesture
        if gesture is None or gesture != previous or self.on_gesture is None:
            return
        now = time.time()
        if now - self._gesture_at < GESTURE_COOLDOWN_S:
            return
        self._gesture_at = now
        self._last_gesture = None
        log("gesture", gesture=gesture)
        await self.on_gesture(gesture)

    async def _analyze(self, jpeg: bytes) -> None:
        try:
            face, who, gesture = await asyncio.to_thread(self._detect, jpeg)
        finally:
            self._analyzing = False
        now = time.time()
        self._hits.append(face is not None)
        await self._on_gesture_seen(gesture)
        if who is not None:
            self._votes.append(who[0])
        enrolling = self._enrolling
        if enrolling and len(enrolling["embs"]) >= enrolling["want"] and not enrolling["done"].done():
            enrolling["done"].set_result(list(enrolling["embs"]))
        if face is not None:
            self.last_face = now
            cx, cy = face.cx, face.cy
            # La caméra regarde la personne : une personne à gauche de l'image est à droite
            # de l'écran vu de face. OPAL_CAM_MIRROR=1 inverse si l'image est déjà miroir.
            x = (1 - 2 * cx) if not self._mirror else (2 * cx - 1)
            y = (2 * cy - 1) * 0.8
            await self._on_gaze(max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)))
            if not self.present and sum(self._hits) >= PRESENCE_ARRIVE_HITS:
                await self._set_present(True)

    async def _presence_watchdog(self) -> None:
        while self.ws is not None:
            await asyncio.sleep(2)
            if self.present and time.time() - self.last_face > PRESENCE_LEAVE_S:
                await self._set_present(False)

    async def _set_present(self, present: bool) -> None:
        self.present = present
        self.present_since = time.time()
        log("presence", present=present)
        await self._on_presence(present)

    # ── Photo à la demande ──
    async def snapshot(self, flash: bool = False, timeout: float = 25.0) -> tuple[bytes, bool]:
        """Photo haute définition ; à défaut, la dernière image du flux.

        Retourne (jpeg, haute_definition). Avec un capteur sans JPEG matériel, la photo
        demande de redémarrer la caméra et de compresser une grande image : plusieurs
        secondes, d'où le délai large.
        """
        if self.ws is None:
            raise RuntimeError("La caméra d'Opal Box n'est pas connectée.")
        if self._snapshot is None or self._snapshot.done():
            self._snapshot = asyncio.get_running_loop().create_future()
            await self._send({"type": "snapshot", "flash": bool(flash)})
        try:
            return await asyncio.wait_for(asyncio.shield(self._snapshot), timeout), True
        except (TimeoutError, RuntimeError) as exc:
            log("snapshot_failed", error=repr(exc))
            if self.frame is not None and time.time() - self.frame_at < 10:
                return self.frame, False
            raise RuntimeError("La caméra n'a pas envoyé de photo à temps.") from exc

    def keep_photo(self, jpeg: bytes) -> str:
        """Garde la photo (les MAX_PHOTOS dernières) ; retourne son identifiant."""
        photo_id = secrets.token_urlsafe(9)
        self.photos[photo_id] = jpeg
        while len(self.photos) > MAX_PHOTOS:
            self.photos.popitem(last=False)
        return photo_id

    def presence_text(self) -> str:
        if self.ws is None:
            return "La caméra d'Opal Box n'est pas connectée."
        minutes = int((time.time() - self.present_since) // 60)
        duree = f"{minutes} min" if minutes else "moins d'une minute"
        if self.present:
            who = self.identity
            qui = who if who else "Quelqu'un que je ne connais pas" if who == "" else "Quelqu'un"
            return f"{qui} est devant Opal Box depuis {duree}."
        return f"Personne devant Opal Box depuis {duree}."
