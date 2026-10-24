"""Opal Box vue par Opal : un nœud, comme le PC et le Mac, qui apporte ses outils.

Protocole des nœuds d'Opal (WebSocket /node, « Authorization: Bearer <node_token> ») :
  nœud → Opal  {"type": "hello", "name", "platform", "tools": [schémas OpenAI], "environment"}
  Opal → nœud  {"type": "call", "id", "tool", "arguments", "timeout"}
  nœud → Opal  {"type": "result", "id", "success", "output", "error"}

Le nœud n'est connecté que tant qu'au moins un boîtier l'est : Opal sait donc
toujours si Opal Box est allumée.
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
            "cligner pour un clin d'œil. Tous les champs sont facultatifs et combinables. "
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
                "cligner": {"type": "boolean"},
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

ENVIRONMENT = (
    "## OPAL BOX\n"
    "« opal-box » n'est pas un ordinateur : c'est ton corps physique, un boîtier avec deux "
    "yeux animés posé chez l'utilisateur. Il n'a qu'un outil, opal_box_yeux. Ne lui envoie "
    "jamais d'outil d'ordinateur (apps, fichiers, navigateur). Utilise tes yeux avec "
    "naturel pour accompagner tes réponses, sans le mentionner à chaque fois."
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
    if (pupil := args.get("pupille")) in PUPILS:
        cmd["pupil"] = PUPILS[pupil]
        done.append(f"pupilles {pupil}s")
    hold = float(args.get("duree_s", 8) or 0)
    cmd["hold_ms"] = int(max(0.0, min(hold, 600.0)) * 1000)
    return cmd, ", ".join(done)


class OpalNode:
    """Liaison nœud vers Opal, active seulement quand un boîtier est en ligne."""

    def __init__(
        self,
        url: str,
        token: str,
        send_to_devices: Callable[[dict[str, Any]], Awaitable[int]],
    ) -> None:
        self._url = url
        self._token = token
        self._send_to_devices = send_to_devices
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

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
                                "tools": [EYES_TOOL],
                                "environment": ENVIRONMENT,
                            },
                            ensure_ascii=False,
                        )
                    )
                    log("node_connected", url=self._url)
                    async for message in ws:
                        msg = json.loads(message)
                        if msg.get("type") == "call":
                            await ws.send(json.dumps(await self._call(msg), ensure_ascii=False))
            except asyncio.CancelledError:
                log("node_disconnected")
                raise
            except (OSError, websockets.WebSocketException) as exc:
                log("node_link_lost", error=str(exc))
            await asyncio.sleep(3)

    async def _call(self, msg: dict[str, Any]) -> dict[str, Any]:
        reply: dict[str, Any] = {"type": "result", "id": msg.get("id")}
        if msg.get("tool") != EYES_TOOL["function"]["name"]:
            return {**reply, "success": False, "error": f"Outil inconnu : {msg.get('tool')}"}
        cmd, summary = eyes_command(msg.get("arguments") or {})
        if not summary:
            return {**reply, "success": False, "error": "Aucune expression demandée."}
        sent = await self._send_to_devices(cmd)
        log("eyes", command=cmd, devices=sent)
        if not sent:
            return {**reply, "success": False, "error": "Opal Box n'est pas connectée."}
        return {**reply, "success": True, "output": f"Yeux : {summary}."}
