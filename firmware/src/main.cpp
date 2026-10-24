// Opal — boîtier vocal ESP32-S3 relié au cerveau Iris via WebSocket.
//
// Maintenir le bouton => le micro est streamé vers le PC.
// Relâcher          => Iris transcrit, réfléchit, et renvoie sa voix en PCM.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP_I2S.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <freertos/ringbuf.h>

#include "ca_certs.h"
#include "config.h"
#include "eyes.h"
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

enum class State { Connecting, Idle, Listening, Thinking, Speaking, Error };

static I2SClass mic;
static I2SClass spk;
static WebSocketsClient ws;
static RingbufHandle_t spkBuffer;

static volatile State state = State::Connecting;
static volatile bool discardAudio = false;  // ignore la fin d'une réponse interrompue
// Expression choisie par Opal (outil opal_box_yeux) : prioritaire sur les états.
static bool eyesHeld = false;
static uint32_t eyesHoldUntil = 0;  // 0 = jusqu'à la prochaine prise de parole
static volatile uint32_t spkRate = SPK_DEFAULT_RATE;
static bool recording = false;
static uint32_t recordStart = 0;
static int32_t micRaw[MIC_CHUNK_SAMPLES];
static int16_t micPcm[MIC_CHUNK_SAMPLES];

// ---------------------------------------------------------------------------
// LED d'état
// ---------------------------------------------------------------------------
static void showState() {
  uint8_t r = 0, g = 0, b = 0;
  switch (state) {
    case State::Connecting: r = 40; g = 20; break;           // orange
    case State::Idle:       g = 10; b = 10; break;           // cyan doux
    case State::Listening:  r = 0; g = 80; b = 0; break;     // vert
    case State::Thinking:   r = 60; g = 0; b = 80; break;    // violet
    case State::Speaking:   b = 90; break;                   // bleu
    case State::Error:      r = 90; break;                   // rouge
  }
  uint16_t k = LED_BRIGHTNESS;
  rgbLedWrite(LED_PIN, r * k / 90, g * k / 90, b * k / 90);
}

// Expression des yeux pour chaque état d'Opal.
static void showEyes() {
  if (eyesHeld) return;
  switch (state) {
    case State::Connecting:
      eyes::setMood(eyes::Mood::Sleepy);
      eyes::autoGaze();
      break;
    case State::Idle:
      eyes::setMood(eyes::Mood::Neutral);
      eyes::autoGaze();
      break;
    case State::Listening:  // attentif, regarde en face
      eyes::setMood(eyes::Mood::Surprised);
      eyes::lookAt(0, 0);
      break;
    case State::Thinking:  // regarde en haut sur le côté
      eyes::setMood(eyes::Mood::Neutral);
      eyes::lookAt(0.55f, -0.6f);
      break;
    case State::Speaking:
      eyes::setMood(eyes::Mood::Happy);
      eyes::autoGaze();
      break;
    case State::Error:
      eyes::setMood(eyes::Mood::Sad);
      eyes::lookAt(0, 0.5f);
      break;
  }
}

static void setState(State s) {
  if (s == state && s != State::Connecting) return;
  state = s;
  showState();
  showEyes();
}

static void releaseEyes() {
  if (!eyesHeld) return;
  eyesHeld = false;
  eyes::setPupil(-1);
  showEyes();
}

