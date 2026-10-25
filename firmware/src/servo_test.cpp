// Test du cou (env « servo-test ») : balaie la tête lentement de gauche à droite avec le
// même code qu'Opal Box (head.cpp) et l'écrit sur les deux ports USB de la carte.
// Le servo bouge : le câblage est bon. Il ne bouge pas : signal (GPIO 38), 5V ou GND commun.

#include <Arduino.h>

#include "config.h"
#include "head.h"

static void say(const char* text) {
  Serial.println(text);
  Serial0.println(text);
}

void setup() {
  Serial.begin(115200);
  Serial0.begin(115200);
  delay(500);
  say("Test du servo de tete sur GPIO 38 : centre, puis balayage gauche-droite");
  head::begin();
}

void loop() {
  static uint32_t lastStep = 0;
  static int step = 0;
  static const float TARGETS[] = {0, -0.9f, 0, 0.9f};
  static const char* const NAMES[] = {"centre", "gauche", "centre", "droite"};
  if (millis() - lastStep > 2500) {
    lastStep = millis();
    head::lookAt(TARGETS[step]);
    char line[64];
    snprintf(line, sizeof(line), "-> %s (%.1f)", NAMES[step], TARGETS[step]);
    say(line);
    step = (step + 1) % 4;
  }
  head::update();
  delay(5);
}
