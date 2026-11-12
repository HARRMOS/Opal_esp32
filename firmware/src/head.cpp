#include "head.h"

#include <Arduino.h>
#include <Preferences.h>

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
constexpr Feel GESTURE = {16.0f, 3.5f, 30.f}; // « non », « oui », balancement

constexpr float DEADBAND = 0.04f;    // variation de cible ignorée (tremblement du visage)
constexpr float TARGET_SMOOTH = 6.f; // lissage de la cible entre deux images de la caméra
constexpr float US_PER_DEG = 11.1f;  // MG90S : ~1000 µs pour 90°

// Un servo : sa broche, son réglage et son état de mouvement.
struct Axis {
  uint8_t pin;
  int centerUs, rangeUs;
  bool invert;
  bool holdAlways;          // TILT porte le poids de la tête : jamais relâché
  float pos = 0, vel = 0;   // position et vitesse réelles
  float wanted = 0;         // dernière position demandée (après zone morte)
  float base = 0;           // position lissée vers laquelle on va
  bool attached = false;
  uint32_t stillSince = 0;
  float restDeg = 0;        // position de repos (degrés, sens « humain » : droite / haut)
  int restSign = 1;         // pan : + droite = + position ; tilt : + haut = − position (y vers le bas)

  void write(float p) {
    float us = constrain(p, -1.f, 1.f) * rangeUs + restSign * restDeg * US_PER_DEG;
    if (invert) us = -us;
    us += centerUs;
    if (!attached) {
      ledcAttach(pin, PWM_HZ, PWM_BITS);
      attached = true;
    }
    ledcWrite(pin, (uint32_t)(us * ((1 << PWM_BITS) - 1) / PERIOD_US));
  }

  void release() {  // plus d'impulsions : le servo ne vibre plus et ne consomme plus
    if (!attached || holdAlways) return;
    ledcWrite(pin, 0);
    ledcDetach(pin);
    attached = false;
  }

  void setWanted(float x) {
    x = constrain(x, -1.f, 1.f);
    if (fabsf(x - wanted) >= DEADBAND) wanted = x;
  }

  void step(float target, const Feel& feel, float dt, uint32_t now) {
    // Ressort amorti critique, avec accélération et vitesse plafonnées.
    const float accel = constrain(feel.omega * feel.omega * (target - pos) - 2 * feel.omega * vel,
                                  -feel.maxAccel, feel.maxAccel);
    vel = constrain(vel + accel * dt, -feel.maxSpeed, feel.maxSpeed);
    pos = constrain(pos + vel * dt, -1.f, 1.f);
    if (fabsf(target - pos) > 0.003f || fabsf(vel) > 0.01f) {
      write(pos);
      stillSince = now;
    } else {
      vel = 0;
      if (now - stillSince > RELEASE_AFTER_MS) release();
    }
  }
};

Axis pan{HEAD_PAN_PIN, HEAD_PAN_CENTER_US, HEAD_PAN_RANGE_US, HEAD_PAN_INVERT, false};
Axis tilt{HEAD_TILT_PIN, HEAD_TILT_CENTER_US, HEAD_TILT_RANGE_US, HEAD_TILT_INVERT, true};

void loadRest() {
  Preferences prefs;
  prefs.begin("head", false);
  pan.restDeg = prefs.getFloat("pan", 0);
  tilt.restDeg = prefs.getFloat("tilt", 0);
  // Version 2 : le sens du servo du cou a été corrigé (HEAD_TILT_INVERT) ; une hauteur réglée avant
  // l'était à l'envers, on la retourne une fois pour que la tête ne bouge pas.
  if (prefs.getUChar("v", 1) < 2) {
    tilt.restDeg = -tilt.restDeg;
    prefs.putFloat("tilt", tilt.restDeg);
    prefs.putUChar("v", 2);
  }
  prefs.end();
  tilt.restSign = -1;
}

// Petits mouvements enchaînés autour de la position de base (gestes), sur un axe.
struct Key {
  float offset;
  uint16_t ms;
};
const Key SHAKE[] = {{0.30f, 170}, {-0.30f, 240}, {0.24f, 220}, {-0.18f, 200}, {0, 220}};
const Key NOD[] = {{0.55f, 200}, {-0.25f, 230}, {0.45f, 210}, {0, 260}};
const Key WIGGLE[] = {{0.12f, 200}, {-0.12f, 240}, {0, 240}};

