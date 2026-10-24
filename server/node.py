"""Opal Box vue par Opal : un nœud, comme le PC et le Mac, qui apporte ses outils.

Protocole des nœuds d'Opal (WebSocket /node, « Authorization: Bearer <node_token> ») :
  nœud → Opal  {"type": "hello", "name", "platform", "tools": [schémas OpenAI], "environment"}
  Opal → nœud  {"type": "call", "id", "tool", "arguments", "timeout"}
  nœud → Opal  {"type": "result", "id", "success", "output", "error"}

  nœud → Opal  {"type": "event", "event", "data"}   (présence devant la caméra)

Le nœud n'est connecté que tant qu'au moins un appareil (yeux ou caméra) l'est :
Opal sait donc toujours si Opal Box est allumée.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import connect

from logs import log

NODE_NAME = "opal-box"

MOODS = {
    "neutre": "neutral",
    "content": "happy",
    "triste": "sad",
    "fache": "angry",
    "surpris": "surprised",
    "endormi": "sleepy",
}
LOOKS = {
    "centre": (0.0, 0.0),
    "gauche": (-0.8, 0.0),
    "droite": (0.8, 0.0),
    "haut": (0.0, -0.8),
    "bas": (0.0, 0.8),
    "haut_gauche": (-0.6, -0.6),
    "haut_droite": (0.6, -0.6),
    "bas_gauche": (-0.6, 0.6),
    "bas_droite": (0.6, 0.6),
}
PUPILS = {"petite": 0.1, "normale": -1.0, "grande": 0.95}

EYES_TOOL = {
    "type": "function",
    "function": {
        "name": "opal_box_yeux",
        "description": (
            "Contrôle les deux yeux (écrans ronds) d'Opal Box, ton boîtier physique posé "
            "devant l'utilisateur. Sert à exprimer une émotion ou à regarder quelque part : "
            "par exemple content quand tu fais une blague, surpris face à une nouvelle, "
            "clin_d_oeil pour un clin d'œil complice. Tous les champs sont facultatifs et combinables. "
            "Après « duree_s » secondes, les yeux reprennent leur comportement automatique."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "humeur": {"type": "string", "enum": list(MOODS)},
                "regard": {
                    "type": "string",
                    "enum": [*LOOKS, "libre"],
                    "description": "Direction vue par la personne en face ; « libre » = regard autonome.",
                },
                "cligner": {"type": "boolean", "description": "Cligner des deux yeux."},
                "clin_d_oeil": {
                    "type": "string",
                    "enum": ["gauche", "droite"],
                    "description": "Clin d'œil d'un seul œil (vu par la personne en face).",
                },
                "pupille": {"type": "string", "enum": list(PUPILS)},
                "duree_s": {
                    "type": "number",
                    "description": (
                        "Durée de l'expression en secondes (défaut 8 ; 0 = jusqu'à ce que "
                        "l'utilisateur reparle ou qu'une autre expression soit demandée)."
                    ),
                },
            },
        },
    },
}

PHOTO_TOOL = {
    "type": "function",
    "function": {
        "name": "opal_box_photo",
        "description": (
            "Prend une photo avec la caméra d'Opal Box (ce qui est devant toi, chez "
            "l'utilisateur) et la décrit. À utiliser quand on te demande ce que tu vois, "
            "qui est là, ou de regarder un objet montré à la caméra."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Ce qu'il faut regarder ou décrire sur la photo.",
                },
                "flash": {"type": "boolean", "description": "Allumer le flash (pièce sombre)."},
            },
        },
    },
}

PRESENCE_TOOL = {
    "type": "function",
    "function": {
        "name": "opal_box_presence",
        "description": "Dit si quelqu'un est devant Opal Box (caméra) et depuis combien de temps.",
        "parameters": {"type": "object", "properties": {}},
    },
}

TOOLS = [EYES_TOOL, PHOTO_TOOL, PRESENCE_TOOL]

ENVIRONMENT = (
    "## OPAL BOX\n"
    "« opal-box » n'est pas un ordinateur : c'est ton corps physique posé chez l'utilisateur, "
    "avec deux yeux animés (opal_box_yeux) et une caméra (opal_box_photo, opal_box_presence). "
    "Ne lui envoie jamais d'outil d'ordinateur (apps, fichiers, navigateur). Tes yeux suivent "
    "seuls le visage de la personne en face. Utilise tes yeux avec naturel pour accompagner "
    "tes réponses, sans le mentionner à chaque fois."
)


def eyes_command(args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Arguments de l'outil → message pour l'ESP32 + résumé lisible pour Opal."""
    cmd: dict[str, Any] = {"type": "eyes"}
    done: list[str] = []
    if (mood := args.get("humeur")) in MOODS:
        cmd["mood"] = MOODS[mood]
        done.append(f"humeur {mood}")
    if (look := args.get("regard")) == "libre":
        cmd["look"] = "auto"
        done.append("regard libre")
    elif look in LOOKS:
        cmd["look"] = list(LOOKS[look])
        done.append(f"regarde {look.replace('_', ' ')}")
    if args.get("cligner"):
        cmd["blink"] = True
        done.append("clignement")
    if (wink := args.get("clin_d_oeil")) in ("gauche", "droite"):
        cmd["wink"] = 0 if wink == "gauche" else 1
        done.append(f"clin d'œil {wink}")
    if (pupil := args.get("pupille")) in PUPILS:
        cmd["pupil"] = PUPILS[pupil]
        done.append(f"pupilles {pupil}s")
    hold = float(args.get("duree_s", 8) or 0)
    cmd["hold_ms"] = int(max(0.0, min(hold, 600.0)) * 1000)
    return cmd, ", ".join(done)


ToolHandler = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]]


class OpalNode:
    """Liaison nœud vers Opal, active seulement quand un appareil est en ligne."""

    def __init__(self, url: str, token: str, handler: ToolHandler) -> None:
        self._url = url
        self._token = token
        self._handler = handler
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def send_event(self, event: str, data: dict[str, Any]) -> None:
        """Remonte un événement à Opal (affiché, et déclencheur « sur événement »)."""
        if self._ws is None:
            return
        try:
            await self._ws.send(
                json.dumps({"type": "event", "event": event, "data": data}, ensure_ascii=False)
            )
        except websockets.WebSocketException:
            pass

    async def _run(self) -> None:
        while True:
            try:
                async with connect(
                    self._url, additional_headers={"Authorization": f"Bearer {self._token}"}
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "name": NODE_NAME,
                                "platform": "esp32",
                                "tools": TOOLS,
                                "environment": ENVIRONMENT,
                            },
                            ensure_ascii=False,
                        )
                    )
                    self._ws = ws
                    log("node_connected", url=self._url)
                    async for message in ws:
                        msg = json.loads(message)
                        if msg.get("type") == "call":
                            # Une photo prend quelques secondes : ne pas bloquer les autres appels.
                            asyncio.create_task(self._call(ws, msg))
            except asyncio.CancelledError:
                log("node_disconnected")
                raise
            except (OSError, websockets.WebSocketException) as exc:
                log("node_link_lost", error=str(exc))
            finally:
                self._ws = None
            await asyncio.sleep(3)

    async def _call(self, ws: Any, msg: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"type": "result", "id": msg.get("id")}
        try:
            ok, text = await self._handler(str(msg.get("tool")), msg.get("arguments") or {})
        except Exception as exc:
            ok, text = False, f"Erreur sur Opal Box : {exc}"
        reply.update({"success": True, "output": text} if ok else {"success": False, "error": text})
        try:
            await ws.send(json.dumps(reply, ensure_ascii=False))
        except websockets.WebSocketException:
            pass
