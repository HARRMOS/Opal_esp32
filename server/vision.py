"""Description d'une photo par Claude Code (abonnement Claude, comme le cerveau d'Opal).

Même appel que le cerveau d'Opal : `claude -p` sans outils ni configuration locale,
image envoyée en bloc base64 sur l'entrée stream-json. Authentification par la
variable CLAUDE_CODE_OAUTH_TOKEN (jeton de `claude setup-token`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil

SYSTEM = (
    "Tu es les yeux d'Opal, un assistant. On te montre une photo prise par la caméra "
    "d'Opal Box, posée chez l'utilisateur. Décris en français, de façon concise et "
    "factuelle, ce qui répond à la question. Si tu n'es pas sûr, dis-le."
)


async def describe(jpeg: bytes, question: str, timeout: float = 90) -> str:
    cli = shutil.which("claude")
    if not cli:
        raise RuntimeError("Claude Code n'est pas installé dans le pont.")
    model = os.environ.get("OPAL_VISION_MODEL", "haiku")
    args = [
        "-p", "--model", model, "--tools", "", "--setting-sources", "",
        "--no-session-persistence", "--strict-mcp-config", "--system-prompt", SYSTEM,
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
    ]
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(jpeg).decode(),
            },
        },
        {"type": "text", "text": question},
    ]
    line = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
    proc = await asyncio.create_subprocess_exec(
        cli, *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "DISABLE_AUTOUPDATER": "1"},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate((line + "\n").encode()), timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError("Claude Code n'a pas répondu à temps.") from None

    for raw in reversed(out.decode("utf-8", errors="replace").splitlines()):
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "result":
            if event.get("is_error"):
                raise RuntimeError(f"Claude Code : {str(event.get('result'))[:200]}")
            return str(event.get("result") or "").strip()
    message = (err.decode("utf-8", errors="replace") or out.decode("utf-8", errors="replace"))
    raise RuntimeError(f"Réponse illisible de Claude Code : {message[-200:]}")