bool idle = true;
uint32_t lastMs = 0, nextGlance = 0;
const Key* gesture = nullptr;
Axis* gestureAxis = nullptr;
size_t gestureLen = 0, gestureStep = 0;
uint32_t stepEnd = 0;

void startGesture(Axis& axis, const Key* keys, size_t len) {
  gestureAxis = &axis;
  gesture = keys;
  gestureLen = len;
  gestureStep = 0;
  stepEnd = 0;
}

float randRange(float a, float b) { return a + (b - a) * (esp_random() / 4294967295.f); }

}  // namespace

void begin() {
  loadRest();
  const uint32_t now = millis();
  for (Axis* a : {&pan, &tilt}) {
    a->pos = a->vel = a->wanted = a->base = 0;
    a->write(0);
    a->stillSince = now;
  }
  lastMs = now;
  nextGlance = now + 8000;
}

void setRest(float panDeg, float tiltDeg) {
  pan.restDeg = constrain(panDeg, -REST_MAX_DEG, REST_MAX_DEG);
  tilt.restDeg = constrain(tiltDeg, -REST_MAX_DEG, REST_MAX_DEG);
  Preferences prefs;
  prefs.begin("head", false);
  prefs.putFloat("pan", pan.restDeg);
  prefs.putFloat("tilt", tilt.restDeg);
  prefs.end();
  const uint32_t now = millis();
  for (Axis* a : {&pan, &tilt}) {  // la tête rejoint tout de suite la nouvelle position
    a->write(a->pos);
    a->stillSince = now;
  }
}

float restPan() { return pan.restDeg; }
float restTilt() { return tilt.restDeg; }

void lookAt(float x) {
  pan.setWanted(x);
  idle = false;
}

void lookAt(float x, float y) {
  pan.setWanted(x);
  tilt.setWanted(y);
  idle = false;
}

void center() {  // tête droite et immobile (sommeil, demande d'Opal)
  pan.wanted = tilt.wanted = 0;
  idle = false;
}

void setIdle(bool on) {
  idle = on;
  if (on) nextGlance = millis() + (uint32_t)randRange(4000, 9000);
}

void shake() { startGesture(pan, SHAKE, sizeof(SHAKE) / sizeof(SHAKE[0])); }
void nod() { startGesture(tilt, NOD, sizeof(NOD) / sizeof(NOD[0])); }
void wiggle() { startGesture(pan, WIGGLE, sizeof(WIGGLE) / sizeof(WIGGLE[0])); }

void update() {
  const uint32_t now = millis();
  float dt = (now - lastMs) / 1000.f;
  if (dt <= 0) return;
  dt = min(dt, 0.05f);
  lastMs = now;

  // Personne à suivre : de temps en temps, un lent coup d'œil d'un côté ou retour au centre.
  if (idle && !gesture && now >= nextGlance) {
    const bool home = randRange(0, 1) < 0.4f;
    pan.wanted = home ? 0 : randRange(-0.6f, 0.6f);
    tilt.wanted = home ? 0 : randRange(-0.4f, 0.3f);
    nextGlance = now + (uint32_t)randRange(6000, 16000);
  }

  for (Axis* a : {&pan, &tilt}) {
    // La cible avance en douceur vers la dernière position demandée : les mises à jour
    // de la caméra (quelques par seconde) deviennent une trajectoire continue.
    a->base += (a->wanted - a->base) * min(1.f, dt * TARGET_SMOOTH);
  }

  float gestureOffset = 0;
  if (gesture) {
    if (now >= stepEnd) {
      if (gestureStep >= gestureLen) {
        gesture = nullptr;
      } else {
        stepEnd = now + gesture[gestureStep].ms;
        gestureStep++;
      }
    }
    if (gesture) gestureOffset = gesture[gestureStep - 1].offset;
  }

  const Feel& calm = idle ? GLANCE : FOLLOW;
  for (Axis* a : {&pan, &tilt}) {
    const bool moving = gesture && gestureAxis == a;
    a->step(a->base + (moving ? gestureOffset : 0), moving ? GESTURE : calm, dt, now);
  }
}

}  // namespace head
