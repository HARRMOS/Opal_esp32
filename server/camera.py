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
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

import cv2
import numpy as np

from logs import log

FRAME, SNAPSHOT = 1, 2
PRESENCE_ARRIVE_HITS = 3     # visages vus sur les 5 dernières analyses pour « arrivée »
PRESENCE_LEAVE_S = 45.0      # plus de visage depuis ce délai = « départ »
GAZE_HOLD_S = 1.2
MAX_PHOTOS = 50


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

    def find(self, jpeg: bytes) -> tuple[float, float, float] | None:
        """Retourne (cx, cy, taille) normalisés 0..1, ou None."""
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
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4]
        return float(x + fw / 2) / w, float(y + fh / 2) / h, float(fw) / w


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

    @property
    def online(self) -> bool:
        return self.ws is not None

    # ── Connexion de la caméra ──
    async def attach(self, ws: Any, hello: dict[str, Any] | None = None) -> None:
        self.ws = ws
        # Un capteur sans JPEG matériel (GC2145) compresse sur l'ESP32 : au-delà de
        # 2 à 3 images/s, son WiFi décroche. Un OV2640 tient des cadences plus hautes.
        self.soft_jpeg = bool((hello or {}).get("soft_jpeg"))
        if not self.present:
            self.present_since = time.time()  # « personne depuis » compte depuis la connexion
        await self.apply_mode()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._presence_watchdog())

    async def detach(self, ws: Any) -> None:
        if self.ws is ws:
            self.ws = None
            if self._snapshot and not self._snapshot.done():
                self._snapshot.set_exception(RuntimeError("Caméra déconnectée."))
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
        if fps := os.environ.get("OPAL_CAM_FPS"):  # réglage manuel du suivi
            track["fps"] = int(fps)
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
    async def on_binary(self, data: bytes) -> None:
        if len(data) < 2:
            return
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

    async def _analyze(self, jpeg: bytes) -> None:
        try:
            face = await asyncio.to_thread(self._tracker.find, jpeg)
        finally:
            self._analyzing = False
        now = time.time()
        self._hits.append(face is not None)
        if face is not None:
            self.last_face = now
            cx, cy, _ = face
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
            return f"Quelqu'un est devant Opal Box depuis {duree}."
        return f"Personne devant Opal Box depuis {duree}."
