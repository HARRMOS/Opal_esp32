"""Visages connus d'Opal Box (reconnaissance SFace d'OpenCV, tout reste sur le VPS).

On ne garde aucune photo : seulement des « empreintes » de 128 nombres par visage,
dans OPAL_FACES_FILE (défaut /models/faces.json, volume du conteneur).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import cv2
import numpy as np

from logs import log

MATCH_THRESHOLD = 0.363  # similarité cosinus SFace : seuil conseillé par OpenCV
MAX_EMBEDDINGS = 20


class FaceBook:
    def __init__(self) -> None:
        model = os.environ.get("OPAL_FACE_ID_MODEL", "/app/face_recognition_sface.onnx")
        self._recognizer = cv2.FaceRecognizerSF.create(model, "")
        self._path = Path(os.environ.get("OPAL_FACES_FILE", "/models/faces.json"))
        self._lock = threading.Lock()
        self.people: dict[str, list[np.ndarray]] = {}
        if self._path.is_file():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self.people = {name: [np.array(e, np.float32) for e in embs] for name, embs in raw.items()}
            log("faces_loaded", people=list(self.people))

    def embedding(self, img: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        crop = self._recognizer.alignCrop(img, face_row)
        return self._recognizer.feature(crop).flatten().astype(np.float32)

    def identify(self, emb: np.ndarray) -> tuple[str | None, float]:
        """Nom de la personne la plus ressemblante, ou None si personne ne l'est assez."""
        best, score = None, 0.0
        with self._lock:
            for name, embs in self.people.items():
                for known in embs:
                    s = float(np.dot(emb, known) / (np.linalg.norm(emb) * np.linalg.norm(known)))
                    if s > score:
                        best, score = name, s
        return (best if score >= MATCH_THRESHOLD else None), score

    def remember(self, name: str, embs: list[np.ndarray]) -> None:
        with self._lock:
            self.people[name] = (self.people.get(name, []) + embs)[-MAX_EMBEDDINGS:]
            self._save()
        log("face_remembered", name=name, samples=len(self.people[name]))

    def forget(self, name: str) -> bool:
        with self._lock:
            found = self.people.pop(name, None) is not None
            self._save()
        return found

    def _save(self) -> None:
        data = {name: [e.tolist() for e in embs] for name, embs in self.people.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self._path)