// {"type":"eyes","mood":"happy","look":[x,y]|"auto","blink":true,"wink":0|1,
//  "pupil":0.1,"hold_ms":8000}
static void applyEyesCommand(JsonDocument& doc) {
  static const char* const MOODS[] = {"neutral", "happy", "sad", "angry", "surprised", "sleepy"};
  const char* mood = doc["mood"] | "";
  for (int i = 0; i < 6; i++) {
    if (strcmp(mood, MOODS[i]) == 0) eyes::setMood((eyes::Mood)i);
  }
  JsonVariant look = doc["look"];
  if (look.is<JsonArrayConst>()) eyes::lookAt(look[0] | 0.f, look[1] | 0.f);
  else if (look.is<const char*>()) eyes::autoGaze();
  if (doc["blink"] | false) eyes::blink();
  if (!doc["wink"].isNull()) eyes::wink(doc["wink"] | 1);
  if (!doc["pupil"].isNull()) eyes::setPupil(doc["pupil"] | -1.f);

  uint32_t hold = doc["hold_ms"] | 8000;
  eyesHeld = true;
  eyesHoldUntil = hold ? millis() + hold : 0;
  if (eyesHoldUntil == 0 && hold) eyesHoldUntil = 1;
}


// ---------------------------------------------------------------------------
// Haut-parleur : tâche dédiée qui vide le tampon PSRAM vers l'I2S
// ---------------------------------------------------------------------------
static void speakerTask(void*) {
  uint32_t currentRate = SPK_DEFAULT_RATE;
  for (;;) {
    size_t len = 0;
    auto* data = (int16_t*)xRingbufferReceiveUpTo(spkBuffer, &len, pdMS_TO_TICKS(50), 2048);
    if (!data) continue;
    if (spkRate != currentRate) {
      currentRate = spkRate;
      spk.configureTX(currentRate, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
    }
    size_t n = len / 2;
    for (size_t i = 0; i < n; i++) data[i] = (int32_t)data[i] * SPK_VOLUME / 100;
    spk.write((uint8_t*)data, n * 2);
    vRingbufferReturnItem(spkBuffer, data);
  }
}

static void flushSpeaker() {
  size_t len;
  void* item;
  while ((item = xRingbufferReceiveUpTo(spkBuffer, &len, 0, SPK_BUFFER_BYTES)) != nullptr) {
    vRingbufferReturnItem(spkBuffer, item);
  }
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
static void sendJson(const char* type) {
  JsonDocument doc;
  doc["type"] = type;
  if (strcmp(type, "hello") == 0) {
    doc["device"] = "opal";
    doc["mic_rate"] = MIC_RATE;
    doc["fw"] = "0.1.0";
  }
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
}

static void onText(const uint8_t* payload, size_t length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  const char* type = doc["type"] | "";

  if (strcmp(type, "state") == 0) {
    const char* v = doc["value"] | "";
    if (strcmp(v, "thinking") == 0) setState(State::Thinking);
    else if (strcmp(v, "speaking") == 0 && !discardAudio) setState(State::Speaking);
    else if (strcmp(v, "idle") == 0 && !recording) setState(State::Idle);
  } else if (strcmp(type, "transcript") == 0) {
    Serial.printf("Toi  : %s\n", (const char*)(doc["text"] | ""));
  } else if (strcmp(type, "reply") == 0) {
    Serial.printf("Iris : %s\n", (const char*)(doc["text"] | ""));
  } else if (strcmp(type, "audio_start") == 0) {
    spkRate = doc["rate"] | SPK_DEFAULT_RATE;
  } else if (strcmp(type, "audio_end") == 0) {
    discardAudio = false;
  } else if (strcmp(type, "eyes") == 0) {
    applyEyesCommand(doc);
  } else if (strcmp(type, "error") == 0) {
    Serial.printf("Erreur : %s\n", (const char*)(doc["message"] | ""));
    setState(State::Error);
  }
}

static void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      Serial.println("Connecté au pont Iris");
      sendJson("hello");
      setState(State::Idle);
      break;
    case WStype_DISCONNECTED:
      if (state != State::Connecting) Serial.println("Pont Iris déconnecté");
      recording = false;
      setState(State::Connecting);
      break;
    case WStype_TEXT:
      onText(payload, length);
      break;
    case WStype_BIN:
      if (!discardAudio) {
        // Bloque si le tampon est plein : TCP ralentit alors le serveur.
        xRingbufferSend(spkBuffer, payload, length, pdMS_TO_TICKS(2000));
      }
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------------------
// Push-to-talk
// ---------------------------------------------------------------------------
static void startRecording() {
  if (state == State::Speaking || state == State::Thinking) {
    // Interruption : on coupe la voix en cours et on ignore la suite.
    discardAudio = true;
    flushSpeaker();
  }
  recording = true;
  recordStart = millis();
  releaseEyes();
  sendJson("start");
  setState(State::Listening);
}

static void stopRecording() {
  recording = false;
  sendJson("stop");
  setState(State::Thinking);
}

static void streamMic() {
  size_t bytes = mic.readBytes((char*)micRaw, sizeof(micRaw));
  size_t n = bytes / sizeof(int32_t);
  for (size_t i = 0; i < n; i++) {
    int32_t s = micRaw[i] >> MIC_GAIN_SHIFT;
    micPcm[i] = (int16_t)constrain(s, -32768, 32767);
  }
  if (n) ws.sendBIN((uint8_t*)micPcm, n * sizeof(int16_t));
}

static void handleButton() {
  static bool last = false;
  static uint32_t changedAt = 0;
  bool pressed = digitalRead(PTT_PIN) == LOW;
  if (pressed != last && millis() - changedAt > 30) {  // anti-rebond
    last = pressed;
    changedAt = millis();
    if (!ws.isConnected()) return;
    if (pressed && !recording) startRecording();
    else if (!pressed && recording) stopRecording();
  }
  if (recording && millis() - recordStart > MAX_RECORD_MS) stopRecording();
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  pinMode(PTT_PIN, INPUT_PULLUP);
  Serial.printf("\nOpal — PSRAM %lu Ko libre\n", (unsigned long)(ESP.getFreePsram() / 1024));

  eyes::begin();  // les yeux tournent seuls, un œil par cœur
  setState(State::Connecting);

  spkBuffer = xRingbufferCreateWithCaps(SPK_BUFFER_BYTES, RINGBUF_TYPE_BYTEBUF, MALLOC_CAP_SPIRAM);
  if (!spkBuffer) {
    Serial.println("Impossible d'allouer le tampon audio en PSRAM");
    setState(State::Error);
    while (true) delay(1000);
  }

  mic.setPins(MIC_PIN_SCK, MIC_PIN_WS, -1, MIC_PIN_SD);
  if (!mic.begin(I2S_MODE_STD, MIC_RATE, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO,
                 I2S_STD_SLOT_LEFT)) {
    Serial.println("Échec init micro I2S");
  }
  spk.setPins(SPK_PIN_BCLK, SPK_PIN_LRC, SPK_PIN_DIN);
  if (!spk.begin(I2S_MODE_STD, SPK_DEFAULT_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("Échec init haut-parleur I2S");
  }
  xTaskCreatePinnedToCore(speakerTask, "speaker", 4096, nullptr, 5, nullptr, 0);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // latence audio
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("WiFi %s", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print('.');
  }
  Serial.printf("\nIP %s -> pont %s://%s:%d%s\n", WiFi.localIP().toString().c_str(),
                OPAL_USE_TLS ? "wss" : "ws", OPAL_SERVER_HOST, OPAL_SERVER_PORT,
                OPAL_SERVER_PATH);

#if OPAL_USE_TLS
  // Le certificat du VPS n'est vérifiable qu'avec l'heure exacte.
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
  ws.setReconnectInterval(2000);
  ws.enableHeartbeat(15000, 3000, 2);
}

void loop() {
  ws.loop();
  handleButton();
  if (eyesHeld && eyesHoldUntil && (int32_t)(millis() - eyesHoldUntil) >= 0) releaseEyes();
  static uint32_t lastFps = 0;
  if (millis() - lastFps > 30000) {
    lastFps = millis();
    Serial.printf("yeux : %.1f fps\n", eyes::fps());
  }
  if (recording) streamMic();
  else delay(2);
}
