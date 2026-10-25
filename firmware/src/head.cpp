#include "head.h"

#include <Arduino.h>

#include "config.h"

namespace head {
namespace {

constexpr uint32_t PWM_HZ = 50;
constexpr uint8_t PWM_BITS = 14;
constexpr uint32_t PERIOD_US = 1000000 / PWM_HZ;
constexpr float MAX_SPEED = 1.6f;          // course par seconde (≈ 90°/s)
constexpr uint32_t RELEASE_AFTER_MS = 1500;

// Petits mouvements enchaînés autour de la position de base (gestes).
struct Key {
  float offset;
  uint16_t ms;
};
const Key SHAKE[] = {{0.30f, 160}, {-0.30f, 220}, {0.24f, 200}, {-0.18f, 180}, {0, 160}};
const Key WIGGLE[] = {{0.12f, 180}, {-0.12f, 220}, {0, 200}};

float pos = 0, target = 0, base = 0;
bool idle = true, attached = false;
uint32_t lastMs = 0, stillSince = 0, nextGlance = 0;
const Key* gesture = nullptr;
size_t gestureLen = 0, gestureStep = 0;
uint32_t stepEnd = 0;

void writePos(float p) {
  if (HEAD_PAN_INVERT) p = -p;
  float us = HEAD_PAN_CENTER_US + constrain(p, -1.f, 1.f) * HEAD_PAN_RANGE_US;
  if (!attached) {
    ledcAttach(HEAD_PAN_PIN, PWM_HZ, PWM_BITS);
    attached = true;
  }
  ledcWrite(HEAD_PAN_PIN, (uint32_t)(us * ((1 << PWM_BITS) - 1) / PERIOD_US));
}

void release() {  // plus d'impulsions : le servo ne vibre plus et ne consomme plus
  if (!attached) return;
  ledcWrite(HEAD_PAN_PIN, 0);
  ledcDetach(HEAD_PAN_PIN);
  attached = false;
}

void startGesture(const Key* keys, size_t len) {
  gesture = keys;
  gestureLen = len;
  gestureStep = 0;
  stepEnd = 0;
}

float randRange(float a, float b) { return a + (b - a) * (esp_random() / 4294967295.f); }

}  // namespace

void begin() {
  pos = target = base = 0;
  writePos(0);
  lastMs = stillSince = millis();
  nextGlance = millis() + 8000;
}

void lookAt(float x) {
  base = constrain(x, -1.f, 1.f);
  idle = false;
}

void center() {  // tête droite et immobile (sommeil, demande d'Opal)
  base = 0;
  idle = false;
}

void setIdle(bool on) {
  idle = on;
  if (on) nextGlance = millis() + (uint32_t)randRange(4000, 9000);
}

void shake() { startGesture(SHAKE, sizeof(SHAKE) / sizeof(SHAKE[0])); }
void wiggle() { startGesture(WIGGLE, sizeof(WIGGLE) / sizeof(WIGGLE[0])); }

void update() {
  const uint32_t now = millis();
  const float dt = min(0.05f, (now - lastMs) / 1000.f);
  lastMs = now;

  // Personne à suivre : de temps en temps, un lent coup d'œil d'un côté ou retour au centre.
  if (idle && !gesture && now >= nextGlance) {
    base = randRange(0, 1) < 0.4f ? 0 : randRange(-0.6f, 0.6f);
    nextGlance = now + (uint32_t)randRange(6000, 16000);
  }

  float speed = MAX_SPEED;
  target = base;
  if (gesture) {
    if (now >= stepEnd) {
      if (gestureStep >= gestureLen) {
        gesture = nullptr;
      } else {
        stepEnd = now + gesture[gestureStep].ms;
        gestureStep++;
      }
    }
    if (gesture) {
      target = base + gesture[gestureStep - 1].offset;
      speed = MAX_SPEED * 2.2f;  // les gestes sont vifs
    }
  }
  if (idle && !gesture) speed = MAX_SPEED * 0.35f;  // coups d'œil paresseux

  const float delta = target - pos;
  if (fabsf(delta) > 0.004f) {
    // Rapproche avec une vitesse plafonnée et un freinage en fin de course.
    const float step = constrain(delta * min(1.f, dt * 8), -speed * dt, speed * dt);
    pos += step;
    writePos(pos);
    stillSince = now;
  } else if (now - stillSince > RELEASE_AFTER_MS) {
    release();
  }
}

}  // namespace head
