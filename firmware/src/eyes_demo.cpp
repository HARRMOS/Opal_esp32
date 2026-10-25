// Démo des yeux seuls (env « eyes ») : enchaîne les humeurs, et se pilote
// depuis le moniteur série :
//   n neutre   h content   s triste   a fâché   u surpris   z endormi
//   b cligner  k/l clin d'œil gauche/droit  g regard auto   p/P pupille serrée/dilatée   c couleur d'iris
//   f prise de photo (objectif, déclic, flash)   r voyant « direct » marche/arrêt
//   w q x d + espace : regarder haut/gauche/bas/droite, centre

#include <Arduino.h>

#include "eyes.h"

static const char* const MOOD_NAMES[] = {"neutre", "content", "triste",
                                         "fâché", "surpris", "endormi"};
static bool cycle = true;
static uint32_t nextMood = 0;
static int mood = 0;
static uint32_t photoAt = 0;  // démo « f » : visée 1,5 s puis déclic

static void handleSerial() {
  if (!Serial.available()) return;
  char ch = Serial.read();
  const char* moods = "nhsauz";
  if (const char* m = strchr(moods, ch); m && ch) {
    cycle = false;
    mood = m - moods;
    eyes::setMood((eyes::Mood)mood);
    Serial.printf("humeur : %s\n", MOOD_NAMES[mood]);
    return;
  }
  switch (ch) {
    case 'b': eyes::blink(); break;
    case 'k': eyes::wink(0); break;
    case 'l': eyes::wink(1); break;
    case 'g': eyes::autoGaze(); cycle = true; break;
    case 'p': eyes::setPupil(0.1f); break;
    case 'P': eyes::setPupil(0.95f); break;
    case 'w': eyes::lookAt(0, -0.8f); break;
    case 'x': eyes::lookAt(0, 0.8f); break;
    case 'q': eyes::lookAt(-0.8f, 0); break;
    case 'd': eyes::lookAt(0.8f, 0); break;
    case ' ': eyes::lookAt(0, 0); break;
    case 'f':
      photoAt = millis();
      eyes::cameraAim();
      break;
    case 'r': {
      static bool rec = false;
      rec = !rec;
      eyes::setRecording(rec);
      break;
    }
    case 'c': {
      static int color = 0;
      color = (color + 1) % 5;
      eyes::setIrisColor((eyes::IrisColor)color);
      break;
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nOpal — démo des yeux");
  eyes::begin();
}

void loop() {
  handleSerial();
  delay(5);
  if (photoAt && millis() - photoAt > 1500) {  // même enchaînement que le pont
    photoAt = 0;
    eyes::cameraShoot();
  }

  uint32_t now = millis();
  if (cycle && now >= nextMood) {
    eyes::setMood((eyes::Mood)mood);
    Serial.printf("humeur : %s  (%.1f fps)\n", MOOD_NAMES[mood], eyes::fps());
    mood = (mood + 1) % 6;
    nextMood = now + 4000;
  }
}
