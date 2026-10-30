# Agent de verrouillage Opal (« Gardien »)

Quand un visage inconnu reste seul devant la caméra d'Opal pendant ton absence, Opal
verrouille tes ordinateurs. Ils se déverrouillent quand Opal te reconnaît devant la caméra,
ou avec ton code secret tapé sur l'écran de garde.

## Comment c'est vraiment verrouillé

L'agent affiche l'écran de garde Opal en plein écran, par-dessus tout. Mais aucun programme
ne peut bloquer `Ctrl+Alt+Suppr` sous Windows (ni toutes les touches système sous macOS) :
c'est une protection voulue du système. Alors dès que quelqu'un tente de sortir de l'écran
(changement de fenêtre, touche Windows, agent tué, ou 5 mauvais codes), l'agent bascule
aussitôt sur le **verrouillage natif du système**, qui exige ton mot de passe de session.

- L'écran Opal est la couche visible et le clavier de code.
- Le verrouillage natif du système est le vrai cadenas.
- Ton **mot de passe de session reste toujours une issue de secours** : tu ne peux jamais
  rester enfermé dehors de ta propre machine.

Le déverrouillage par reconnaissance de visage vient du pont (la caméra), **jamais de l'IA** :
on ne peut pas demander à Opal « déverrouille mon PC » par la voix.

## Installation (Windows)

1. Il faut un Python avec `tkinter` (l'installeur officiel python.org l'inclut).
2. Configure ton code secret (c'est toi qui le tapes, il n'est stocké que chiffré) :
   ```
   python opal_guard.py --setup
   ```
   On te demande l'adresse du pont (`https://iris.inoma.pro`), le **jeton du gardien**
   (`OPAL_GUARD_TOKEN`, voir ci-dessous) et ton code.
3. Démarre l'agent au logon :
   ```
   powershell -ExecutionPolicy Bypass -File installer_windows.ps1
   ```

## Installation (macOS)

0. tkinter : `python3 -c "import tkinter"`. Si erreur : `brew install python-tk` (ou installe
   Python depuis python.org, qui l'inclut).
1. Récupère l'agent (par exemple depuis le VPS) dans `~/.opal_guard/` :
   ```
   mkdir -p ~/.opal_guard
   scp ubuntu@51.210.177.60:/home/ubuntu/Opal_esp32/agent/opal_guard.py \
       ubuntu@51.210.177.60:/home/ubuntu/Opal_esp32/agent/pro.inoma.opal-guard.plist ~/.opal_guard/
   ```
2. Configure (ton code reste masqué, jamais transmis) :
   ```
   python3 ~/.opal_guard/opal_guard.py --setup
   ```
3. Démarre au login via un LaunchAgent (génère le plist avec les bons chemins) :
   ```
   sed -e "s#__PYTHON__#$(which python3)#" -e "s#__SCRIPT__#$HOME/.opal_guard/opal_guard.py#" \
     ~/.opal_guard/pro.inoma.opal-guard.plist > ~/Library/LaunchAgents/pro.inoma.opal-guard.plist
   launchctl load ~/Library/LaunchAgents/pro.inoma.opal-guard.plist
   ```
4. macOS demandera peut-être d'autoriser Python (Accessibilité / Surveillance de l'entrée) :
   accepte, sinon l'écran de garde ne peut pas garder la main.

Sur macOS, aucun programme ne peut non plus bloquer toutes les touches système : l'agent
s'appuie sur le **verrouillage natif macOS** (écran de connexion, mot de passe exigé) dès
qu'on tente de quitter l'écran de garde. Pour l'arrêter :
`launchctl unload ~/Library/LaunchAgents/pro.inoma.opal-guard.plist`.

## Le jeton du gardien

Un secret partagé entre l'agent et le pont, différent du mot de passe de la caméra. Il est
dans `deploy/opal-bridge.env` sur le VPS, variable `OPAL_GUARD_TOKEN`. L'agent le présente
pour recevoir les ordres verrouiller / déverrouiller, et **ne peut rien faire d'autre** (pas
d'accès à la caméra).

## Réglages (pont, `opal-bridge.env`)

- `OPAL_GUARD_ENABLED` : `1` (défaut) pour activer la garde automatique, `0` pour la couper.
- `OPAL_GUARD_LOCK_AFTER_S` : secondes de présence d'un inconnu seul avant verrouillage (défaut 30).
- `OPAL_GUARD_TOKEN` : le jeton ci-dessus.
