// Démo du corps d'Opal sans WiFi (env « robot ») : les yeux, les deux servos du cou et le
// haut-parleur (MAX98357A) avec les mêmes modules qu'Opal Box (eyes.cpp, head.cpp).
// Enchaîne seule regards, « oui », « non » et sons ; se pilote aussi depuis le moniteur série :
//   q d w x espace  tête + yeux à gauche / droite / en haut / en bas / au centre
//   o oui   n non   j balancement content   g regard libre   a démo auto marche/arrêt
//   1 2 3  son : bonjour / oui / non      + -  volume
// Câblage : voir include/config.h (TILT sur GPIO 39, PAN sur 38, ampli BCLK 15 / LRC 16 / DIN 7).

#include <Arduino.h>
#include <ESP_I2S.h>
#include <freertos/queue.h>

#include "config.h"
#include "eyes.h"
#include "head.h"

namespace {

// ---------------------------------------------------------------------------
// Sons : petites mélodies synthétisées, jouées par une tâche à part (le reste ne bloque pas)
// ---------------------------------------------------------------------------
constexpr uint32_t RATE = SPK_DEFAULT_RATE;

struct Note {
  float hz;   // 0 = silence
  uint16_t ms;
};
const Note HELLO[] = {{784, 110}, {988, 110}, {1175, 110}, {1568, 260}};
const Note YES[] = {{880, 90}, {1319, 160}};
const Note NO[] = {{392, 140}, {0, 50}, {311, 220}};
struct Melody {
  const Note* notes;
  size_t len;
};
const Melody MELODIES[] = {{HELLO, 4}, {YES, 2}, {NO, 3}};

I2SClass spk;
QueueHandle_t soundQueue;
volatile int volume = SPK_VOLUME;

void playNote(const Note& n) {
  static int16_t buf[256];
  const uint32_t total = RATE * n.ms / 1000;
  const uint32_t fade = RATE / 200;  // 5 ms d'attaque et d'extinction : pas de clic
  float phase = 0;
  const float step = 2 * PI * n.hz / RATE;
  for (uint32_t i = 0; i < total;) {
    size_t k = 0;
    for (; k < 256 && i < total; k++, i++) {
      float env = 1;
      if (i < fade) env = (float)i / fade;
      else if (total - i < fade * 4) env = (float)(total - i) / (fade * 4);
      // Fondamentale + un peu d'octave : timbre plus doux qu'un sinus pur.
      const float s = n.hz > 0 ? 0.8f * sinf(phase) + 0.2f * sinf(2 * phase) : 0;
      phase += step;
      if (phase > 2 * PI) phase -= 2 * PI;
      buf[k] = (int16_t)(s * env * 12000 * volume / 100);
    }
    spk.write((uint8_t*)buf, k * 2);
  }
}

void soundTask(void*) {
  int id;
  for (;;) {
    if (xQueueReceive(soundQueue, &id, portMAX_DELAY) != pdTRUE) continue;
    const Melody& m = MELODIES[id];
    for (size_t i = 0; i < m.len; i++) playNote(m.notes[i]);
    playNote({0, 60});  // vide le tampon I2S : silence net après la mélodie
  }
}

void sound(int id) { xQueueSend(soundQueue, &id, 0); }

// ---------------------------------------------------------------------------
// Regards et gestes : la tête fait le gros du mouvement, les yeux le reste (comme Opal Box)
// ---------------------------------------------------------------------------
void look(float x, float y) {
  head::lookAt(x * HEAD_FOLLOW, y * HEAD_FOLLOW);
  eyes::lookAt(x * (1 - HEAD_FOLLOW) * 1.5f, y * (1 - HEAD_FOLLOW) * 1.5f);
}

void yes() {
  head::nod();
  eyes::setMood(eyes::Mood::Happy);
  sound(1);
}

void no() {
  head::shake();
  eyes::setMood(eyes::Mood::Angry);
  sound(2);
}

void freeGaze() {
  head::setIdle(true);
  eyes::autoGaze();
  eyes::setMood(eyes::Mood::Neutral);
}

// Démo automatique : une étape toutes les 3 s.
bool autoDemo = true;
uint32_t nextStep = 0;
int demoStep = 0;

void runDemoStep() {
  switch (demoStep) {
    case 0: look(-1, 0); Serial.println("-> gauche"); break;
    case 1: look(1, 0); Serial.println("-> droite"); break;
    case 2: look(0, -1); Serial.println("-> en haut"); break;
    case 3: look(0, 1); Serial.println("-> en bas"); break;
    case 4: look(0, 0); yes(); Serial.println("-> oui"); break;
    case 5: no(); Serial.println("-> non"); break;
    case 6: eyes::setMood(eyes::Mood::Happy); head::wiggle(); sound(0); Serial.println("-> content"); break;
    case 7: freeGaze(); Serial.println("-> regard libre"); break;
  }
  demoStep = (demoStep + 1) % 8;
  nextStep = millis() + (demoStep == 0 ? 8000 : 3000);
}

void handleSerial() {
  if (!Serial.available()) return;
  const char ch = Serial.read();
  if (ch == '\n' || ch == '\r') return;
  if (ch != 'a') autoDemo = false;
  switch (ch) {
    case 'q': look(-1, 0); break;
    case 'd': look(1, 0); break;
    case 'w': look(0, -1); break;
    case 'x': look(0, 1); break;
    case ' ': look(0, 0); eyes::setMood(eyes::Mood::Neutral); break;
    case 'o': yes(); break;
    case 'n': no(); break;
    case 'j': eyes::setMood(eyes::Mood::Happy); head::wiggle(); break;
    case 'g': freeGaze(); break;
    case 'a': autoDemo = !autoDemo; Serial.printf("démo auto %s\n", autoDemo ? "marche" : "arrêt"); break;
    case '1': case '2': case '3': sound(ch - '1'); break;
    case '+': volume = min(100, volume + 10); Serial.printf("volume %d\n", volume); sound(1); break;
    case '-': volume = max(0, volume - 10); Serial.printf("volume %d\n", volume); sound(1); break;
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nOpal — démo du corps : yeux, cou (PAN 38, TILT 39), haut-parleur");

  eyes::begin();
  head::begin();  // les deux servos au centre : moment de visser les palonniers bien droits

  soundQueue = xQueueCreate(4, sizeof(int));
  spk.setPins(SPK_PIN_BCLK, SPK_PIN_LRC, SPK_PIN_DIN);
  if (!spk.begin(I2S_MODE_STD, RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("Échec init haut-parleur I2S");
  }
  xTaskCreatePinnedToCore(soundTask, "sound", 4096, nullptr, 5, nullptr, 0);

  eyes::showScene(eyes::Scene::Boot, 2500);
  sound(0);
  nextStep = millis() + 4000;
}

void loop() {
  handleSerial();
  head::update();
  if (autoDemo && (int32_t)(millis() - nextStep) >= 0) runDemoStep();
  delay(5);
}
