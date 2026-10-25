#pragma once

#include <LovyanGFX.hpp>

#include "eyes.h"

namespace eyes {
namespace scenes {

// Dessine l'image courante de l'animation `scene` pour l'œil e (0 gauche, 1 droit).
// start : instant de début de l'animation (millis), now : instant présent.
void draw(int e, lgfx::LGFX_Sprite& s, Scene scene, const SceneInfo& info, uint32_t now, uint32_t start);

}  // namespace scenes
}  // namespace eyes
