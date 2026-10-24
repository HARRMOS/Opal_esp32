#pragma once

// Copier ce fichier en secrets.h (ignoré par git) et remplir.
#define WIFI_SSID "MonWiFi"
#define WIFI_PASSWORD "motdepasse"

// Pont Opal sur le VPS, derrière Caddy (HTTPS Let's Encrypt).
#define OPAL_SERVER_HOST "opal.mondomaine.fr"
#define OPAL_SERVER_PORT 443
#define OPAL_USE_TLS 1
// Même valeur que OPAL_DEVICE_TOKEN sur le VPS.
#define OPAL_DEVICE_TOKEN "colle-ici-le-jeton"

// Variante réseau local sans TLS (pont lancé sur le PC avec --host 0.0.0.0) :
// #define OPAL_SERVER_HOST "192.168.1.10"
// #define OPAL_SERVER_PORT 8765
// #define OPAL_USE_TLS 0
