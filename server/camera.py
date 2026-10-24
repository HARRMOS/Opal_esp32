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
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import cv2
import numpy as np

from logs import log

FRAME, SNAPSHOT = 1, 2
PRESENCE_ARRIVE_HITS = 3     # visages vus sur les 5 dernières analyses pour « arrivée »
PRESENCE_LEAVE_S = 45.0      # plus de visage depuis ce délai = « départ »
GAZE_HOLD_S = 1.2


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
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self.new_frame = asyncio.Condition()
        self.viewers = 0
        self._snapshot: asyncio.Future[bytes] | None = None
        self._analyzing = False
        self._hits: deque[bool] = deque(maxlen=5)
        self.present = False
        self.present_since = 0.0
        self.last_face = 0.0
        self._watchdog: asyncio.Task[None] | None = None

    @property
    def online(self) -> bool:
        return self.ws is not None

    # ── Connexion de la caméra ──
    async def attach(self, ws: Any) -> None:
        self.ws = ws
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
        """Flux léger pour le suivi ; plus net et plus fluide quand quelqu'un regarde le direct."""
        mode = {"type": "cam", "fps": 10, "size": "vga"} if self.viewers else {
            "type": "cam", "fps": 6, "size": "qvga"}
        await self._send(mode)

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
    async def snapshot(self, flash: bool = False, timeout: float = 6.0) -> bytes:
        if self.ws is None:
            raise RuntimeError("La caméra d'Opal Box n'est pas connectée.")
        if self._snapshot is None or self._snapshot.done():
            self._snapshot = asyncio.get_running_loop().create_future()
            await self._send({"type": "snapshot", "flash": bool(flash)})
        return await asyncio.wait_for(asyncio.shield(self._snapshot), timeout)

    def presence_text(self) -> str:
        if self.ws is None:
            return "La caméra d'Opal Box n'est pas connectée."
        minutes = int((time.time() - self.present_since) // 60)
        duree = f"{minutes} min" if minutes else "moins d'une minute"
        if self.present:
            return f"Quelqu'un est devant Opal Box depuis {duree}."
        return f"Personne devant Opal Box depuis {duree}."
