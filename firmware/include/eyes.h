#pragma once

#include <stdint.h>

// Yeux réalistes d'Opal sur deux écrans ronds GC9A01.
// Rendu procédural : sclère veinée, iris à fibres, pupille qui se dilate,
// paupières avec ombre, clignements et saccades automatiques.
namespace eyes {

enum class Mood : uint8_t { Neutral, Happy, Sad, Angry, Surprised, Sleepy };
enum class IrisColor : uint8_t { Hazel, Blue, Brown, Green };

void begin(IrisColor color = IrisColor::Hazel);
void update();  // anime et dessine une image ; à appeler en boucle

void setMood(Mood mood);
void lookAt(float x, float y);  // -1..1 (droite/bas positifs) ; coupe le regard auto
void autoGaze();                // regard autonome (saccades aléatoires)
void blink();
void setPupil(float size);      // 0 (serrée) .. 1 (dilatée) ; < 0 = selon l'humeur
void setIrisColor(IrisColor color);

float fps();

}  // namespace eyes
