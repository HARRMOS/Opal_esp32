"""Posture du haut du corps devant la caméra (BlazePose, modèles MediaPipe d'OpenCV Zoo).

Sert au geste « bras croisés en X » (pause de la musique) et au dessin des bras sur
le direct. Points BlazePose utilisés : 11/12 épaules, 13/14 coudes, 15/16 poignets
(gauche/droite de la personne).
"""

from __future__ import annotations

import os

import numpy as np

from vendor.mp_persondet import MPPersonDet
from vendor.mp_pose import MPPose

ARM_POINTS = (11, 12, 13, 14, 15, 16)
MIN_VISIBILITY = 0.5


def classify_arms(lm: np.ndarray) -> str | None:
    """« bras_croises » si les avant-bras forment un X devant le buste.

    lm : (39, 5) points écran x, y, z, visibilité, présence (y vers le bas).
    """
    if any(lm[i, 3] < MIN_VISIBILITY for i in ARM_POINTS):
        return None
    sl, sr, el, er, wl, wr = (lm[i, :2] for i in ARM_POINTS)
    shoulder_w = float(np.linalg.norm(sl - sr))
    if shoulder_w < 1:
        return None
    # Poignets passés de l'autre côté : leur ordre gauche/droite est l'inverse des épaules.
    swapped = (wl[0] - wr[0]) * (sl[0] - sr[0]) < 0
    # Avant-bras levés devant le buste : poignets au-dessus des coudes, à hauteur du torse.
    raised = wl[1] < el[1] and wr[1] < er[1]
    top, bottom = min(sl[1], sr[1]) - 0.9 * shoulder_w, max(sl[1], sr[1]) + 1.3 * shoulder_w
    chest = all(top < w[1] < bottom for w in (wl, wr))
    # Les poignets restent près du centre du corps (pas les bras grands ouverts).
    mid_x = (sl[0] + sr[0]) / 2
    close = all(abs(w[0] - mid_x) < 0.9 * shoulder_w for w in (wl, wr))
    return "bras_croises" if swapped and raised and chest and close else None


class PoseDetector:
    def __init__(self) -> None:
        self._person = MPPersonDet(
            os.environ.get("OPAL_PERSON_MODEL", "/app/person_detection_mediapipe.onnx"),
            nmsThreshold=0.3,
            scoreThreshold=0.5,
            topK=1,
        )
        self._pose = MPPose(
            os.environ.get("OPAL_POSE_MODEL", "/app/pose_estimation_mediapipe.onnx"),
            confThreshold=0.6,
        )

    def detect(self, img: np.ndarray) -> tuple[str | None, dict | None]:
        """(geste des bras ou None, bras vus : points normalisés des épaules, coudes, poignets)."""
        h, w = img.shape[:2]
        persons = self._person.infer(img)
        if persons is None or len(persons) == 0:
            return None, None
        result = self._pose.infer(img, persons[0])
        if result is None:
            return None, None
        lm = result[1]
        seen = {
            "arms": [
                (float(lm[i, 0]) / w, float(lm[i, 1]) / h, float(lm[i, 3])) for i in ARM_POINTS
            ]
        }
        return classify_arms(lm), seen
