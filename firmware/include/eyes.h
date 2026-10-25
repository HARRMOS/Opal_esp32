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
void setAsleep(bool asleep); // paupières closes + écran assombri (la nuit)

// Style d'œil : l'opale réaliste, ou le « gardien » (objectif rouge qui pulse).
enum class Style : uint8_t { Opal, Guard };
void setStyle(Style style);

// Animations plein écran (autres que des yeux), dessinées à la place des yeux.
enum class Scene : uint8_t {
  None, Hearts, Clock, Weather, Music, Thinking, Timer, Sleep, Message, Dizzy, Matrix, Boot
};
// Données des animations qui en ont besoin (météo, minuteur, messages).
struct SceneInfo {
  int temp = 0, tmin = 0, tmax = 0;
  char icon[8] = "sun";    // sun, cloud, rain, snow, storm, fog
  char desc[28] = "";
  char place[20] = "";
  int count = 0;           // nombre de messages
  char label[24] = "";     // texte libre (minuteur, messages)
  uint32_t timerMs = 0;    // durée du minuteur
};
// durationMs = 0 : jusqu'à endScene() (ou une autre animation).
void showScene(Scene scene, uint32_t durationMs, const SceneInfo* info = nullptr);
void endScene();
Scene scene();

float fps();

}  // namespace eyes
