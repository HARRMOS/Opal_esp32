#include "head.h"

#include <Arduino.h>

#include "config.h"

namespace head {
namespace {

constexpr uint32_t PWM_HZ = 50;
constexpr uint8_t PWM_BITS = 14;
constexpr uint32_t PERIOD_US = 1000000 / PWM_HZ;
constexpr uint32_t RELEASE_AFTER_MS = 4000;

// Mouvement de type ressort amorti (critique) : démarre en douceur, freine à l'arrivée.
// Raideur (rad/s) et plafonds de vitesse / accélération, en courses par seconde.
struct Feel {
  float omega, maxSpeed, maxAccel;
};
constexpr Feel FOLLOW = {7.0f, 1.4f, 6.0f};   // suivre un visage
constexpr Feel GLANCE = {3.5f, 0.6f, 1.5f};   // coup d'œil paresseux
constexpr Feel GESTURE = {16.0f, 3.5f, 30.f}; // « non », balancement

constexpr float DEADBAND = 0.04f;    // variation de cible ignorée (tremblement du visage)
constexpr float TARGET_SMOOTH = 6.f; // lissage de la cible entre deux images de la caméra

// Petits mouvements enchaînés autour de la position de base (gestes).
struct Key {
  float offset;
  uint16_t ms;
};
const Key SHAKE[] = {{0.30f, 170}, {-0.30f, 240}, {0.24f, 220}, {-0.18f, 200}, {0, 220}};
const Key WIGGLE[] = {{0.12f, 200}, {-0.12f, 240}, {0, 240}};

float pos = 0, vel = 0;   // position et vitesse réelles de la tête
float wanted = 0;         // dernière direction demandée (après zone morte)
float base = 0;           // direction lissée vers laquelle on va
bool idle = true, attached = false;
uint32_t lastMs = 0, stillSince = 0, nextGlance = 0;
const Key* gesture = nullptr;
size_t gestureLen = 0, gestureStep = 0;
uint32_t stepEnd = 0;

void writePos(float p) {
  if (HEAD_PAN_INVERT) p = -p;
  const float us = HEAD_PAN_CENTER_US + constrain(p, -1.f, 1.f) * HEAD_PAN_RANGE_US;
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

void setWanted(float x) {
  x = constrain(x, -1.f, 1.f);
  if (fabsf(x - wanted) >= DEADBAND) wanted = x;
}

}  // namespace

void begin() {
  pos = vel = wanted = base = 0;
  writePos(0);
  lastMs = stillSince = millis();
  nextGlance = millis() + 8000;
}

void lookAt(float x) {
  setWanted(x);
  idle = false;
}

void center() {  // tête droite et immobile (sommeil, demande d'Opal)
  wanted = 0;
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
  float dt = (now - lastMs) / 1000.f;
  if (dt <= 0) return;
  dt = min(dt, 0.05f);
  lastMs = now;

  // Personne à suivre : de temps en temps, un lent coup d'œil d'un côté ou retour au centre.
  if (idle && !gesture && now >= nextGlance) {
    wanted = randRange(0, 1) < 0.4f ? 0 : randRange(-0.6f, 0.6f);
    nextGlance = now + (uint32_t)randRange(6000, 16000);
  }

  // La cible avance en douceur vers la dernière direction demandée : les mises à jour
  // de la caméra (quelques par seconde) deviennent une trajectoire continue.
  base += (wanted - base) * min(1.f, dt * TARGET_SMOOTH);

  Feel feel = idle ? GLANCE : FOLLOW;
  float target = base;
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
      feel = GESTURE;
    }
  }

  // Ressort amorti critique, avec accélération et vitesse plafonnées.
  const float accel = constrain(feel.omega * feel.omega * (target - pos) - 2 * feel.omega * vel,
                                -feel.maxAccel, feel.maxAccel);
  vel = constrain(vel + accel * dt, -feel.maxSpeed, feel.maxSpeed);
  pos = constrain(pos + vel * dt, -1.f, 1.f);

  if (fabsf(target - pos) > 0.003f || fabsf(vel) > 0.01f) {
    writePos(pos);
    stillSince = now;
  } else {
    vel = 0;
    if (now - stillSince > RELEASE_AFTER_MS) release();
  }
}

}  // namespace head
