#!/usr/bin/env python3
"""Agent de verrouillage d'Opal Box, sur le PC (Windows) ou le Mac.

Opal (le pont, à partir de la caméra) décide du moment ; cet agent applique le verrouillage :

  - il affiche l'écran de garde Opal en plein écran, par-dessus tout, sur tous les moniteurs ;
  - toute tentative d'en sortir (changement de fenêtre, touche système, agent tué) bascule
    aussitôt sur le VERROUILLAGE NATIF du système (mot de passe exigé) : c'est le vrai cadenas,
    l'écran Opal n'en est que la couche visible ;
  - déverrouillage : quand la caméra d'Opal reconnaît le propriétaire (le pont envoie « unlock »),
    ou avec le code secret tapé ici. Le mot de passe du système reste toujours une issue de
    secours, pour ne jamais rester enfermé dehors de sa propre machine.

Aucune dépendance en dehors de la bibliothèque standard (urllib + tkinter).

Installation / configuration (une seule fois, c'est toi qui tapes ton code, l'agent ne
l'envoie jamais nulle part) :

    python opal_guard.py --setup

Puis lancer l'agent (au démarrage de session, voir le README) :

    python opal_guard.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import platform
import queue
import secrets
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_DIR = Path.home() / ".opal_guard"
CONFIG_PATH = CONFIG_DIR / "config.json"
# Présent tant que c'est verrouillé : si l'agent est tué puis relancé (tâche planifiée,
# LaunchAgent), il revient directement verrouillé au lieu d'ouvrir l'accès.
LOCK_FLAG = CONFIG_DIR / "locked"
PBKDF2_ROUNDS = 200_000
MAX_CODE_ATTEMPTS = 5  # au-delà : verrouillage natif + remise à zéro
POLL_TIMEOUT_S = 30
IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


# ── Configuration ────────────────────────────────────────────────────────────
def load_config() -> dict | None:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def hash_code(code: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode(), salt, PBKDF2_ROUNDS).hex()


def setup() -> None:
    print("Configuration de l'agent de verrouillage Opal.\n")
    server = input("Adresse du pont [https://iris.inoma.pro] : ").strip() or "https://iris.inoma.pro"
    token = input("Jeton du gardien (OPAL_GUARD_TOKEN) : ").strip()
    machine = input(f"Nom de cette machine [{platform.node()}] : ").strip() or platform.node()
    while True:
        import getpass  # saisie masquée : le code ne s'affiche pas

        code = getpass.getpass("Choisis ton code secret (4 chiffres ou plus) : ").strip()
        if len(code) >= 4 and getpass.getpass("Confirme le code : ").strip() == code:
            break
        print("Les codes ne correspondent pas, ou trop courts. Recommence.")
    salt = secrets.token_bytes(16)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "server": server.rstrip("/"),
                "token": token,
                "machine": machine[:40],
                "salt": salt.hex(),
                "code_hash": hash_code(code, salt),
                "code_len": len(code),
            }
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    print(f"\nEnregistré dans {CONFIG_PATH}. Ton code n'est stocké que sous forme chiffrée.")
    print("Lance maintenant : python opal_guard.py")


# ── Verrouillage natif du système (le vrai cadenas) ──────────────────────────
def native_lock() -> None:
    """Verrouille la session au niveau du système : mot de passe exigé pour revenir."""
    try:
        if IS_WINDOWS:
            import ctypes

            ctypes.windll.user32.LockWorkStation()
        elif IS_MAC:
            # Écran de verrouillage macOS (mot de passe exigé), comme Ctrl+Cmd+Q. L'ancien
            # outil CGSession n'existe plus depuis Big Sur : on passe par login.framework.
            import ctypes

            login = "/System/Library/PrivateFrameworks/login.framework/Versions/Current/login"
            if os.path.exists(login):
                ctypes.CDLL(login).SACLockScreenImmediate()
            else:
                os.system("pmset displaysleepnow")  # veille écran : mot de passe au réveil
        else:
            os.system("loginctl lock-session || xdg-screensaver lock")
    except Exception as exc:  # noqa: BLE001
        print(f"verrou natif indisponible : {exc}", file=sys.stderr)


# ── Blocage clavier bas niveau (Windows) ─────────────────────────────────────
# Un programme ne peut pas bloquer Ctrl+Alt+Suppr (c'est voulu par Windows, et c'est notre
# filet vers le verrou natif). Mais un hook WH_KEYBOARD_LL bloque tout le reste tant que
# c'est verrouillé : touche Windows, Alt+Tab, Alt+Échap, Ctrl+Échap, Alt+F4, Ctrl+Maj+Échap.
class KeyBlocker:
    def __init__(self, is_locked) -> None:
        self._is_locked = is_locked
        self._hook = None
        self._proc = None
        if not IS_WINDOWS:
            return
        import ctypes
        from ctypes import wintypes

        self._ctypes, self._wintypes = ctypes, wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._WH = 13  # WH_KEYBOARD_LL
        self._DOWN = (0x0100, 0x0104)  # WM_KEYDOWN, WM_SYSKEYDOWN

        class KBD(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("extra", ctypes.POINTER(wintypes.ULONG))]

        self._KBD = KBD
        self._PROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        self._user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self._user32.SetWindowsHookExW.argtypes = [ctypes.c_int, self._PROC, wintypes.HINSTANCE,
                                                   wintypes.DWORD]
        self._user32.CallNextHookEx.restype = wintypes.LPARAM
        self._user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                                wintypes.LPARAM]

    def _down(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _callback(self, nCode, wParam, lParam):  # noqa: ANN001
        if nCode == 0 and self._is_locked() and wParam in self._DOWN:
            vk = self._ctypes.cast(lParam, self._ctypes.POINTER(self._KBD)).contents.vkCode
            alt, ctrl, shift = self._down(0x12), self._down(0x11), self._down(0x10)
            block = (
                vk in (0x5B, 0x5C)                       # touche Windows gauche / droite
                or (vk == 0x09 and alt)                  # Alt+Tab
                or (vk == 0x1B and (alt or ctrl))        # Alt+Échap, Ctrl+Échap (menu Démarrer)
                or (vk == 0x73 and alt)                  # Alt+F4
                or (vk == 0x1B and ctrl and shift)       # Ctrl+Maj+Échap (Gestionnaire)
            )
            if block:
                return 1  # touche avalée
        return self._user32.CallNextHookEx(None, nCode, wParam, lParam)

    def install(self) -> None:
        if not IS_WINDOWS or self._hook:
            return
        self._proc = self._PROC(self._callback)  # garder la référence (sinon ramassé)
        self._hook = self._user32.SetWindowsHookExW(self._WH, self._proc, None, 0)

    def remove(self) -> None:
        if IS_WINDOWS and self._hook:
            self._user32.UnhookWindowsHookEx(self._hook)
            self._hook = None


# ── Écran de garde Opal (Tkinter, sur le thread principal) ───────────────────
class LockScreen:
    """Fenêtre plein écran, par-dessus tout, avec l'œil du gardien et le clavier de code."""

    def __init__(self, config: dict, on_unlock, report) -> None:
        self._config = config
        self._on_unlock = on_unlock  # appelé quand le bon code est saisi
        self._report = report
        self._salt = bytes.fromhex(config["salt"])
        self._locked = False
        self._entry = ""
        self._attempts = 0
        self._tamper_armed = False
        self._code_len = int(config.get("code_len", 0))  # 0 = longueur inconnue → validation par OK
        self._blocker = KeyBlocker(lambda: self._locked)

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Opal Gardien")
        self.root.configure(bg="#07060a")
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)  # la croix ne ferme pas
        if IS_MAC:  # Cmd+Q ne quitte pas l'écran de garde
            self.root.createcommand("tk::mac::Quit", lambda: None)

        self._build()
        # Les chiffres tapés au clavier physique entrent le code ; Entrée valide, Retour arrière
        # efface. Toute autre touche est avalée tant que c'est verrouillé.
        self.root.bind_all("<Key>", self._on_physical_key)
        self.root.bind("<FocusOut>", self._on_focus_out)
        self._pulse()

    # Construction de l'interface (œil rouge à gauche, message + clavier à droite).
    def _build(self) -> None:
        wrap = tk.Frame(self.root, bg="#07060a")
        wrap.pack(expand=True, fill="both")

        left = tk.Frame(wrap, bg="#0a0405")
        left.place(relx=0, rely=0, relwidth=0.5, relheight=1)
        self.canvas = tk.Canvas(left, bg="#0a0405", highlightthickness=0, width=400, height=400)
        self.canvas.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(left, text="OPAL · GARDIEN", fg="#e8231c", bg="#0a0405",
                 font=("Consolas", 13)).place(relx=0.08, rely=0.06)

        right = tk.Frame(wrap, bg="#0b090d")
        right.place(relx=0.5, rely=0, relwidth=0.5, relheight=1)
        pad = tk.Frame(right, bg="#0b090d")
        pad.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(pad, text="PC verrouillé par Opal", fg="#efe9e4", bg="#0b090d",
                 font=("Segoe UI", 26, "bold")).pack(anchor="w", pady=(0, 8))
        self.msg = tk.Label(
            pad, bg="#0b090d", fg="#9b8f8c", justify="left", wraplength=420,
            text="Un visage inconnu est resté devant la caméra d'Opal.\n"
                 "Déverrouillage quand Opal te reconnaît, ou avec ton code secret.",
            font=("Segoe UI", 13))
        self.msg.pack(anchor="w", pady=(0, 14))
        self.dots = tk.Label(pad, text="", fg="#e8231c", bg="#0b090d", font=("Consolas", 30))
        self.dots.pack(anchor="w")
        self.status = tk.Label(pad, text="Entre ton code", fg="#9b8f8c", bg="#0b090d",
                               font=("Consolas", 13))
        self.status.pack(anchor="w", pady=(4, 16))

        keys = tk.Frame(pad, bg="#0b090d")
        keys.pack(anchor="w")
        # Dernière ligne : ⌫ (effacer), 0, OK (valider) — OK fonctionne quelle que soit la
        # longueur du code, donc un code de 6 chiffres passe aussi bien qu'un de 4.
        for i, label in enumerate(["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "OK"]):
            ok = label == "OK"
            btn = tk.Button(
                keys, text=label, width=5, height=2, font=("Segoe UI", 18),
                bg="#1d3a2a" if ok else "#151114", fg="#8ff0bd" if ok else "#efe9e4",
                activebackground="#22181b", activeforeground="#fff",
                relief="flat", bd=0, highlightthickness=0,
                command=lambda k=label: self._key(k))
            btn.grid(row=i // 3, column=i % 3, padx=5, pady=5, sticky="nsew")

    # ── Verrouiller / déverrouiller (appelé depuis le thread réseau via la file) ──
    def lock(self, reason: str = "") -> None:
        self._locked = True
        self._entry = ""
        self._attempts = 0
        self._tamper_armed = False
        if reason:
            self.msg.config(text=reason + "\nDéverrouillage quand Opal te reconnaît, ou avec ton code.")
        self._refresh()
        self.root.deiconify()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        if IS_MAC:  # au-dessus même du Dock et des autres apps
            try:
                self.root.call("::tk::unsupported::MacWindowStyle", "style", self.root._w,
                               "plain", "none")
            except tk.TclError:
                pass
        try:
            LOCK_FLAG.write_text(reason or "verrouillé", encoding="utf-8")
        except OSError:
            pass
        self.root.lift()
        self.root.focus_force()
        try:
            self.root.grab_set_global()
        except tk.TclError:
            pass
        self._blocker.install()  # bloque Windows/Alt+Tab/etc. tant que c'est verrouillé
        # Laisse une seconde avant d'armer la détection de fuite (le temps de prendre le focus).
        self.root.after(1200, lambda: setattr(self, "_tamper_armed", True))
        self._report(True)

    def unlock(self, by: str) -> None:
        if not self._locked:
            return
        self._locked = False
        self._tamper_armed = False
        self._blocker.remove()
        try:
            self.root.grab_release()
        except tk.TclError:
            pass
        self.root.attributes("-topmost", False)
        self.root.withdraw()
        LOCK_FLAG.unlink(missing_ok=True)
        self._report(False)
        print(f"déverrouillé ({by})")

    # ── Saisie du code ──
    def _key(self, k: str) -> None:
        if not self._locked:
            return
        if k == "⌫":
            self._entry = self._entry[:-1]
        elif k == "OK":
            if self._entry:
                self._check()
            return
        elif k.isdigit() and len(self._entry) < 16:
            self._entry += k
        self._refresh()
        # Validation automatique seulement si on connaît la longueur du code (confort).
        if self._code_len and len(self._entry) == self._code_len:
            self._check()

    def _on_physical_key(self, event):  # noqa: ANN001
        # Clavier physique : chiffres → saisie, Entrée → valider, Retour arrière → effacer.
        if not self._locked:
            return None
        if event.keysym in ("Return", "KP_Enter"):
            self._key("OK")
        elif event.keysym in ("BackSpace", "Delete"):
            self._key("⌫")
        elif len(event.char) == 1 and event.char.isdigit():
            self._key(event.char)
        return "break"  # rien d'autre ne passe

    def _check(self) -> bool:
        good = hmac.compare_digest(hash_code(self._entry, self._salt), self._config["code_hash"])
        if good:
            self.status.config(text="Opal te laisse entrer", fg="#4fd18b")
            self.root.after(500, lambda: (self.unlock("code"), self._on_unlock()))
            return True
        self._attempts += 1
        self._entry = ""
        if self._attempts >= MAX_CODE_ATTEMPTS:
            self.status.config(text="Trop d'essais : verrouillage du système", fg="#e8231c")
            self._attempts = 0
            native_lock()
        else:
            self.status.config(text=f"Code incorrect ({MAX_CODE_ATTEMPTS - self._attempts} essais)",
                               fg="#e8231c")
        self._refresh(keep_status=True)
        return False

    def _refresh(self, keep_status: bool = False) -> None:
        n = len(self._entry)
        # Un point par chiffre ; si on connaît la longueur, des cercles vides pour les manquants.
        pad = max(0, self._code_len - n) if self._code_len else 0
        self.dots.config(text="●" * n + "○" * pad)
        if not keep_status:
            self.status.config(text="Entre ton code puis OK", fg="#9b8f8c")

    # ── Anti-contournement ──
    def _on_focus_out(self, _event) -> None:  # noqa: ANN001
        # On a perdu le focus alors que c'est verrouillé : quelqu'un a forcé le passage
        # (touche Windows, Alt+Tab, Ctrl+Alt+Suppr…). On engage le vrai verrou du système.
        if self._locked and self._tamper_armed:
            print("tentative de sortie : bascule sur le verrou du système")
            native_lock()
            self.root.after(400, self._reassert)

    def _reassert(self) -> None:
        if not self._locked:
            return
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        try:
            self.root.grab_set_global()
        except tk.TclError:
            pass

    def _pulse(self) -> None:
        # Œil-objectif rouge qui respire (dessiné au Canvas).
        self.canvas.delete("all")
        cx, cy = 200, 200
        r = 120 + int(10 * math.sin(time.time() * 2.4))
        for i, (rad, col) in enumerate([(r + 16, "#2a0506"), (r, "#7c0f0c"), (int(r * 0.74), "#e82420"),
                                        (int(r * 0.42), "#ffbe2e"), (int(r * 0.18), "#fff6d8")]):
            self.canvas.create_oval(cx - rad, cy - rad, cx + rad, cy + rad, fill=col, outline="")
        self.canvas.create_oval(cx - r - 6, cy - r - 6, cx + r + 6, cy + r + 6, outline="#5a5f66", width=5)
        self.root.after(60, self._pulse)

    # ── Boucle : applique les commandes venues du thread réseau ──
    def pump(self, commands: queue.Queue) -> None:
        try:
            while True:
                cmd = commands.get_nowait()
                if cmd["cmd"] == "lock":
                    self.lock(cmd.get("reason", ""))
                elif cmd["cmd"] == "unlock":
                    self.unlock("Opal t'a reconnu")
                # Si verrouillé, garde la main (utile si une autre fenêtre a volé le focus).
                if self._locked and self._tamper_armed:
                    self._reassert()
        except queue.Empty:
            pass
        self.root.after(250, lambda: self.pump(commands))


# ── Thread réseau : attente longue des commandes du pont ─────────────────────
def network_loop(config: dict, commands: queue.Queue, locked_flag) -> None:
    base, token, machine = config["server"], config["token"], config["machine"]
    ctx = ssl.create_default_context()
    headers = {"Authorization": f"Bearer {token}"}

    def post_state(locked: bool) -> None:
        data = json.dumps({"machine": machine, "locked": locked}).encode()
        try:
            urllib.request.urlopen(
                urllib.request.Request(base + "/opal/cam/guard/state", data=data,
                                       headers={**headers, "Content-Type": "application/json"}),
                timeout=10, context=ctx)
        except Exception:  # noqa: BLE001
            pass

    locked_flag["post"] = post_state
    url = f"{base}/opal/cam/guard/poll?machine={urllib.parse.quote(machine)}"
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=POLL_TIMEOUT_S + 10, context=ctx) as resp:
                cmd = json.loads(resp.read())
            if cmd.get("cmd") in ("lock", "unlock"):
                commands.put(cmd)
        except urllib.error.HTTPError as exc:
            print(f"pont : {exc.code} {exc.reason}", file=sys.stderr)
            time.sleep(10)
        except Exception:  # noqa: BLE001  (réseau coupé : on réessaie)
            time.sleep(5)


def tk_works() -> bool:
    """L'écran de garde peut-il s'ouvrir ? Testé dans un processus à part, car sur certains
    macOS la bibliothèque Tk arrête le programme net au lieu de lever une erreur."""
    try:
        probe = subprocess.run([sys.executable, "-c", "import tkinter; tkinter.Tk().destroy()"],
                               capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if probe.returncode != 0:
        print("Tk indisponible :", probe.stderr.decode(errors="replace").strip()[-200:],
              file=sys.stderr)
    return probe.returncode == 0


# ── Écran de garde « sphère » : page web dans une fenêtre Edge plein écran (Windows) ──
LOCK_PAGE = Path(__file__).resolve().parent.parent / "garde" / "lock.html"
UNLOCK_ANIMATION_S = 2.3  # durée de l'animation « Bon retour » avant de fermer l'écran
WINDOWED = False  # tests uniquement : fenêtre normale au lieu du plein écran par-dessus tout
COVER_HTML = ("<!doctype html><html><body style='margin:0;height:100vh;display:grid;place-items:center;"
              "background:#07040a;color:#75606a;font:13px Consolas,monospace;letter-spacing:.35em'>"
              "VERROUILLÉ PAR OPAL</body></html>")


def web_page() -> str | None:
    """La page de garde/lock.html, enveloppée pour l'agent (qui se signale à la page)."""
    try:
        body = LOCK_PAGE.read_text(encoding="utf-8")
    except OSError:
        return None
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<script>window.OPAL_AGENT = true;</script></head><body>" + body + "</body></html>")


def session_locked() -> bool:
    """La session Windows est-elle verrouillée (écran de connexion affiché) ?"""
    import ctypes

    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    try:
        return not user32.SwitchDesktop(ctypes.c_void_p(desk))
    finally:
        user32.CloseDesktop(ctypes.c_void_p(desk))


class PageApi:
    """Seule fonction offerte à la page : vérifier le code (rien d'autre n'est exposé)."""

    def __init__(self, guard: "WebGuard") -> None:
        self._guard = guard

    def check_code(self, code: str) -> dict:
        return self._guard.check_code(code)


class WebGuard:
    """Écran de garde dans une fenêtre web plein écran, par-dessus tout, sur chaque moniteur."""

    def __init__(self, config: dict, html: str) -> None:
        import webview

        self.config, self.html = config, html
        self.salt = bytes.fromhex(config["salt"])
        self.locked = self.armed = False
        self.attempts = 0
        self.reason, self.since = "", 0.0
        self.post = lambda _locked: None
        self.blocker = KeyBlocker(lambda: self.locked)
        screens = list(webview.screens)
        primary = next((s for s in screens if s.x == 0 and s.y == 0), screens[0] if screens else None)
        full = {} if WINDOWED else {"fullscreen": True, "on_top": True, "frameless": True}
        self.window = webview.create_window(
            "Opal Gardien", html=html, js_api=PageApi(self), hidden=True, focus=True,
            background_color="#07040a", width=1280, height=720,
            **({"screen": primary} if primary and not WINDOWED else {}), **full)
        self.window.events.loaded += self._on_loaded
        self.window.events.closing += self._on_closing
        # Les autres moniteurs reçoivent un voile sombre : aucun ne doit rester utilisable.
        self.covers = [] if WINDOWED else [
            webview.create_window("Opal Gardien (écran)", html=COVER_HTML, screen=s, hidden=True,
                                  focus=False, background_color="#07040a", **full)
            for s in screens if s is not primary]
        for cover in self.covers:
            cover.events.closing += self._on_closing

    def _on_closing(self) -> bool:
        return not self.locked  # False : fermeture refusée tant que c'est verrouillé

    def _on_loaded(self) -> None:
        if not self.locked:
            return
        info = {"online": True, "machines": "PC · opal-box", "reason": self.reason,
                "since": int(self.since * 1000), "codeLength": int(self.config.get("code_len", 0))}
        self.window.evaluate_js(f"window.opal && window.opal.update({json.dumps(info)})")

    def lock(self, reason: str = "") -> None:
        if self.locked:
            return
        self.locked, self.armed, self.attempts = True, False, 0
        self.reason, self.since = reason or "Verrouillé par Opal", time.time()
        try:
            LOCK_FLAG.write_text(self.reason, encoding="utf-8")
        except OSError:
            pass
        self.window.load_html(self.html)  # écran remis à neuf (déclenche « loaded »)
        for w in (self.window, *self.covers):
            w.show()
        # Une seconde et demie pour prendre la main avant de surveiller les fuites.
        threading.Timer(1.5, lambda: setattr(self, "armed", self.locked)).start()
        self.post(True)
        print("verrouillé :", self.reason)

    def unlock(self, by: str) -> None:
        if not self.locked:
            return
        if by != "code":  # la page n'a pas encore joué son animation de retour
            try:
                self.window.evaluate_js("window.opal && window.opal.unlocked()")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(UNLOCK_ANIMATION_S)
        self.locked = self.armed = False
        LOCK_FLAG.unlink(missing_ok=True)
        for w in (self.window, *self.covers):
            w.hide()
        self.post(False)
        print("déverrouillé :", by)

    def check_code(self, code: str) -> dict:
        if not self.locked:
            return {"ok": False}
        if hmac.compare_digest(hash_code(str(code)[:16], self.salt), self.config["code_hash"]):
            self.attempts = 0
            threading.Timer(UNLOCK_ANIMATION_S, self.unlock, args=("code",)).start()
            return {"ok": True}
        self.attempts += 1
        if self.attempts >= MAX_CODE_ATTEMPTS:
            self.attempts = 0
            threading.Thread(target=self.system_lock, daemon=True).start()
            return {"ok": False, "message": "Trop d'essais : verrouillage de Windows"}
        return {"ok": False, "remaining": MAX_CODE_ATTEMPTS - self.attempts}

    def system_lock(self) -> None:
        """Verrou de Windows. Si la session est rouverte avec le mot de passe Windows (preuve
        que c'est le propriétaire), l'écran Opal se lève aussi."""
        self.armed = False
        time.sleep(0.6)  # laisse la page afficher son message
        native_lock()
        time.sleep(2)
        while self.locked and session_locked():
            time.sleep(0.5)
        if self.locked:
            self.unlock("mot de passe Windows")

    def watch(self) -> None:
        """Une fenêtre d'un autre programme passe devant (Ctrl+Alt+Suppr puis Gestionnaire des
        tâches…) : verrou de Windows."""
        import ctypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p

        def foreign() -> bool:
            fg = user32.GetForegroundWindow()
            if not fg:  # écran sécurisé (Ctrl+Alt+Suppr) ou session verrouillée : rien à voir
                return False
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), ctypes.byref(pid))
            return pid.value != os.getpid()

        while True:
            time.sleep(0.3)
            if self.locked and self.armed and foreign():
                time.sleep(0.5)  # simple clignement de focus (notification…) ?
                if self.locked and self.armed and foreign():
                    print("tentative de sortie : verrou de Windows")
                    self.system_lock()

    def hook_loop(self) -> None:
        """Le blocage des touches système a besoin d'un fil avec sa propre boucle de messages."""
        import ctypes
        from ctypes import wintypes

        self.blocker.install()
        msg = wintypes.MSG()
        user32 = ctypes.windll.user32
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def run_commands(self, commands: queue.Queue) -> None:
        if LOCK_FLAG.exists():
            self.lock("Verrouillage repris après un redémarrage de l'agent")
        while True:
            cmd = commands.get()
            if cmd["cmd"] == "lock":
                self.lock(str(cmd.get("reason", "")))
            elif cmd["cmd"] == "unlock":
                self.unlock("Opal t'a reconnu")


def run_web(config: dict, html: str) -> None:
    import atexit
    import signal

    import webview

    guard = WebGuard(config, html)
    commands: queue.Queue = queue.Queue()
    link: dict = {}
    guard.post = lambda locked: link.get("post", lambda _l: None)(locked)

    def on_exit(*_args) -> None:  # noqa: ANN002
        if guard.locked:
            native_lock()

    atexit.register(on_exit)
    signal.signal(signal.SIGTERM, lambda *a: (on_exit(), os._exit(0)))

    def start() -> None:
        threading.Thread(target=network_loop, args=(config, commands, link), daemon=True).start()
        threading.Thread(target=guard.hook_loop, daemon=True).start()
        threading.Thread(target=guard.watch, daemon=True).start()
        print(f"Agent Opal Gardien prêt ({config['machine']}), écran sphère.")
        guard.run_commands(commands)

    webview.start(start, gui="edgechromium")


def run_fallback(config: dict) -> None:
    """Mode secours sans écran de garde : un ordre de verrouillage engage directement le
    verrou du système (mot de passe exigé). Le déverrouillage se fait alors avec le mot de
    passe de la session, pas par le visage ni le code Opal."""
    print("Mode secours : verrou du système seulement (écran Opal indisponible).")
    commands: queue.Queue = queue.Queue()
    link: dict = {}
    threading.Thread(target=network_loop, args=(config, commands, link), daemon=True).start()
    if LOCK_FLAG.exists():
        native_lock()
    while True:
        if commands.get()["cmd"] == "lock":
            native_lock()
            link.get("post", lambda _l: None)(True)


def main() -> None:
    # Journal ligne par ligne (sinon, redirigé vers un fichier par launchd, il n'apparaît
    # qu'au bout de plusieurs Ko). Sous pythonw (tâche planifiée Windows), il n'y a pas de
    # console : le journal va dans ~/.opal_guard/agent.log.
    if sys.stdout is None or sys.stderr is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(CONFIG_DIR / "agent.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log_file
        sys.stderr = sys.stderr or log_file
        print(f"--- démarrage {time.strftime('%Y-%m-%d %H:%M:%S')} ({sys.executable})")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(line_buffering=True)
    if IS_MAC and os.environ.get("SYSTEM_VERSION_COMPAT") != "0":
        # macOS 26 annonce « 16 » aux programmes compilés avec un ancien SDK, et Tk refuse
        # alors de démarrer (« macOS 26 or later required, have instead 16 »). On se relance
        # en demandant la vraie version du système.
        os.environ["SYSTEM_VERSION_COMPAT"] = "0"
        os.execv(sys.executable, [sys.executable, *sys.argv])
    if "--setup" in sys.argv:
        setup()
        return
    config = load_config()
    if not config:
        sys.exit("Pas encore configuré. Lance d'abord : python opal_guard.py --setup")
    # Sous Windows : le nouvel écran (fenêtre Edge) si pywebview et la page sont là ;
    # sinon l'ancien écran Tk, puis le mode secours.
    html = web_page() if IS_WINDOWS else None
    if html:
        try:
            import webview  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            print(f"pywebview indisponible ({exc!r}) : écran Tk", file=sys.stderr)
        else:
            run_web(config, html)
            return
    if not tk_works():
        run_fallback(config)
        return

    commands: queue.Queue = queue.Queue()
    locked_flag: dict = {}
    screen = LockScreen(config, on_unlock=lambda: None,
                        report=lambda locked: locked_flag.get("post", lambda _l: None)(locked))
    threading.Thread(target=network_loop, args=(config, commands, locked_flag), daemon=True).start()

    # Si quelqu'un arrête l'agent pendant le verrouillage, on engage le verrou du système.
    import atexit
    import signal

    def on_exit(*_args) -> None:  # noqa: ANN002
        if screen._locked:
            native_lock()

    atexit.register(on_exit)
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, lambda *a: (on_exit(), sys.exit(0)))

    # Relancé alors qu'il était verrouillé (agent tué, redémarrage) : on reverrouille aussitôt.
    if LOCK_FLAG.exists():
        commands.put({"cmd": "lock", "reason": "Verrouillage repris après un redémarrage de l'agent."})
    screen.pump(commands)
    print(f"Agent Opal Gardien prêt ({config['machine']}). En attente des ordres du pont.")
    screen.root.mainloop()


if __name__ == "__main__":
    main()
