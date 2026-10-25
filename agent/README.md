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

Même configuration (`python3 opal_guard.py --setup`), puis lance l'agent au login avec un
LaunchAgent (`~/Library/LaunchAgents/pro.inoma.opal-guard.plist`) pointant sur
`python3 .../opal_guard.py`. L'agent utilise le verrouillage natif macOS en secours.

## Le jeton du gardien

Un secret partagé entre l'agent et le pont, différent du mot de passe de la caméra. Il est
dans `deploy/opal-bridge.env` sur le VPS, variable `OPAL_GUARD_TOKEN`. L'agent le présente
pour recevoir les ordres verrouiller / déverrouiller, et **ne peut rien faire d'autre** (pas
d'accès à la caméra).

## Réglages (pont, `opal-bridge.env`)

- `OPAL_GUARD_ENABLED` : `1` (défaut) pour activer la garde automatique, `0` pour la couper.
- `OPAL_GUARD_LOCK_AFTER_S` : secondes de présence d'un inconnu seul avant verrouillage (défaut 30).
- `OPAL_GUARD_TOKEN` : le jeton ci-dessus.
