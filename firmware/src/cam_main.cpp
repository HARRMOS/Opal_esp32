// Opal Box — caméra (ESP32-CAM AI-Thinker) reliée au pont Opal en wss.
// Capteur OV2640 (JPEG matériel) ou GC2145 « RHYX M21-45 » (sans JPEG : l'image brute
// est compressée en JPEG par l'ESP32, un peu plus lentement).
//
// Envoie en continu de petites images JPEG (suivi du visage, présence, direct vidéo)
// et une photo haute définition quand Opal en demande une (outil opal_box_photo).
// Trames binaires préfixées d'un octet : 0x01 image du flux, 0x02 photo demandée.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <img_converters.h>

#include "ca_certs.h"
#include "config.h"
#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "include/secrets.h absent : copie secrets.example.h en secrets.h"
#include "secrets.example.h"
#endif
#ifndef OPAL_USE_TLS
#define OPAL_USE_TLS 1
#endif
#ifndef OPAL_SERVER_PORT
#define OPAL_SERVER_PORT (OPAL_USE_TLS ? 443 : 8765)
#endif
#ifndef OPAL_DEVICE_TOKEN
#error "Ajoute #define OPAL_DEVICE_TOKEN \"...\" dans include/secrets.h (voir secrets.example.h)"
#endif

// Broches de l'ESP32-CAM AI-Thinker
#define CAM_PIN_PWDN 32
#define CAM_PIN_XCLK 0
#define CAM_PIN_SIOD 26
#define CAM_PIN_SIOC 27
#define CAM_PIN_D7 35
#define CAM_PIN_D6 34
#define CAM_PIN_D5 39
#define CAM_PIN_D4 36
#define CAM_PIN_D3 21
#define CAM_PIN_D2 19
#define CAM_PIN_D1 18
#define CAM_PIN_D0 5
#define CAM_PIN_VSYNC 25
#define CAM_PIN_HREF 23
#define CAM_PIN_PCLK 22
#define FLASH_LED 4    // grosse LED blanche
#define STATUS_LED 33  // petite LED rouge, allumée à LOW

// Orientation selon le montage de la caméra dans le boîtier
#define CAM_VFLIP 0
#define CAM_HMIRROR 0

static WebSocketsClient ws;
static uint8_t* txBuffer;  // [type][jpeg...] en PSRAM
static size_t txCapacity = 0;
static uint32_t frameIntervalMs = 1000 / 6;
static framesize_t streamSize = FRAMESIZE_QVGA;
static uint32_t lastFrame = 0;
static bool snapshotRequested = false, snapshotFlash = false;
static bool softJpeg = false;  // capteur sans JPEG matériel (GC2145)
static const char* sensorName = "?";
static camera_config_t camConfig = {};

static bool sendBytes(uint8_t kind, const uint8_t* jpeg, size_t len) {
  if (len + 1 > txCapacity) return false;
  txBuffer[0] = kind;
  memcpy(txBuffer + 1, jpeg, len);
  return ws.sendBIN(txBuffer, len + 1);
}

static bool sendJpeg(uint8_t kind, camera_fb_t* fb) {
  if (fb->format == PIXFORMAT_JPEG) return sendBytes(kind, fb->buf, fb->len);
  uint8_t* jpeg = nullptr;
  size_t len = 0;
  if (!frame2jpg(fb, kind == 0x02 ? 85 : 70, &jpeg, &len)) return false;
  bool ok = sendBytes(kind, jpeg, len);
  free(jpeg);
  return ok;
}

static void applyOrientation() {
  sensor_t* s = esp_camera_sensor_get();
  if (!s) return;
  s->set_vflip(s, CAM_VFLIP);
  s->set_hmirror(s, CAM_HMIRROR);
}

static void setFrameSize(framesize_t size) {
  if (!softJpeg) {  // en JPEG, le pilote change de taille à la volée
    sensor_t* s = esp_camera_sensor_get();
    if (s) s->set_framesize(s, size);
    return;
  }
  // En image brute, les tampons ont la taille exacte d'une image : on redémarre la caméra.
  if (camConfig.frame_size == size) return;
  esp_camera_deinit();
  camConfig.frame_size = size;
  if (esp_camera_init(&camConfig) != ESP_OK) Serial.println("Changement de taille impossible");
  applyOrientation();
}

// Photo pour Opal : SVGA (800x600), quelques images jetées le temps que l'exposition s'adapte.
static void takeSnapshot() {
  snapshotRequested = false;
  if (snapshotFlash) digitalWrite(FLASH_LED, HIGH);
  setFrameSize(FRAMESIZE_SVGA);
  for (int i = 0; i < 3; i++) {
    camera_fb_t* skip = esp_camera_fb_get();
    if (skip) esp_camera_fb_return(skip);
  }
  camera_fb_t* fb = esp_camera_fb_get();
  digitalWrite(FLASH_LED, LOW);
  if (fb) {
    Serial.printf("Photo %ux%u -> %s\n", fb->width, fb->height,
                  sendJpeg(0x02, fb) ? "envoyée" : "échec");
    esp_camera_fb_return(fb);
  }
  setFrameSize(streamSize);
}

static void onText(const uint8_t* payload, size_t length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  const char* type = doc["type"] | "";
  if (strcmp(type, "cam") == 0) {
    uint32_t fps = constrain((int)(doc["fps"] | 6), 1, 15);
    frameIntervalMs = 1000 / fps;
    streamSize = strcmp(doc["size"] | "qvga", "vga") == 0 ? FRAMESIZE_VGA : FRAMESIZE_QVGA;
    setFrameSize(streamSize);
    Serial.printf("Flux : %lu img/s, %s\n", (unsigned long)fps, doc["size"] | "qvga");
  } else if (strcmp(type, "snapshot") == 0) {
    snapshotFlash = doc["flash"] | false;
    snapshotRequested = true;
  }
}

