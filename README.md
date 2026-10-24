# Opal — le corps matériel d'Iris

Opal est un boîtier vocal ESP32-S3 : tu maintiens le bouton, tu parles, Iris
(qui tourne sur le VPS) te répond par le haut-parleur.

```
 [INMP441] --I2S--> ESP32-S3 --wss + jeton--> Traefik (VPS) --> opal_bridge --> Opal (Brain)
 [MAX98357A] <--I2S--   (N16R8)     <--- PCM 24 kHz ---   STT local + TTS PCM
```

## Matériel

| Module | Broche module | GPIO ESP32-S3 |
|---|---|---|
| Micro INMP441 | SCK | 5 |
| | WS | 6 |
| | SD | 4 |
| | L/R | GND |
| | VDD / GND | 3V3 / GND |
| Ampli MAX98357A | BCLK | 15 |
| | LRC | 16 |
| | DIN | 7 |
| | VIN / GND | 5V / GND |
| | SD, GAIN | non connectés |
| Bouton push-to-talk | — | 0 (bouton BOOT intégré) |
| LED RGB intégrée | — | 48 (38 sur DevKitC-1 v1.1) |

Ne pas utiliser les GPIO 35, 36, 37 : ils sont réservés à la PSRAM octale du N16R8.
Toutes les broches se changent dans [firmware/include/config.h](firmware/include/config.h).

LED : orange = connexion, cyan = prêt, vert = écoute, violet = réflexion,
bleu = parle, rouge = erreur.

Yeux : endormis pendant la connexion, regard libre au repos, grands ouverts
et fixés sur toi pendant l'écoute, en haut à droite pendant la réflexion,
contents quand Opal parle, tristes en cas d'erreur.

## Yeux (2 écrans ronds GC9A01)

![Aperçu des humeurs](docs/eyes_preview.png)

Chaque écran a son propre bus SPI (les deux sont rafraîchis en parallèle en DMA).

| Broche écran | Œil gauche | Œil droit |
|---|---|---|
| VCC / GND | 3V3 / GND | 3V3 / GND |
| SCL | 12 | 2 |
| SDA | 11 | 1 |
| DC | 10 | 42 |
| CS | 9 | 41 |
| RST | 14 | 14 (commun) |
| BLK | 3V3 | 3V3 |

Démo seule (humeurs en boucle, pilotable depuis le moniteur série) :

```bash
cd firmware && pio run -e eyes -t upload -t monitor
```

Touches : `n` neutre, `h` content, `s` triste, `a` fâché, `u` surpris,
`z` endormi, `b` cligner, `w`/`q`/`x`/`d`/espace pour orienter le regard,
`g` regard automatique, `p`/`P` pupille serrée/dilatée, `c` couleur d'iris.

Si l'image est parasitée, passer `EYE_SPI_HZ` à 40 MHz ; si les couleurs sont
inversées, `EYE_INVERT` à `false` ; si un œil est tourné, `EYE_L/R_ROTATION`
([firmware/include/config.h](firmware/include/config.h)).

## 1. Le pont sur le VPS (Docker + Traefik)

Le pont tourne sur le VPS à côté d'Opal, dans un conteneur qui réutilise
l'image d'Opal, et Traefik lui fournit le HTTPS. L'ESP32 s'y connecte en
`wss://<domaine>/opal` avec un jeton secret (en-tête `Authorization: Bearer`).

- Service : [deploy/docker-compose.opal-bridge.yml](deploy/docker-compose.opal-bridge.yml)
  (image, volume, réseau Traefik et certresolver à adapter au VPS).
- Jeton : `deploy/opal-bridge.env` sur le VPS (modèle :
  [deploy/opal-bridge.env.example](deploy/opal-bridge.env.example)).
- Voix : TTS OpenAI (PCM) si dispo, sinon Piper (`OPAL_PIPER_MODEL`) ou espeak-ng.

En réseau local sans VPS, on peut aussi lancer le pont sur le PC :

```bash
OPAL_DEVICE_TOKEN=... ../Iris/.venv/Scripts/python.exe server/opal_bridge.py --iris-dir ../Iris --host 0.0.0.0
```

## 2. Flasher l'ESP32

1. Copier `firmware/include/secrets.example.h` en `firmware/include/secrets.h`
   et remplir : WiFi, domaine du VPS, jeton (même valeur que `OPAL_DEVICE_TOKEN`).
2. Brancher la carte sur le port USB marqué **USB** (ou **COM**), puis :

```powershell
cd firmware
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run -e opal-s3 -t upload -t monitor
```

## Protocole

Documenté en tête de [server/opal_bridge.py](server/opal_bridge.py) :
JSON pour le contrôle, trames binaires PCM 16 bits mono pour l'audio
(16 kHz micro → VPS, 24 kHz VPS → haut-parleur).

## Réglages utiles

- Micro trop faible / saturé : `MIC_GAIN_SHIFT` (plus petit = plus fort).
- Volume : `SPK_VOLUME` (0–100).
- Appuyer pendant qu'Opal parle l'interrompt et relance l'écoute.
