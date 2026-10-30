"""Incrustation « vision d'Opal » dans les images du direct.

Dessiné image par image sur le VPS (donc parfaitement synchronisé, visible partout) :
coins de viseur autour du visage avec prénom et ressemblance, points du visage,
squelette de la main avec le geste reconnu, bandeau d'état en haut.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FRESH_S = 0.8  # au-delà, une détection n'est plus dessinée
OBJECTS_FRESH_S = 1.6  # les objets ne sont cherchés qu'une fois par seconde
YELLOW = (242, 201, 76)

GREEN, ORANGE, GREY, BLUE = (79, 209, 139), (242, 162, 74), (154, 166, 189), (90, 169, 255)
GESTURES = {"pouce_haut": "Pouce levé", "pouce_bas": "Pouce baissé", "main_ouverte": "Main ouverte",
            "ok": "Signe OK", "bras_croises": "Bras croisés"}
PURPLE = (180, 140, 255)
FEELINGS = {"happy": "sourit", "surprised": "surpris", "sad": "triste", "angry": "fâché",
            "fearful": "inquiet", "disgust": "dégoûté"}
# Liaisons des 21 points de la main (MediaPipe) : poignet, pouce, index, majeur, annulaire, auriculaire
HAND_LINKS = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10),
              (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (0, 17),
              (17, 18), (18, 19), (19, 20)]
FINGER_TIPS = (4, 8, 12, 16, 20)
# Bras (pose.py) : épaules 0/1, coudes 2/3, poignets 4/5
ARM_LINKS = [(0, 1), (0, 2), (2, 4), (1, 3), (3, 5)]

_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _corners(img: np.ndarray, x1: int, y1: int, x2: int, y2: int, color: tuple, t: int) -> None:
    """Coins de viseur plutôt qu'un cadre plein : plus léger, style « vision de robot »."""
    bgr = color[::-1]
    arm = max(8, min(x2 - x1, y2 - y1) // 4)
    for (cx, cy, dx, dy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
        cv2.line(img, (cx, cy), (cx + dx * arm, cy), bgr, t, cv2.LINE_AA)
        cv2.line(img, (cx, cy), (cx, cy + dy * arm), bgr, t, cv2.LINE_AA)


def _labels(img: np.ndarray, items: list[tuple[str, int, int, tuple]]) -> np.ndarray:
    """Étiquettes arrondies (texte avec accents, via Pillow) : (texte, x, y_bas, couleur)."""
    if not items:
        return img
    h, w = img.shape[:2]
    size = max(14, w // 34)
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    font = _font(size)
    for text, x, y, color in items:
        tw = draw.textlength(text, font=font)
        pad = size * 0.45
        bw, bh = tw + 2 * pad, size * 1.45
        top = y - bh - 4 if y - bh - 4 > 0 else y + 4
        x = int(min(max(0, x), w - bw))
        draw.rounded_rectangle((x, top, x + bw, top + bh), radius=bh / 2, fill=color)
        draw.text((x + pad, top + bh / 2), text, font=font, fill=(11, 13, 18), anchor="lm")
    return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def draw(img: np.ndarray, info: dict[str, Any]) -> np.ndarray:
    """Dessine les détections récentes de `info` (voir CameraHub.infos) sur l'image BGR."""
    h, w = img.shape[:2]
    now = info.get("now", 0)
    t = max(2, w // 220)
    labels: list[tuple[str, int, int, tuple]] = []

    face = info.get("face")
    if face and now - face.get("at", 0) < FRESH_S:
        name, score = face.get("name"), face.get("score")
        color = GREEN if name else ORANGE if name == "" else GREY
        text = name if name else "Inconnu" if name == "" else "Visage…"
        if name is not None and score is not None:
            text += f"  {round(score * 100)} %"
        if feeling := FEELINGS.get(face.get("expression") or ""):
            text += f"  ·  {feeling}"
        if face.get("facing"):
            text += "  ·  me regarde"
        x, y, bw, bh = face["box"]
        x1, y1, x2, y2 = int(x * w), int(y * h), int((x + bw) * w), int((y + bh) * h)
        _corners(img, x1, y1, x2, y2, color, t)
        for px, py in face.get("points") or []:
            cv2.circle(img, (int(px * w), int(py * h)), max(2, t), color[::-1], -1, cv2.LINE_AA)
        labels.append((text, x1, y1, color))

    hand = info.get("hand")
    if hand and now - hand.get("at", 0) < FRESH_S:
        pts = [(int(px * w), int(py * h)) for px, py in hand.get("points") or []]
        if len(pts) == 21:
            for a, b in HAND_LINKS:
                cv2.line(img, pts[a], pts[b], BLUE[::-1], t, cv2.LINE_AA)
            for i, p in enumerate(pts):
                r = t + 2 if i in FINGER_TIPS else t + 1
                cv2.circle(img, p, r, (255, 255, 255), -1, cv2.LINE_AA)
                cv2.circle(img, p, r, BLUE[::-1], 1, cv2.LINE_AA)
        x, y = hand["box"][:2]
        gesture = GESTURES.get(hand.get("gesture") or "", "Main")
        labels.append((gesture, int(x * w), int(y * h), BLUE))

    arms = info.get("arms")
    if arms and now - arms.get("at", 0) < FRESH_S and len(arms.get("arms") or []) == 6:
        pts = [(int(x * w), int(y * h), vis) for x, y, vis in arms["arms"]]
        crossed = arms.get("gesture") == "bras_croises"
        color = (ORANGE if crossed else PURPLE)[::-1]
        for a, b in ARM_LINKS:
            if pts[a][2] >= 0.5 and pts[b][2] >= 0.5:
                cv2.line(img, pts[a][:2], pts[b][:2], color, t + 1, cv2.LINE_AA)
        if crossed:
            labels.append((GESTURES["bras_croises"], min(pts[4][0], pts[5][0]),
                           min(pts[4][1], pts[5][1]), ORANGE))

    # Objets (analysés une fois par seconde : gardés un peu plus longtemps à l'écran).
    objects = info.get("objects")
    people = 0
    if objects and now - objects.get("at", 0) < OBJECTS_FRESH_S:
        for obj in objects.get("items") or []:
            if obj["label"] == "person":  # le visage a déjà son cadre : on compte seulement
                people += 1
                continue
            x, y, bw, bh = obj["box"]
            x1, y1, x2, y2 = int(x * w), int(y * h), int((x + bw) * w), int((y + bh) * h)
            cv2.rectangle(img, (x1, y1), (x2, y2), YELLOW[::-1], max(1, t - 1), cv2.LINE_AA)
            labels.append((f"{obj['fr']}  {round(obj['score'] * 100)} %", x1, y1, YELLOW))

    # Bandeau d'état en haut à gauche.
    who = info.get("who")
    state = (f"{who} est là" if who else "Inconnu" if who == "" else "Quelqu'un est là"
             ) if info.get("present") else "Personne"
    if people >= 2:
        state += f"  ·  {people} personnes"
    last = info.get("last_gesture")
    if last and now - last["at"] < 5:
        state += f"  ·  {GESTURES.get(last['name'], last['name'])} !"
    labels.append((f"OPAL · {state}", 8, int(h * 0.02) + max(14, w // 34) * 2, (231, 225, 217)))
    return _labels(img, labels)
