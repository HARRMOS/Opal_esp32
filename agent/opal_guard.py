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
            # Suspend la session → écran de connexion macOS (mot de passe exigé).
            os.system(
                "/System/Library/CoreServices/Menu\\ Extras/User.menu/Contents/Resources/"
                "CGSession -suspend"
            )
        else:
            os.system("loginctl lock-session || xdg-screensaver lock")
    except Exception as exc:  # noqa: BLE001
        print(f"verrou natif indisponible : {exc}", file=sys.stderr)


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

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Opal Gardien")
        self.root.configure(bg="#07060a")
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)  # la croix ne ferme pas

        self._build()
        # Toute touche est avalée tant que c'est verrouillé (sauf le clavier à l'écran).
        self.root.bind_all("<Key>", self._swallow_key)
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
        for i, label in enumerate(["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "←"]):
            btn = tk.Button(
                keys, text=label, width=5, height=2, font=("Segoe UI", 18),
                bg="#151114", fg="#efe9e4", activebackground="#22181b", activeforeground="#fff",
                relief="flat", bd=0, highlightthickness=0,
                command=lambda k=label: self._key(k))
            btn.grid(row=i // 3, column=i % 3, padx=5, pady=5)

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
            self.root.call("::tk::unsupported::MacWindowStyle", "style", self.root._w, "plain", "none")
        self.root.lift()
        self.root.focus_force()
        try:
            self.root.grab_set_global()
        except tk.TclError:
            pass
        # Laisse une seconde avant d'armer la détection de fuite (le temps de prendre le focus).
        self.root.after(1200, lambda: setattr(self, "_tamper_armed", True))
        self._report(True)

    def unlock(self, by: str) -> None:
        if not self._locked:
            return
        self._locked = False
        self._tamper_armed = False
        try:
            self.root.grab_release()
        except tk.TclError:
            pass
        self.root.attributes("-topmost", False)
        self.root.withdraw()
        self._report(False)
        print(f"déverrouillé ({by})")

    # ── Saisie du code ──
    def _key(self, k: str) -> None:
        if not self._locked:
            return
        if k == "C":
            self._entry = ""
        elif k == "←":
            self._entry = self._entry[:-1]
        elif len(self._entry) < 12:
            self._entry += k
        self._refresh()
        if len(self._entry) >= 4 and k not in ("C", "←") and self._check():
            pass

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
        self.dots.config(text="●" * len(self._entry) + "○" * max(0, 4 - len(self._entry)))
        if not keep_status:
            self.status.config(text="Entre ton code", fg="#9b8f8c")

    # ── Anti-contournement ──
    def _swallow_key(self, event):  # noqa: ANN001
        # Les boutons du clavier à l'écran gèrent la saisie ; toute autre touche est ignorée.
        return "break" if self._locked else None

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




def main() -> None:
    if "--setup" in sys.argv:
        setup()
        return
    config = load_config()
    if not config:
        sys.exit("Pas encore configuré. Lance d'abord : python opal_guard.py --setup")

    commands: queue.Queue = queue.Queue()
    locked_flag: dict = {}
    screen = LockScreen(config, on_unlock=lambda: None,
                        report=lambda locked: locked_flag.get("post", lambda _l: None)(locked))
    threading.Thread(target=network_loop, args=(config, commands, locked_flag), daemon=True).start()
    screen.pump(commands)
    print(f"Agent Opal Gardien prêt ({config['machine']}). En attente des ordres du pont.")
    screen.root.mainloop()


if __name__ == "__main__":
    main()