static void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED: {
      Serial.println("Connecté au pont Opal");
      digitalWrite(STATUS_LED, LOW);
      JsonDocument hello;
      hello["type"] = "hello";
      hello["device"] = "camera";
      hello["sensor"] = sensorName;
      hello["soft_jpeg"] = softJpeg;
      hello["fw"] = "0.3.0";
      String out;
      serializeJson(hello, out);
      ws.sendTXT(out);
      break;
    }
    case WStype_DISCONNECTED:
      digitalWrite(STATUS_LED, HIGH);
      Serial.printf("Pont déconnecté (RAM interne libre %u, plus grand bloc %u)\n",
                    heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                    heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
      break;
    case WStype_TEXT:
      onText(payload, length);
      break;
    default:
      break;
  }
}

static bool initCamera() {
  camera_config_t config = {};
  config.pin_pwdn = CAM_PIN_PWDN;
  config.pin_reset = -1;
  config.pin_xclk = CAM_PIN_XCLK;
  config.pin_sccb_sda = CAM_PIN_SIOD;
  config.pin_sccb_scl = CAM_PIN_SIOC;
  config.pin_d7 = CAM_PIN_D7;
  config.pin_d6 = CAM_PIN_D6;
  config.pin_d5 = CAM_PIN_D5;
  config.pin_d4 = CAM_PIN_D4;
  config.pin_d3 = CAM_PIN_D3;
  config.pin_d2 = CAM_PIN_D2;
  config.pin_d1 = CAM_PIN_D1;
  config.pin_d0 = CAM_PIN_D0;
  config.pin_vsync = CAM_PIN_VSYNC;
  config.pin_href = CAM_PIN_HREF;
  config.pin_pclk = CAM_PIN_PCLK;
  config.xclk_freq_hz = 20000000;
  config.ledc_timer = LEDC_TIMER_0;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_SVGA;  // tampons dimensionnés pour la photo la plus grande
  config.jpeg_quality = 12;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  if (esp_camera_init(&config) != ESP_OK) {
    // Capteur sans JPEG matériel (GC2145…) : image brute RGB565, compressée par l'ESP32.
    // Tampons à la taille exacte du flux ; la photo redémarre la caméra en SVGA.
    esp_camera_deinit();
    config.pixel_format = PIXFORMAT_RGB565;
    config.frame_size = streamSize;
    if (esp_camera_init(&config) != ESP_OK) return false;
    softJpeg = true;
  }
  camConfig = config;
  sensor_t* s = esp_camera_sensor_get();
  switch (s->id.PID) {
    case OV2640_PID: sensorName = "ov2640"; break;
    case OV3660_PID: sensorName = "ov3660"; break;
    case GC2145_PID: sensorName = "gc2145"; break;
    default: sensorName = "autre"; break;
  }
  Serial.printf("Capteur %s (PID 0x%04x)%s\n", sensorName, s->id.PID,
                softJpeg ? ", JPEG logiciel" : "");
  applyOrientation();
  if (!softJpeg) s->set_framesize(s, streamSize);
  return true;
}

void setup() {
  Serial.begin(115200);
  pinMode(FLASH_LED, OUTPUT);
  digitalWrite(FLASH_LED, LOW);
  pinMode(STATUS_LED, OUTPUT);
  digitalWrite(STATUS_LED, HIGH);
  Serial.printf("\nOpal Box caméra — PSRAM %lu Ko\n", (unsigned long)(ESP.getFreePsram() / 1024));

  if (!initCamera()) {
    Serial.println("Caméra introuvable : vérifie la nappe du capteur");
    while (true) {
      digitalWrite(STATUS_LED, !digitalRead(STATUS_LED));
      delay(200);
    }
  }
  txCapacity = 200 * 1024;
  txBuffer = (uint8_t*)ps_malloc(txCapacity);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("WiFi %s", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print('.');
  }
  Serial.printf("\nIP %s -> %s://%s:%d%s\n", WiFi.localIP().toString().c_str(),
                OPAL_USE_TLS ? "wss" : "ws", OPAL_SERVER_HOST, OPAL_SERVER_PORT, OPAL_SERVER_PATH);

#if OPAL_USE_TLS
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  Serial.print("Heure NTP");
  while (time(nullptr) < 1700000000) {
    delay(300);
    Serial.print('.');
  }
  Serial.println(" ok");
  ws.beginSslWithCA(OPAL_SERVER_HOST, OPAL_SERVER_PORT, OPAL_SERVER_PATH, OPAL_ROOT_CA);
#else
  ws.begin(OPAL_SERVER_HOST, OPAL_SERVER_PORT, OPAL_SERVER_PATH);
#endif
  ws.setExtraHeaders("Authorization: Bearer " OPAL_DEVICE_TOKEN);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 3000, 2);

  // Mises à jour par WiFi (env cam-ota) : plus besoin de la passerelle ni du fil IO0.
  ArduinoOTA.setHostname("opal-cam");
  ArduinoOTA.setPassword(OPAL_DEVICE_TOKEN);
  ArduinoOTA.onStart([] {
    Serial.println("Mise à jour par WiFi...");
    ws.disconnect();
  });
  ArduinoOTA.begin();
}

void loop() {
  ArduinoOTA.handle();
  ws.loop();
  if (!ws.isConnected()) {
    delay(10);
    return;
  }
  if (snapshotRequested) {
    takeSnapshot();
    return;
  }
  if (millis() - lastFrame >= frameIntervalMs) {
    lastFrame = millis();
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb) {
      sendJpeg(0x01, fb);
      esp_camera_fb_return(fb);
    }
  }
  delay(1);
}
