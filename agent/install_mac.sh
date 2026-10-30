#!/bin/bash
# Installe ou met à jour l'agent Opal Gardien sur macOS (démarrage à l'ouverture de session).
# Il faut avoir lancé une fois « python3 opal_guard.py --setup » (jeton + code secret).
#   cd ~/.opal_guard && bash install_mac.sh
set -u
VPS=ubuntu@51.210.177.60
SRC=${SRC:-/home/ubuntu/Opal_esp32/agent}
DIR=$HOME/.opal_guard
LABEL=pro.inoma.opal-guard
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
LOG=/tmp/opal-guard.log

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
[ -f "$DIR/config.json" ] || { echo "Pas configuré : lance d'abord python3 $DIR/opal_guard.py --setup"; exit 1; }

echo "1/4 Récupération de l'agent depuis le VPS…"
scp -q "$VPS:$SRC/opal_guard.py" "$VPS:$SRC/$LABEL.plist" "$DIR/" || exit 1

echo "2/4 Recherche d'un Python capable d'ouvrir l'écran de garde (Tk)…"
PY=""
for c in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3)"; do
  [ -x "$c" ] || continue
  if "$c" -c "import tkinter; tkinter.Tk().destroy()" >/dev/null 2>&1; then PY=$c; break; fi
done
if [ -z "$PY" ]; then
  PY=$(command -v python3)
  echo "    Aucun Python avec Tk : mode secours (verrou macOS seul)."
  echo "    Pour l'écran Opal, installe Python depuis python.org puis relance ce script."
else
  echo "    $PY"
fi

echo "3/4 Vérification HTTPS vers le pont (certificats)…"
https_ok() {
  "$PY" - <<'EOF'
import ssl, sys, urllib.error, urllib.request
try:
    urllib.request.urlopen("https://iris.inoma.pro", timeout=8)
except urllib.error.HTTPError:
    pass  # le serveur a répondu : le HTTPS fonctionne
except urllib.error.URLError as exc:
    sys.exit(3 if isinstance(exc.reason, ssl.SSLCertVerificationError) else 0)
EOF
}
if ! https_ok; then
  echo "    Certificats manquants : installation (Install Certificates.command)…"
  for f in /Applications/Python\ 3*/Install\ Certificates.command; do
    [ -f "$f" ] && bash "$f" >/dev/null 2>&1
  done
  https_ok || { echo "    Toujours impossible : double-clique sur Install Certificates.command"; exit 1; }
fi
echo "    OK"

echo "4/4 Démarrage automatique…"
sed -e "s#__PYTHON__#$PY#" -e "s#__SCRIPT__#$DIR/opal_guard.py#" "$DIR/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null || { echo "    plist invalide"; exit 1; }
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
sleep 2  # laisse l'ancien agent s'arrêter, sinon bootstrap échoue (« Input/output error »)
: > "$LOG"
launchctl bootstrap "gui/$(id -u)" "$PLIST" || { echo "    échec du démarrage"; exit 1; }
sleep 8
echo
launchctl print "gui/$(id -u)/$LABEL" | grep -E "^[[:space:]]*(state|pid) =" | sed 's/^[[:space:]]*/    /'
echo "--- journal de l'agent :"
cat "$LOG"
