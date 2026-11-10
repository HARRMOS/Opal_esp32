#include "wifi_setup.h"

#include <Arduino.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_wifi.h>

#include "config.h"
#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"
#endif

namespace wifisetup {
namespace {

constexpr uint32_t TRY_MS = 20000;
constexpr const char* PORTAL_SSID = "Opal-config";

String savedSsid, savedPass;

void loadCredentials() {
  Preferences prefs;
  prefs.begin("wifi", true);
  savedSsid = prefs.getString("ssid", WIFI_SSID);
  savedPass = prefs.getString("pass", WIFI_PASSWORD);
  prefs.end();
}

void saveCredentials(const String& ssid, const String& pass) {
  Preferences prefs;
  prefs.begin("wifi", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
}

void startRadio(wifi_mode_t mode) {
  WiFi.mode(mode);
  WiFi.setSleep(false);  // latence audio
  esp_wifi_set_country_code("FR", true);  // canaux 1 à 13 : les Bbox émettent souvent sur 12 ou 13
  WiFi.setTxPower(WIFI_POWER_15dBm);
}

bool tryConnect(uint32_t ms) {
  WiFi.begin(savedSsid.c_str(), savedPass.c_str());
  Serial.printf("WiFi %s", savedSsid.c_str());
  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < ms) {
    delay(300);
    Serial.print('.');
  }
  Serial.println();
  return WiFi.status() == WL_CONNECTED;
}

String htmlEscape(const String& s) {
  String out;
  for (char c : s) {
    if (c == '<') out += "&lt;";
    else if (c == '>') out += "&gt;";
    else if (c == '&') out += "&amp;";
    else if (c == '"') out += "&quot;";
    else out += c;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Portail : réseau « Opal-config » + page de choix du WiFi (s'ouvre seule sur le téléphone)
// ---------------------------------------------------------------------------
WebServer server(80);
DNSServer dns;
String networksHtml;

void scan() {
  const int n = WiFi.scanNetworks();
  Serial.printf("portail : %d réseaux captés\n", n);
  networksHtml = "";
  if (n <= 0) {
    networksHtml = "<p class=warn>Aucun réseau capté. Rapproche Opal de la box, ou vérifie l'antenne de la carte.</p>";
    return;
  }
  for (int i = 0; i < n; i++) {
    const String ssid = WiFi.SSID(i);
    if (ssid.isEmpty()) continue;
    const int rssi = WiFi.RSSI(i);
    const char* bars = rssi > -60 ? "▮▮▮▮" : rssi > -70 ? "▮▮▮▯" : rssi > -80 ? "▮▮▯▯" : "▮▯▯▯";
    const String esc = htmlEscape(ssid);
    networksHtml += "<label><input type=radio name=ssid value=\"" + esc + "\"" +
                    (ssid == savedSsid ? " checked" : "") + "><span>" + esc + "</span><small>" + bars +
                    " " + String(rssi) + " dBm · canal " + String(WiFi.channel(i)) + "</small></label>";
  }
  WiFi.scanDelete();
}

const char PAGE_HEAD[] PROGMEM = R"HTML(<!doctype html><html lang=fr><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Opal — WiFi</title><style>
body{font:16px -apple-system,system-ui,sans-serif;background:#14101a;color:#f3ece2;margin:0;padding:20px;max-width:480px}
h1{font-size:22px;margin:0 0 4px;color:#d8b46a}p{color:#bdb3a6;margin:0 0 16px}
label{display:flex;align-items:center;gap:10px;padding:12px;margin:6px 0;background:#231b2b;border-radius:10px}
label span{flex:1;word-break:break-all}small{color:#998f84;font-size:12px;white-space:nowrap}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:12px;margin:6px 0 14px;border-radius:10px;
border:1px solid #4a3a55;background:#1b1522;color:#f3ece2;font-size:16px}
button{width:100%;padding:14px;border:0;border-radius:10px;background:#7a1f35;color:#fff;font-size:17px;font-weight:600}
a{color:#d8b46a}.warn{color:#ff9a8a}</style>
<h1>Connecter Opal au WiFi</h1><p>Choisis ta box (seuls les réseaux 2,4 GHz apparaissent), puis tape son mot de passe.</p>
<form method=post action=/save>)HTML";

const char PAGE_TAIL[] PROGMEM = R"HTML(<p style="margin-top:14px">Réseau masqué ? Écris son nom :</p>
<input type=text name=other placeholder="Nom du réseau (facultatif)" autocapitalize=off autocorrect=off>
<p>Mot de passe :</p><input type=password name=pass autocapitalize=off autocorrect=off>
<button>Connecter</button></form><p style="margin-top:16px"><a href=/rescan>Rechercher à nouveau</a></p></html>)HTML";

void handleRoot() {
  String page = FPSTR(PAGE_HEAD);
  page += networksHtml;
  page += FPSTR(PAGE_TAIL);
  server.send(200, "text/html; charset=utf-8", page);
}

void handleSave() {
  String ssid = server.arg("other");
  ssid.trim();
  if (ssid.isEmpty()) ssid = server.arg("ssid");
  if (ssid.isEmpty()) {
    server.sendHeader("Location", "/");
    server.send(302);
    return;
  }
  saveCredentials(ssid, server.arg("pass"));
  server.send(200, "text/html; charset=utf-8",
              "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
              "<body style='font:17px system-ui;background:#14101a;color:#f3ece2;padding:24px'>"
              "<h2 style='color:#d8b46a'>C'est noté</h2><p>Opal redémarre et se connecte à <b>" +
                  htmlEscape(ssid) +
                  "</b>.</p><p>LED cyan : c'est bon. Si « Opal-config » réapparaît, le mot de passe "
                  "était faux : recommence.</p>");
  Serial.printf("portail : nouveau réseau %s, redémarrage\n", ssid.c_str());
  delay(1500);
  ESP.restart();
}

[[noreturn]] void runPortal() {
  Serial.printf("portail : réseau « %s » ouvert, connecte-toi avec le téléphone\n", PORTAL_SSID);
  WiFi.disconnect();
  startRadio(WIFI_AP_STA);
  WiFi.softAP(PORTAL_SSID);
  delay(200);
  scan();
  dns.start(53, "*", WiFi.softAPIP());  // toute adresse mène à la page : elle s'ouvre seule
  server.on("/", handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.on("/rescan", [] {
    scan();
    server.sendHeader("Location", "/");
    server.send(302);
  });
  server.onNotFound([] {
    server.sendHeader("Location", "http://" + WiFi.softAPIP().toString() + "/");
    server.send(302);
  });
  server.begin();
  uint32_t lastRetry = millis();
  for (;;) {
    dns.processNextRequest();
    server.handleClient();
    // LED blanche qui respire : en attente de réglage.
    const uint8_t k = 20 + 40 * (0.5f + 0.5f * sinf(millis() / 600.f));
    rgbLedWrite(LED_PIN, k, k, k);
    // Box redémarrée ou revenue à portée : on retente le réseau enregistré toutes les 3 min.
    if (millis() - lastRetry > 180000 && WiFi.softAPgetStationNum() == 0) {
      lastRetry = millis();
      if (tryConnect(TRY_MS)) ESP.restart();
    }
    delay(2);
  }
}

}  // namespace

void connect() {
  // Raison de chaque échec (15 = mot de passe refusé, 201 = réseau introuvable, 2/4/8 = box qui
  // refuse ou coupe, 202/203/204 = authentification) : visible sur le port USB.
  WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t info) {
    Serial.printf("\nWiFi refusé, raison %u\n", info.wifi_sta_disconnected.reason);
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  loadCredentials();
  Serial.printf("WiFi enregistré : %s (%u caractères de mot de passe)\n", savedSsid.c_str(), savedPass.length());
  startRadio(WIFI_STA);
  if (tryConnect(TRY_MS)) return;
  // Un réseau tapé dans le portail peut être faux : on retente celui de secrets.h avant le portail,
  // et s'il passe il redevient le réseau enregistré.
  if (savedSsid != WIFI_SSID || savedPass != WIFI_PASSWORD) {
    savedSsid = WIFI_SSID;
    savedPass = WIFI_PASSWORD;
    if (tryConnect(TRY_MS)) {
      saveCredentials(savedSsid, savedPass);
      return;
    }
  }
  Serial.println("WiFi : échec, ouverture du portail de réglage");
  runPortal();
}

}  // namespace wifisetup
