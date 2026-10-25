#pragma once

// Cou d'Opal Box : un servo qui tourne la tête de gauche à droite.
// Positions de -1 (gauche) à 1 (droite), vues par la personne en face, comme les yeux.
// Mouvements lissés et à vitesse limitée ; le servo est relâché à l'arrêt (silence).
namespace head {

void begin();     // met la tête au centre
void update();    // à appeler souvent (loop)

void lookAt(float x);  // suivre une direction (suivi du visage, Opal)
void center();
void setIdle(bool idle);  // personne à suivre : coups d'œil lents de temps en temps

void shake();   // « non »
void wiggle();  // petit balancement content

}  // namespace head
