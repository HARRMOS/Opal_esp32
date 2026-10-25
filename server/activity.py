"""Opal Box reflète ce que fait Opal, quel que soit l'appareil où on lui parle.

Le pont s'abonne au flux temps réel de l'interface web d'Opal (WebSocket /ws,
« Authorization: Bearer <jeton distant> ») et traduit chaque événement en
expression des yeux :

  message de l'utilisateur  → attentif, regarde en face
  réflexion                 → regarde en l'air
  outil sur un ordinateur   → concentré, regarde « l'écran » (en bas)
  réponse                   → émotion devinée dans le texte
  erreur                    → triste
  demande de confirmation   → surpris, attend (un pouce levé la valide, voir gestures)
  notification              → clignement surpris

Si Opal a lui-même choisi une expression (outil opal_box_yeux) pendant ce tour,
la réponse ne la remplace pas.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import connect

from logs import log

SendEyes = Callable[[dict[str, Any]], Awaitable[int]]

# Outils qui ont déjà leur propre animation sur Opal Box.
_BOX_TOOLS = ("opal_box_yeux", "opal_box_photo", "opal_box_presence")


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


_SAD = re.compile(
    r"\b(desole|malheureusement|impossible|echec|echoue|erreur|pas pu|n'ai pas pu|"
    r"je ne peux pas|introuvable|helas|navre)\b"
)
_HAPPY = re.compile(
    r"\b(super|parfait|genial|excellent|c'est fait|voila|ravi|avec plaisir|bravo|"
    r"felicitations|top|cool|reussi|termine)\b|[😀😃😄😁😊🙂😉🎉]"
)
_SURPRISED = re.compile(r"\b(oh|wow|incroyable|etonnant|surprenant|waouh)\b|!\?|\?!")


def mood_for(text: str) -> str:
    """Émotion simple d'une réponse (mots-clés français, sans appel à une IA)."""
    plain = _plain(text[:600])
    if _SAD.search(plain):
        return "sad"
    if _SURPRISED.search(plain):
        return "surprised"
    if _HAPPY.search(plain):
        return "happy"
    return "neutral"


class OpalActivity:
    def __init__(
        self,
        url: str,
        token: str,
        send_eyes: SendEyes,
        on_confirm_request: Callable[[str | None], None] | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._send_eyes = send_eyes
        self._on_confirm_request = on_confirm_request
        self._state = ""
        self._opal_chose_eyes = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                async with connect(
                    self._url,
                    additional_headers={"Authorization": f"Bearer {self._token}"},
                    max_size=None,
                ) as ws:
                    log("activity_connected", url=self._url)
                    async for message in ws:
                        try:
                            event = json.loads(message)
                        except ValueError:
                            continue
                        await self._handle(event)
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException) as exc:
                log("activity_link_lost", error=str(exc))
            await asyncio.sleep(5)

    async def _eyes(self, state: str, **cmd: Any) -> None:
        """Envoie une expression, sans répéter le même état (la réflexion est bavarde)."""
        if state == self._state and state in ("thinking", "tool"):
            return
        self._state = state
        sent = await self._send_eyes({"type": "eyes", **cmd})
        log("activity", state=state, mood=cmd.get("mood"), devices=sent)

    async def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "user_message":
            self._opal_chose_eyes = False
            await self._eyes("listening", mood="surprised", look=[0, 0], hold_ms=4000)
        elif kind in ("thinking", "iteration", "plan_step_start"):
            await self._eyes("thinking", mood="neutral", look=[0.55, -0.6], hold_ms=20000)
        elif kind == "tool_call":
            tool = str(data.get("tool") or data.get("name") or "")
            if tool in _BOX_TOOLS:
                self._opal_chose_eyes = self._opal_chose_eyes or tool == "opal_box_yeux"
                self._state = "box_tool"
                return
            await self._eyes("tool", mood="neutral", look=[-0.25, 0.55], pupil=0.25, hold_ms=20000)
        elif kind == "assistant_message":
            if self._opal_chose_eyes:
                self._state = "answered"
                return
            text = str(data.get("content") or "")
            await self._eyes("answered", mood=mood_for(text), look="auto", pupil=-1, hold_ms=6000)
        elif kind == "error":
            await self._eyes("error", mood="sad", look=[0, 0.4], pupil=-1, hold_ms=5000)
        elif kind == "confirm_request":
            if self._on_confirm_request:
                self._on_confirm_request(str(data.get("id") or "") or None)
            await self._eyes("confirm", mood="surprised", look=[0, 0], blink=True, hold_ms=60000)
        elif kind == "confirm_resolved":
            if self._on_confirm_request:
                self._on_confirm_request(None)
            await self._eyes("confirmed", mood="happy", look="auto", hold_ms=2500)
        elif kind in ("notice", "node_event", "briefing", "suggestion"):
            if kind == "notice" and "opal-box" in str(data.get("message", "")).lower():
                return  # ses propres connexions / déconnexions
            await self._eyes("notify", mood="surprised", blink=True, hold_ms=2500)
