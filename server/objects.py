"""Objets devant la caméra d'Opal Box (téléphone, tasse, bouteille, livre, ordinateur…).

Modèle NanoDet-Plus d'OpenCV Zoo (vendor/nanodet.py), entraîné sur les 80 catégories COCO.
L'image est mise au format 416×416 sans déformation (bandes noires), puis les boîtes sont
ramenées aux coordonnées de l'image, normalisées entre 0 et 1.
"""

from __future__ import annotations

import os

import cv2
import numpy as np

from vendor.nanodet import NanoDet

INPUT = 416
COCO = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich",
    "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)
FRENCH = {
    "person": "personne", "cell phone": "téléphone", "cup": "tasse", "bottle": "bouteille",
    "wine glass": "verre", "book": "livre", "laptop": "ordinateur", "keyboard": "clavier",
    "mouse": "souris", "remote": "télécommande", "scissors": "ciseaux", "cat": "chat",
    "dog": "chien", "banana": "banane", "apple": "pomme", "orange": "orange",
    "sandwich": "sandwich", "pizza": "pizza", "donut": "beignet", "cake": "gâteau",
    "bowl": "bol", "fork": "fourchette", "spoon": "cuillère", "knife": "couteau",
    "toothbrush": "brosse à dents", "clock": "horloge", "backpack": "sac à dos",
    "handbag": "sac", "umbrella": "parapluie", "tv": "écran", "chair": "chaise",
    "couch": "canapé", "bed": "lit", "potted plant": "plante", "teddy bear": "peluche",
    "vase": "vase", "dining table": "table", "sports ball": "ballon", "tie": "cravate",
    "suitcase": "valise", "hair drier": "sèche-cheveux", "bird": "oiseau",
}


def letterbox(img: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Image ramenée à 416×416 sans la déformer : (image, (haut, gauche, h utile, l utile))."""
    h, w = img.shape[:2]
    scale = INPUT / max(h, w)
    nh, nw = round(h * scale), round(w * scale)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    top, left = (INPUT - nh) // 2, (INPUT - nw) // 2
    boxed = cv2.copyMakeBorder(resized, top, INPUT - nh - top, left, INPUT - nw - left,
                               cv2.BORDER_CONSTANT, value=0)
    return boxed, (top, left, nh, nw)


class _NanoDet(NanoDet):
    def infer(self, srcimg: np.ndarray) -> np.ndarray:
        self.net.setInput(self.pre_process(srcimg))
        outs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        # L'ordre des sorties change selon la version d'OpenCV : post_process attend
        # score, boîte, score, boîte… du niveau le plus fin (le plus d'ancres) au plus grossier.
        scores = sorted((o for o in outs if o.shape[-1] == len(COCO)), key=lambda o: -o.shape[-2])
        boxes = sorted((o for o in outs if o.shape[-1] != len(COCO)), key=lambda o: -o.shape[-2])
        return self.post_process([o for pair in zip(scores, boxes) for o in pair])


class ObjectDetector:
    def __init__(self, min_score: float = 0.4) -> None:
        self._net = _NanoDet(
            os.environ.get("OPAL_OBJECT_MODEL", "/app/object_detection_nanodet.onnx"),
            prob_threshold=min_score, iou_threshold=0.6,
        )

    def detect(self, img: np.ndarray) -> list[dict]:
        """Objets vus sur l'image BGR : [{label, fr, score, box (x, y, l, h normalisés)}]."""
        h, w = img.shape[:2]
        boxed, (top, left, nh, nw) = letterbox(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        found = []
        for x1, y1, x2, y2, score, cls in self._net.infer(boxed):
            # Retour aux coordonnées de l'image d'origine, puis entre 0 et 1.
            x1, x2 = ((np.array([x1, x2]) - left) / nw).clip(0, 1)
            y1, y2 = ((np.array([y1, y2]) - top) / nh).clip(0, 1)
            if x2 - x1 < 0.01 or y2 - y1 < 0.01:
                continue
            label = COCO[int(cls)]
            found.append({
                "label": label, "fr": FRENCH.get(label, label), "score": round(float(score), 2),
                "box": (float(x1), float(y1), float(x2 - x1), float(y2 - y1)),
            })
        return found
