"""Direct vidéo de la caméra d'Opal Box : https://<domaine>/opal/cam

Protégé par mot de passe (authentification HTTP Basic, OPAL_VIEWER_PASSWORD) :
le navigateur le demande une fois, le flux MJPEG s'affiche dans une simple page.
"""

from __future__ import annotations

import base64
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
  img{max-width:100vw;max-height:85vh;border-radius:12px;background:#000}
  p{opacity:.6}
</style></head>
<body><img src="/opal/cam/stream" alt="Caméra d'Opal Box">
<p>Opal Box · direct</p></body></html>"""


def make_app(cams: CameraHub, password: str) -> web.Application:
    @web.middleware
    async def auth(request: web.Request, handler):  # type: ignore[no-untyped-def]
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

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-store",
            }
        )
        await response.prepare(request)
        cams.viewers += 1
        await cams.apply_mode()
        try:
            while True:
                async with cams.new_frame:
                    await cams.new_frame.wait()
                jpeg = cams.frame
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
            await cams.apply_mode()
        return response

    app = web.Application(middlewares=[auth])
    app.router.add_get("/opal/cam", page)
    app.router.add_get("/opal/cam/", page)
    app.router.add_get("/opal/cam/stream", stream)
    app.router.add_get("/opal/cam/photo.jpg", photo)
    return app
