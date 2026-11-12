#pragma once

// Cou d'Opal Box : deux servos. PAN tourne la tête de gauche à droite, TILT l'incline.
// Positions de -1 à 1 : x gauche → droite (vu par la personne en face, comme les yeux),
// y haut → bas (comme les yeux). Mouvements lissés et à vitesse limitée ; les servos sont
// relâchés à l'arrêt (silence).
namespace head {

void begin();     // met la tête au centre
void update();    // à appeler souvent (loop)

void lookAt(float x);           // suivre une direction (suivi du visage, Opal) ; garde l'inclinaison
void lookAt(float x, float y);  // direction et inclinaison
void center();
void setIdle(bool idle);  // personne à suivre : coups d'œil lents de temps en temps

// Position de repos, réglée depuis Opal (page Réglages) et retenue même débranché.
// Degrés : pan + = vers la droite (vue de face), tilt + = plus haut. Bornée à ±REST_MAX_DEG.
constexpr float REST_MAX_DEG = 35.f;
void setRest(float panDeg, float tiltDeg);
float restPan();
float restTilt();

void shake();   // « non »
void nod();     // « oui »
void wiggle();  // petit balancement content

}  // namespace head
