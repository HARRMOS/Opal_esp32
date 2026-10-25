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

from collections.abc import Callable

from aiohttp import WSMsgType, web

from camera import CameraHub

PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Opal Box — caméra</title>
<style>
  body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:10px;background:#0b0d12;color:#cfd6e4;font:15px system-ui,sans-serif}
  #wrap{position:relative;line-height:0}
  img{display:block;max-width:min(100vw,960px);max-height:76vh;width:auto;height:auto;
      min-width:min(90vw,480px);border-radius:12px;background:#000}
  .bar{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;max-width:94vw}
  .chip{padding:5px 12px;border-radius:999px;background:#151a26;border:1px solid #2a3040;font-size:14px}
  .chip.ok{border-color:#2f7a52;color:#9fe0bb}.chip.warn{border-color:#8a5a22;color:#f2c48a}
  nav{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
  a{color:#cfd6e4;text-decoration:none;padding:6px 14px;border:1px solid #2a3040;border-radius:999px}
  a.on{background:#1d2333;border-color:#4a5675}
</style></head>
<body>
<div id="wrap"><img id="v" alt="Caméra d'Opal Box"></div>
<div class="bar"><span id="who" class="chip">…</span><span id="gesture" class="chip">Aucun geste</span><span id="src" class="chip">…</span></div>
<nav><a id="fluide" href="?">Fluide</a><a id="net" href="?hd=1">Net</a><a href="/opal/cam/envoyer">Envoyer ma caméra</a></nav>
<script>
const hd = new URLSearchParams(location.search).has("hd");
// Visage, main et geste sont incrustés dans la vidéo par le VPS ; ici, la barre d'état.
const v = document.getElementById("v");
v.src = "/opal/cam/stream" + (hd ? "?hd=1" : "");
document.getElementById(hd ? "net" : "fluide").classList.add("on");
const GESTES = { pouce_haut: "👍 pouce levé", pouce_bas: "👎 pouce baissé", main_ouverte: "✋ main ouverte", ok: "👌 OK", bras_croises: "❌ bras croisés" };

function draw(s) {
  const who = document.getElementById("who");
  who.textContent = !s.present ? "Personne devant la caméra"
    : s.who ? `${s.who} est là` : s.who === "" ? "Inconnu devant la caméra" : "Quelqu'un est là";
  who.className = "chip" + (s.who ? " ok" : s.present && s.who === "" ? " warn" : "");
  const g = document.getElementById("gesture"), last = s.last_gesture;
  if (last) {
    const ago = Math.round(s.now - last.at);
    g.textContent = `Dernier geste : ${GESTES[last.name] || last.name} · il y a ${ago < 60 ? ago + " s" : Math.round(ago / 60) + " min"}`;
    g.className = "chip" + (ago < 5 ? " ok" : "");
  }
  document.getElementById("src").textContent = (s.source === "navigateur" ? "Caméra iPhone / Mac"
    : s.source ? "ESP32-CAM" : "Caméra hors ligne") + (s.source ? ` · ${s.fps_in} img/s` : "");
}

async function tick() {
  try {
    const res = await fetch("/opal/cam/infos", { cache: "no-store" });
    if (res.ok) draw(await res.json());
  } catch {}
  setTimeout(tick, 250);
}
tick();
</script></body></html>"""

# Page « Envoyer ma caméra » : un iPhone ou un Mac devient la caméra d'Opal Box.
# Même protocole que l'ESP32-CAM (JPEG préfixés 0x01 flux / 0x02 photo).
SOURCE_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Opal Box — envoyer ma caméra</title>
<style>
  body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:12px;background:#0b0d12;color:#cfd6e4;font:15px system-ui,sans-serif}
  #pv{position:relative;line-height:0}
  video{width:min(92vw,640px);border-radius:12px;background:#000;transform:scaleX(-1)}
  #ov{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
  video.back{transform:none}
  .row{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
  button,select{font:inherit;color:#cfd6e4;background:#1d2333;border:1px solid #4a5675;
       border-radius:999px;padding:8px 16px}
  button.stop{background:#3a1d24;border-color:#7a3a48}
  p{opacity:.65;margin:0;text-align:center;max-width:90vw}
</style></head>
<body>
<div id="pv"><video id="v" playsinline muted autoplay></video><canvas id="ov"></canvas></div>
<div class="row">
  <select id="facing"><option value="user">Caméra avant</option>
    <option value="environment">Caméra arrière</option></select>
  <button id="go">Démarrer</button>
</div>
<p id="status">Cet appareil deviendra la caméra d'Opal Box tant que la page reste ouverte.</p>
<script>
const v = document.getElementById("v"), go = document.getElementById("go");
const facing = document.getElementById("facing"), status = document.getElementById("status");
const canvas = document.createElement("canvas"), ctx = canvas.getContext("2d");
let ws = null, stream = null, timer = null, fps = 4, size = "qvga", busy = false, lock = null, running = false;

function say(t) { status.textContent = t; }

function grab(kind, width, quality) {
  if (!v.videoWidth || !ws || ws.readyState !== 1) return Promise.resolve();
  const w = Math.min(width, v.videoWidth), h = Math.round(w * v.videoHeight / v.videoWidth);
  canvas.width = w; canvas.height = h;
  ctx.drawImage(v, 0, 0, w, h);
  return new Promise((done) => canvas.toBlob(async (blob) => {
    if (blob && ws && ws.readyState === 1) {
      const data = new Uint8Array(await blob.arrayBuffer());
      const out = new Uint8Array(data.length + 1);
      out[0] = kind; out.set(data, 1);
      ws.send(out);
    }
    done();
  }, "image/jpeg", quality));
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(async () => {
    if (busy) return;
    busy = true;
    // Réseau en retard : on saute l'image plutôt que d'empiler (direct sans décalage).
    if (ws && ws.bufferedAmount < 96 * 1024)
      await grab(1, size === "hd" ? 960 : size === "vga" ? 640 : 320, 0.85);
    busy = false;
  }, 1000 / Math.max(1, fps));
}

// Voix d'Opal : PCM 16 bits mono envoyé par le VPS, joué à la suite (phrase après phrase).
let audio = null, voiceRate = 24000, playhead = 0, spokeUntil = 0;
function playVoice(buf) {
  if (!audio) return;
  const pcm = new Int16Array(buf), clip = audio.createBuffer(1, pcm.length, voiceRate);
  const data = clip.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 32768;
  const src = audio.createBufferSource();
  src.buffer = clip; src.connect(audio.destination);
  playhead = Math.max(playhead, audio.currentTime + 0.05);
  src.start(playhead);
  playhead += clip.duration + 0.15;
}

function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/opal/cam/source");
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "hello", device: "camera", sensor: "navigateur", soft_jpeg: false }));
    say("Connecté : Opal Box voit par cet appareil.");
    schedule();
  };
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) { playVoice(e.data); return; }
    const m = JSON.parse(e.data);
    if (m.type === "say") { voiceRate = m.rate || 24000; say("🗣 Opal : " + m.text); spokeUntil = Date.now() + 8000; }
    if (m.type === "cam") { fps = m.fps || fps; size = m.size || size; schedule(); }
    if (m.type === "snapshot") grab(2, 1280, 0.9);
  };
  ws.onclose = () => {
    clearInterval(timer);
    if (running) { say("Connexion perdue, nouvel essai…"); setTimeout(connect, 3000); }
  };
}

async function start() {
  // Créé pendant le clic : iPhone n'autorise le son qu'après un geste de l'utilisateur.
  try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); await audio.resume(); } catch {}
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: facing.value, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
  } catch (err) { say("Caméra refusée : " + err.message); return; }
  v.srcObject = stream;
  v.classList.toggle("back", facing.value === "environment");
  await v.play();
  running = true;
  try { lock = await navigator.wakeLock?.request("screen"); } catch {}
  go.textContent = "Arrêter"; go.classList.add("stop"); facing.disabled = true;
  connect();
}

function stop() {
  running = false;
  clearInterval(timer);
  ws?.close(); ws = null;
  stream?.getTracks().forEach((t) => t.stop()); stream = null;
  lock?.release?.(); lock = null;
  go.textContent = "Démarrer"; go.classList.remove("stop"); facing.disabled = false;
  say("Arrêté : l'ESP32-CAM reprend la main.");
}

go.onclick = () => (running ? stop() : start());
// Vision d'Opal par-dessus l'aperçu : ce que le VPS détecte (visage, main, geste).
const ov = document.getElementById("ov"), octx = ov.getContext("2d");
const GESTES = { pouce_haut: "Pouce levé", pouce_bas: "Pouce baissé", main_ouverte: "Main ouverte",
  ok: "👌 OK", bras_croises: "Bras croisés" };
// Bras : épaules, coudes, poignets (gauche/droite de la personne)
const ARM_LINKS = [[0,1],[0,2],[2,4],[1,3],[3,5]];
const LINKS = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],
  [9,13],[13,14],[14,15],[15,16],[13,17],[0,17],[17,18],[18,19],[19,20]];
const mirrored = () => !v.classList.contains("back");
const px = (x) => (mirrored() ? 1 - x : x) * ov.width;  // l'aperçu avant est en miroir

function tag(text, x, y, color) {
  const size = Math.max(13, ov.width / 30);
  octx.font = `600 ${size}px system-ui, sans-serif`;
  const w = octx.measureText(text).width + size * 0.9, h = size * 1.5;
  x = Math.min(Math.max(0, x), ov.width - w);
  const top = y - h - 4 < 0 ? y + 4 : y - h - 4;
  octx.fillStyle = color; octx.beginPath(); octx.roundRect(x, top, w, h, h / 2); octx.fill();
  octx.fillStyle = "#0b0d12"; octx.fillText(text, x + size * 0.45, top + h * 0.7);
}

function corners(x1, y1, x2, y2, color) {
  const arm = Math.max(8, Math.min(x2 - x1, y2 - y1) / 4);
  octx.strokeStyle = color; octx.lineWidth = Math.max(2, ov.width / 220); octx.beginPath();
  for (const [cx, cy, dx, dy] of [[x1, y1, 1, 1], [x2, y1, -1, 1], [x1, y2, 1, -1], [x2, y2, -1, -1]]) {
    octx.moveTo(cx + dx * arm, cy); octx.lineTo(cx, cy); octx.lineTo(cx, cy + dy * arm);
  }
  octx.stroke();
}

function drawVision(s) {
  const r = devicePixelRatio || 1;
  ov.width = v.clientWidth * r; ov.height = v.clientHeight * r;
  octx.clearRect(0, 0, ov.width, ov.height);
  const fresh = (d) => d && s.now - d.at < 0.8, H = ov.height;
  if (fresh(s.face)) {
    const f = s.face, [x, y, w, h] = f.box;
    const color = f.name ? "#4fd18b" : f.name === "" ? "#f2a24a" : "#9aa6bd";
    let text = f.name ? f.name : f.name === "" ? "Inconnu" : "Visage…";
    if (f.name != null && f.score != null) text += `  ${Math.round(f.score * 100)} %`;
    const xa = px(x), xb = px(x + w);
    corners(Math.min(xa, xb), y * H, Math.max(xa, xb), (y + h) * H, color);
    octx.fillStyle = color;
    for (const [qx, qy] of f.points || []) { octx.beginPath(); octx.arc(px(qx), qy * H, Math.max(2, ov.width / 260), 0, 7); octx.fill(); }
    tag(text, Math.min(xa, xb), y * H, color);
  }
  if (fresh(s.hand) && (s.hand.points || []).length === 21) {
    const p = s.hand.points.map(([qx, qy]) => [px(qx), qy * H]);
    octx.strokeStyle = "#5aa9ff"; octx.lineWidth = Math.max(2, ov.width / 220); octx.beginPath();
    for (const [a, b] of LINKS) { octx.moveTo(...p[a]); octx.lineTo(...p[b]); }
    octx.stroke();
    octx.fillStyle = "#fff";
    for (const q of p) { octx.beginPath(); octx.arc(q[0], q[1], Math.max(2.5, ov.width / 200), 0, 7); octx.fill(); }
    const top = Math.min(...p.map((q) => q[1])), left = Math.min(...p.map((q) => q[0]));
    tag(GESTES[s.hand.gesture] || "Main", left, top, "#5aa9ff");
  }
  if (fresh(s.arms) && (s.arms.arms || []).length === 6) {
    const p = s.arms.arms.map(([qx, qy, vis]) => [px(qx), qy * H, vis]);
    const crossed = s.arms.gesture === "bras_croises", color = crossed ? "#f2a24a" : "#b48cff";
    octx.strokeStyle = color; octx.lineWidth = Math.max(3, ov.width / 160); octx.beginPath();
    for (const [a, b] of ARM_LINKS) {
      if (p[a][2] < 0.5 || p[b][2] < 0.5) continue;
      octx.moveTo(p[a][0], p[a][1]); octx.lineTo(p[b][0], p[b][1]);
    }
    octx.stroke();
    if (crossed) tag("Bras croisés", Math.min(p[4][0], p[5][0]), Math.min(p[4][1], p[5][1]), color);
  }
  let state = !s.present ? "Personne" : s.who ? `${s.who} est là` : s.who === "" ? "Inconnu" : "Quelqu'un est là";
  const last = s.last_gesture;
  if (last && s.now - last.at < 5) state += `  ·  ${GESTES[last.name] || last.name} !`;
  if (running && Date.now() > spokeUntil) say(`Opal voit : ${state}`);
}

async function vision() {
  if (running) {
    try {
      const res = await fetch("/opal/cam/infos", { cache: "no-store" });
      if (res.ok) drawVision(await res.json());
    } catch {}
  } else {
    octx.clearRect(0, 0, ov.width, ov.height);
  }
  setTimeout(vision, 150);
}
vision();
</script></body></html>"""


class BrowserSource:
    """Connexion WebSocket d'aiohttp présentée comme celle d'une ESP32-CAM."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self._ws = ws

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, str):
            await self._ws.send_str(message)
        else:
            await self._ws.send_bytes(message)


def make_app(
    cams: CameraHub,
    password: str,
    iris_token: str = "",
    on_sources_changed: Callable[[], None] | None = None,
) -> web.Application:
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

    async def infos(_: web.Request) -> web.Response:
        return web.json_response(cams.infos(), headers={"Cache-Control": "no-store"})

    async def source_page(_: web.Request) -> web.Response:
        return web.Response(text=SOURCE_PAGE, content_type="text/html")

    async def source(request: web.Request) -> web.WebSocketResponse:
        """Un navigateur (iPhone, Mac) envoie sa caméra : il devient la source active."""
        ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=20)
        await ws.prepare(request)
        link = BrowserSource(ws)
        try:
            first = await ws.receive_json(timeout=15)
            if first.get("type") != "hello":
                return ws
            await cams.attach(link, first)
            if on_sources_changed:
                on_sources_changed()
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await cams.on_binary(msg.data, link)
        except (TimeoutError, ValueError):
            pass
        finally:
            await cams.detach(link)
            if on_sources_changed:
                on_sources_changed()
        return ws

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
    app.router.add_get("/opal/cam/infos", infos)
    app.router.add_get("/opal/cam/envoyer", source_page)
    app.router.add_get("/opal/cam/source", source)
    app.router.add_get("/opal/cam/photos/{photo_id}.jpg", saved_photo)
    return app
