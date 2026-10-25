"""Ce que dit le visage : expression (sourire, surprise, tristesse…) et regard vers Opal.

Expression : modèle MobileFaceNet d'OpenCV Zoo (facial_expression_recognition), sur le
visage aligné à 112×112 grâce aux 5 points de YuNet. Regard : géométrie de ces mêmes
points (nez centré entre les yeux = la personne regarde la caméra).
"""

from __future__ import annotations

import os

import cv2
import numpy as np

EXPRESSIONS = ("angry", "disgust", "fearful", "happy", "neutral", "sad", "surprised")
# Position des yeux, du nez et des coins de la bouche sur un visage aligné 112×112.
_REFERENCE = np.float32([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                         [41.5493, 92.3655], [70.7299, 92.2041]])


class ExpressionReader:
    def __init__(self) -> None:
        self._net = cv2.dnn.readNet(
            os.environ.get("OPAL_EXPRESSION_MODEL", "/app/facial_expression_recognition.onnx")
        )

    def read(self, img: np.ndarray, row: np.ndarray) -> tuple[str, float]:
        """(expression, confiance) du visage YuNet `row` dans l'image BGR `img`."""
        points = np.float32(row[4:14]).reshape(5, 2)
        matrix, _ = cv2.estimateAffinePartial2D(points, _REFERENCE, method=cv2.LMEDS)
        if matrix is None:
            return "neutral", 0.0
        face = cv2.warpAffine(img, matrix, (112, 112))
        rgb = (cv2.cvtColor(face, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0 - 0.5) / 0.5
        self._net.setInput(cv2.dnn.blobFromImage(rgb), "data")
        scores = self._net.forward("label").reshape(-1).astype(np.float64)
        probs = np.exp(scores - scores.max())
        probs /= probs.sum()
        best = int(np.argmax(probs))
        return EXPRESSIONS[best], float(probs[best])


def looks_at_camera(row: np.ndarray) -> bool:
    """Visage tourné vers la caméra : nez centré entre les yeux, ni trop haut ni trop bas."""
    left_eye, right_eye, nose, mouth_l, mouth_r = np.float32(row[4:14]).reshape(5, 2)
    eyes = (left_eye + right_eye) / 2
    eye_gap = float(np.linalg.norm(right_eye - left_eye))
    mouth_y = (mouth_l[1] + mouth_r[1]) / 2
    if eye_gap < 1 or mouth_y - eyes[1] < 1:
        return False
    yaw = (nose[0] - eyes[0]) / eye_gap  # ~0 de face, ±0,3 et plus de profil
    pitch = (nose[1] - eyes[1]) / (mouth_y - eyes[1])  # ~0,5 de face
    return bool(abs(yaw) < 0.13 and 0.35 < pitch < 0.68)  # bool Python (JSON), pas numpy
