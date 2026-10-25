#pragma once

#include <stdint.h>

// Yeux réalistes d'Opal sur deux écrans ronds GC9A01.
// Rendu procédural : sclère veinée, iris à fibres, pupille qui se dilate,
// paupières avec ombre, clignements et saccades automatiques.
namespace eyes {

enum class Mood : uint8_t { Neutral, Happy, Sad, Angry, Surprised, Sleepy };
enum class IrisColor : uint8_t { Opal, Hazel, Blue, Brown, Green };

// Prépare les textures puis lance l'animation : un œil par cœur, en tâche de fond.
void begin(IrisColor color = IrisColor::Opal);

void setMood(Mood mood);
void lookAt(float x, float y);  // -1..1 (droite/bas positifs) ; coupe le regard auto
void autoGaze();                // regard autonome (saccades aléatoires)
void blink();
void wink(int eye);  // clin d'œil : 0 = œil gauche, 1 = œil droit (vus de face)
void setPupil(float size);      // 0 (serrée) .. 1 (dilatée) ; < 0 = selon l'humeur
void setIrisColor(IrisColor color);

// Mode « appareil photo » : l'iris devient un objectif qui fait la mise au point,
// déclic + flash à la prise de vue, puis retour à l'œil normal.
void cameraAim();
void cameraShoot();
void cameraDone();
void setRecording(bool on);  // voyant rouge sur l'œil droit pendant le direct

float fps();

}  // namespace eyes
