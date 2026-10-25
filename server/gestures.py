"""Gestes de la main devant la caméra d'Opal Box.

Détection de la paume puis des 21 points de la main (modèles MediaPipe d'OpenCV Zoo,
vendor/), puis reconnaissance par la géométrie des doigts :

    main_ouverte  les cinq doigts tendus      → coucou : la boîte se réveille
    pouce_haut    pouce tendu vers le haut    → valide la confirmation d'Opal en attente
    pouce_bas     pouce tendu vers le bas     → la refuse
    ok            👌 pouce contre index       → lance la playlist (voir opal_bridge)

Les bras croisés (pause de la musique) sont dans pose.py.
"""

from __future__ import annotations

import os

import numpy as np

from vendor.mp_handpose import MPHandPose
from vendor.mp_palmdet import MPPalmDet

# Indices MediaPipe : 0 poignet ; pouce 1-4 ; index 5-8 ; majeur 9-12 ; annulaire 13-16 ; auriculaire 17-20
_FINGERS = ((6, 8), (10, 12), (14, 16), (18, 20))  # (articulation du milieu, bout)


def classify(points: np.ndarray) -> str | None:
    """Geste à partir des 21 points (x, y) de la main, y vers le bas."""
    pts = points[:, :2]
    wrist = pts[0]
    size = np.linalg.norm(pts[9] - wrist)  # poignet → base du majeur : taille de la main
    if size < 1:
        return None

    def far(i: int) -> float:
        return float(np.linalg.norm(pts[i] - wrist))

    extended = [far(tip) > far(pip) * 1.12 for pip, tip in _FINGERS]
    folded = [far(tip) < far(pip) * 1.02 for pip, tip in _FINGERS]
    thumb_out = np.linalg.norm(pts[4] - pts[5]) > 0.55 * size and (
        np.linalg.norm(pts[4] - pts[5]) > np.linalg.norm(pts[3] - pts[5]) * 1.1
    )

    # « Coucou » : main levée, doigts bien droits, vers le haut et écartés. Une main qui
    # passe de côté, à plat, ou qui tient un objet (doigts serrés ou repliés) ne compte pas.
    straight = all(far(tip) > far(pip) * 1.2 for pip, tip in _FINGERS)
    upright = all(pts[tip][1] < wrist[1] - 1.2 * size for _, tip in _FINGERS)
    tips = pts[[8, 12, 16, 20]]
    spread = float(np.mean(np.linalg.norm(np.diff(tips, axis=0), axis=1))) > 0.28 * size
    if straight and upright and spread and thumb_out:
        return "main_ouverte"
    # 👌 : bout du pouce contre le bout de l'index (cercle), les trois autres doigts tendus.
    circle = np.linalg.norm(pts[4] - pts[8]) < 0.35 * size
    others_up = all(far(tip) > far(pip) * 1.12 for pip, tip in _FINGERS[1:])
    if circle and others_up:
        return "ok"
    if all(extended):
        return None  # main ouverte mais pas un vrai « coucou »
    if all(folded) and thumb_out:
        knuckles_y = float(np.mean(pts[[5, 9, 13, 17], 1]))
        if pts[4][1] < knuckles_y - 0.45 * size:
            return "pouce_haut"
        if pts[4][1] > knuckles_y + 0.45 * size:
            return "pouce_bas"
    return None


class GestureDetector:
    def __init__(self) -> None:
        self._palm = MPPalmDet(
            os.environ.get("OPAL_PALM_MODEL", "/app/palm_detection_mediapipe.onnx"),
            nmsThreshold=0.3,
            scoreThreshold=0.6,
        )
        self._hand = MPHandPose(
            os.environ.get("OPAL_HAND_MODEL", "/app/handpose_estimation_mediapipe.onnx"),
            confThreshold=0.8,
        )

    def detect(self, img: np.ndarray) -> tuple[str | None, dict | None]:
        """(geste reconnu ou None, main vue : boîte et 21 points normalisés, ou None)."""
        h, w = img.shape[:2]
        palms = self._palm.infer(img)
        found = None
        for palm in palms[:2]:
            hand = self._hand.infer(img, palm)
            if hand is None:
                continue
            x1, y1, x2, y2 = (float(v) for v in hand[:4])
            points = hand[4:67].reshape(21, 3)
            seen = {
                "box": (max(0.0, x1 / w), max(0.0, y1 / h), (x2 - x1) / w, (y2 - y1) / h),
                "points": [(float(px) / w, float(py) / h) for px, py, _ in points],
            }
            found = found or seen
            bx, by, bw, bh = seen["box"]
            if bx < 0.01 or by < 0.01 or bx + bw > 0.99 or by + bh > 0.99:
                continue  # main coupée par le bord : on ne devine pas de geste
            gesture = classify(points)
            if gesture:
                return gesture, seen
        return None, found
