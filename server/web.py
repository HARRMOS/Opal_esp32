"""Direct vidéo de la caméra d'Opal Box : https://<domaine>/opal/cam

Protégé par mot de passe (authentification HTTP Basic, OPAL_VIEWER_PASSWORD) :
le navigateur le demande une fois, le flux MJPEG s'affiche dans une simple page.
Un navigateur déjà appairé à Opal entre directement : son cookie de session
(iris_session = HMAC du jeton d'Opal) est reconnu, comme le fait Opal lui-même.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac

from aiohttp import web

from camera import CameraHub

PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Opal Box — caméra</title>
<style>
  body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;background:#0b0d12;color:#cfd6e4;font:15px system-ui,sans-serif}
  img{width:min(100vw,960px);max-height:82vh;object-fit:contain;border-radius:12px;background:#000}
  nav{display:flex;gap:8px;margin:14px 0 4px}
  a{color:#cfd6e4;text-decoration:none;padding:6px 14px;border:1px solid #2a3040;border-radius:999px}
  a.on{background:#1d2333;border-color:#4a5675}
  p{opacity:.55;margin:6px}
</style></head>
<body><img id="v" alt="Caméra d'Opal Box">
<nav><a id="fluide" href="?">Fluide</a><a id="net" href="?hd=1">Net</a></nav>
<p id="info"></p>
<script>
  const hd = new URLSearchParams(location.search).has("hd");
  document.getElementById("v").src = "/opal/cam/stream" + (hd ? "?hd=1" : "");
  document.getElementById(hd ? "net" : "fluide").classList.add("on");
  document.getElementById("info").textContent = hd
    ? "Opal Box · direct net (640x480, environ 1 image/s)"
    : "Opal Box · direct fluide (environ 3 images/s)";
</script></body></html>"""


def make_app(cams: CameraHub, password: str, iris_token: str = "") -> web.Application:
    session = (
        hmac.new(iris_token.encode(), b"iris-session-v1", hashlib.sha256).hexdigest()
        if iris_token
        else ""
    )

    @web.middleware
    async def auth(request: web.Request, handler):  # type: ignore[no-untyped-def]
        cookie = request.cookies.get("iris_session", "")
        if session and cookie and hmac.compare_digest(cookie, session):
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                given = ""
            if password and hmac.compare_digest(given.encode(), password.encode()):
                return await handler(request)
        return web.Response(
            status=401,
            text="Mot de passe requis.",
            headers={"WWW-Authenticate": 'Basic realm="Opal Box", charset="UTF-8"'},
        )

    async def page(_: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")

    async def photo(_: web.Request) -> web.Response:
        if cams.frame is None:
            return web.Response(status=503, text="Caméra hors ligne.")
        return web.Response(body=cams.frame, content_type="image/jpeg")

    async def saved_photo(request: web.Request) -> web.Response:
        jpeg = cams.photos.get(request.match_info["photo_id"])
        if jpeg is None:
            return web.Response(status=404, text="Photo expirée ou introuvable.")
        return web.Response(body=jpeg, content_type="image/jpeg")

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-store",
            }
        )
        await response.prepare(request)
        hd = "hd" in request.query
        cams.viewers += 1
        cams.viewers_hd += hd
        await cams.apply_mode()
        try:
            while True:
                async with cams.new_frame:
                    await cams.new_frame.wait()
                jpeg = await asyncio.to_thread(cams.enhanced)
                if jpeg is None:
                    continue
                await response.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode()
                    + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            cams.viewers -= 1
            cams.viewers_hd -= hd
            await cams.apply_mode()
        return response

    app = web.Application(middlewares=[auth])
    app.router.add_get("/opal/cam", page)
    app.router.add_get("/opal/cam/", page)
    app.router.add_get("/opal/cam/stream", stream)
    app.router.add_get("/opal/cam/photo.jpg", photo)
    app.router.add_get("/opal/cam/photos/{photo_id}.jpg", saved_photo)
    return app
