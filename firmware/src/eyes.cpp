#include "eyes.h"

#include <Arduino.h>
#include <LovyanGFX.hpp>
#include <esp_heap_caps.h>
#include <freertos/semphr.h>
#include <math.h>

#include "config.h"
#include "scenes.h"

namespace eyes {
namespace {

constexpr int W = 240, H = 240, CX = 120, CY = 120;
constexpr int VISIBLE_R2 = 121 * 121;  // hors de ce cercle l'écran rond n'affiche rien
constexpr int IRIS_R = 62, IRIS_D = IRIS_R * 2;
constexpr int ANG = 256, RINGS = 64;    // texture d'iris en coordonnées polaires
constexpr int STRIP_H = 24;             // bandes rendues puis envoyées en DMA
constexpr int LID_CLOSE_Y = 132;        // ligne où les paupières se rejoignent
constexpr float UPPER_OPEN_H = 120.f, LOWER_OPEN_H = 110.f;
constexpr float GAZE_X_PX = 38.f, GAZE_Y_PX = 30.f;

// ---------------------------------------------------------------------------
// Écrans
// ---------------------------------------------------------------------------
class Panel : public lgfx::LGFX_Device {
  lgfx::Panel_GC9A01 panel_;
  lgfx::Bus_SPI bus_;

 public:
  Panel(spi_host_device_t host, int sclk, int mosi, int dc, int cs, int rst) {
    auto b = bus_.config();
    b.spi_host = host;
    b.spi_mode = 0;
    b.freq_write = EYE_SPI_HZ;
    b.spi_3wire = false;
    b.use_lock = true;
    b.dma_channel = SPI_DMA_CH_AUTO;
    b.pin_sclk = sclk;
    b.pin_mosi = mosi;
    b.pin_miso = -1;
    b.pin_dc = dc;
    bus_.config(b);
    panel_.setBus(&bus_);

    auto p = panel_.config();
    p.pin_cs = cs;
    p.pin_rst = rst;
    p.pin_busy = -1;
    p.memory_width = p.panel_width = W;
    p.memory_height = p.panel_height = H;
    p.readable = false;
    p.invert = EYE_INVERT;
    p.rgb_order = false;
    p.bus_shared = false;
    panel_.config(p);
    setPanel(&panel_);
  }
};

// Le RST est commun : seul l'œil gauche le pilote (il réinitialise les deux).
Panel leftPanel(SPI2_HOST, EYE_L_SCLK, EYE_L_MOSI, EYE_L_DC, EYE_L_CS, EYE_RST);
Panel rightPanel(SPI3_HOST, EYE_R_SCLK, EYE_R_MOSI, EYE_R_DC, EYE_R_CS, -1);
Panel* const lcd[2] = {&leftPanel, &rightPanel};
// Côté du nez pour chaque œil (+1 = vers x croissant) : sert aux sourcils/convergence.
constexpr int INNER[2] = {+1, -1};

// ---------------------------------------------------------------------------
// Couleurs RGB565
// ---------------------------------------------------------------------------
constexpr uint16_t rgb(int r, int g, int b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

uint16_t rgbf(float r, float g, float b) {
  auto c = [](float v) { return (int)constrain(v, 0.f, 255.f); };
  return rgb(c(r), c(g), c(b));
}

// Échelle de luminosité s/32 sur les trois canaux en une multiplication.
inline uint16_t scale565(uint16_t c, uint32_t s) {
  uint32_t x = (c | ((uint32_t)c << 16)) & 0x07E0F81F;
  x = ((x * s) >> 5) & 0x07E0F81F;
  return (uint16_t)(x | (x >> 16));
}

// Mélange a -> b avec alpha/32.
inline uint16_t blend565(uint16_t a, uint16_t b, uint32_t alpha) {
  uint32_t xa = (a | ((uint32_t)a << 16)) & 0x07E0F81F;
  uint32_t xb = (b | ((uint32_t)b << 16)) & 0x07E0F81F;
  uint32_t x = ((xa * (32 - alpha) + xb * alpha) >> 5) & 0x07E0F81F;
  return (uint16_t)(x | (x >> 16));
}

struct Palette {
  float inner[3], mid[3], outer[3], limbus[3];
};

const Palette PALETTES[] = {
    {},                                                               // Opal : buildClassicTex()
    {{205, 150, 65}, {115, 128, 70}, {72, 96, 60}, {22, 28, 22}},     // Hazel
    {{175, 195, 205}, {90, 140, 190}, {52, 92, 148}, {18, 28, 48}},   // Blue
    {{150, 95, 45}, {108, 66, 32}, {76, 46, 24}, {24, 14, 10}},       // Brown
    {{185, 175, 85}, {82, 145, 92}, {46, 106, 72}, {14, 34, 24}},     // Green
};

// ---------------------------------------------------------------------------
// Textures précalculées au démarrage
// ---------------------------------------------------------------------------
uint16_t* irisTex;  // [angle][anneau]
uint16_t* polar;    // pour chaque pixel autour de l'iris : (distance*2) << 8 | angle
uint8_t spanHalf[H];  // demi-largeur visible de chaque ligne de l'écran rond
float lidCurve[W];
uint16_t* strip[2][2];

volatile Style style = Style::Opal;
IrisColor irisColor = IrisColor::Opal;

// Animations plein écran : un sprite 240×240 par œil (PSRAM).
lgfx::LGFX_Sprite sprite[2];
volatile bool sceneRequested = false;
Scene sceneWanted = Scene::None;
SceneInfo infoWanted;
uint32_t sceneWantedMs = 0;
Scene sceneNow = Scene::None;  // lu par les deux tâches pendant l'image
SceneInfo sceneInfo;
uint32_t sceneStart = 0, sceneUntil = 0;

uint32_t seed = 0x6F70616C;  // "opal" : textures identiques à chaque démarrage
float rnd() {
  seed ^= seed << 13;
  seed ^= seed >> 17;
  seed ^= seed << 5;
  return (seed & 0xFFFFFF) / 16777215.f;
}

float smoothstep(float a, float b, float x) {
  float t = constrain((x - a) / (b - a), 0.f, 1.f);
  return t * t * (3 - 2 * t);
}

void lerp3(const float* a, const float* b, float t, float* out) {
  for (int i = 0; i < 3; i++) out[i] = a[i] + (b[i] - a[i]) * t;
}

void buildPolar() {
  for (int y = 0; y < IRIS_D; y++) {
    for (int x = 0; x < IRIS_D; x++) {
      float fx = x - IRIS_R + 0.5f, fy = y - IRIS_R + 0.5f;
      int d = min(255, (int)(sqrtf(fx * fx + fy * fy) * 2));
      int a = (int)((atan2f(fy, fx) / (2 * PI) + 0.5f) * ANG) & (ANG - 1);
      polar[y * IRIS_D + x] = (d << 8) | a;
    }
  }
}

void buildIris(const Palette& pal) {
  float fiber[ANG], sector[ANG], collar[ANG];
  float raw[ANG];
  for (int a = 0; a < ANG; a++) raw[a] = rnd();
  float p1 = rnd() * 6, p2 = rnd() * 6, p3 = rnd() * 6;
  for (int a = 0; a < ANG; a++) {
    float th = a * 2 * PI / ANG;
    fiber[a] = 0.5f * raw[a] + 0.25f * (raw[(a + ANG - 1) % ANG] + raw[(a + 1) % ANG]);
    sector[a] = 0.88f + 0.12f * sinf(th * 3 + p1) + 0.07f * sinf(th * 7 + p2);
    collar[a] = 0.33f + 0.035f * sinf(th * 9 + p3) + 0.02f * (rnd() - 0.5f);
  }

  struct Crypt { float a, t, ra, rt; };
  Crypt crypts[24];
  for (auto& c : crypts) c = {rnd() * ANG, 0.32f + rnd() * 0.48f, 2 + rnd() * 5, 0.03f + rnd() * 0.06f};

  const float ruff[3] = {38, 24, 20};
  for (int r = 0; r < RINGS; r++) {
    float t = (r + 0.5f) / RINGS;
    for (int a = 0; a < ANG; a++) {
      // Les fibres ondulent légèrement en s'éloignant de la pupille.
      int aw = (a + (int)(3 * sinf(t * 12 + a * 0.05f)) + ANG) & (ANG - 1);
      float col[3];
      float k;
      if (t < collar[a]) {
        k = t / collar[a];
        lerp3(ruff, pal.inner, smoothstep(0.f, 0.35f, k), col);
      } else {
        k = (t - collar[a]) / (0.86f - collar[a]);
        if (k < 0.5f) lerp3(pal.inner, pal.mid, k * 2, col);
        else lerp3(pal.mid, pal.outer, min(1.f, (k - 0.5f) * 2), col);
      }
      if (t > 0.86f) lerp3(col, pal.limbus, smoothstep(0.86f, 1.f, t), col);

      float b = sector[a] * (0.62f + 0.7f * fiber[aw]);
      if (fabsf(t - collar[a]) < 0.025f) b *= 1.25f;  // collerette
      for (const auto& c : crypts) {
        float da = fabsf(a - c.a);
        if (da > ANG / 2) da = ANG - da;
        float e = (da / c.ra) * (da / c.ra) + ((t - c.t) / c.rt) * ((t - c.t) / c.rt);
        if (e < 1) b *= 0.55f + 0.45f * e;
      }
      b += (1 - b) * smoothstep(0.86f, 1.f, t) * 0.7f;  // limbe plus uni
      irisTex[a * RINGS + r] = rgbf(col[0] * b, col[1] * b, col[2] * b);
    }
  }
}

void buildClassicTex();

void buildIrisFor(IrisColor color) {
  irisColor = color;
  if (color == IrisColor::Opal) buildClassicTex();
  else buildIris(PALETTES[(int)color]);
}

void buildShade() {
  for (int y = 0; y < H; y++) {
    int dy = y - CY;
    spanHalf[y] = (uint8_t)min(CX, (int)sqrtf(max(0, VISIBLE_R2 - dy * dy)));
  }
  for (int x = 0; x < W; x++) {
    float u = (x - CX) / 128.f;
    lidCurve[x] = sqrtf(max(0.f, 1 - u * u));
  }
}

// ---------------------------------------------------------------------------
// Animation
// ---------------------------------------------------------------------------
struct Pose {
  float openU, openL, tilt, pupil;
};

// tilt > 0 : coin intérieur de la paupière abaissé (colère) ; < 0 : tristesse.
const Pose MOODS[] = {
    {0.85f, 0.90f, 0.0f, 0.50f},   // Neutral
    {0.80f, 0.40f, 0.0f, 0.62f},   // Happy
    {0.70f, 0.95f, -0.7f, 0.45f},  // Sad
    {0.68f, 0.82f, 0.9f, 0.30f},   // Angry
    {1.20f, 1.10f, 0.0f, 0.20f},   // Surprised
    {0.32f, 0.92f, 0.0f, 0.65f},   // Sleepy
};

// Clignement d'un œil : fermeture rapide, pause, ouverture plus lente qui ralentit.
struct Blink {
  bool active = false, wink = false;
  uint32_t start = 0;
  uint16_t closeMs = 80, holdMs = 40, openMs = 170;
};

struct {
  Pose target = MOODS[0], pose = MOODS[0];
  float gx = 0, gy = 0, tgx = 0, tgy = 0;
  bool autoGaze = true;
  uint32_t nextSaccade = 0;
  float pupilOverride = -1, pupil = 0.5f;
  Blink blink[2];
  uint32_t nextBlink = 2000;
  bool doubleBlink = false;
  volatile bool blinkRequest = false;
  volatile int8_t winkRequest = -1;
  uint32_t lastMs = 0;
} anim;

struct EyeFrame {
  int icx, icy;
  int fx, fy;                  // aplatissement de l'iris (8.8) quand l'œil tourne
  int irisHalfW, irisHalfH;    // étendue de l'iris à l'écran
  int pupil2, ringScale;
  float dotX, dotY;            // Gardien : point de lumière qui fait le tour de l'anneau
  int16_t upper[W], lower[W];  // bords des paupières en 1/16 de pixel
};
EyeFrame frame[2];

// ---------------------------------------------------------------------------
// Yeux « épurés » (validés sur l'aperçu web) : fond noir, paupières noires sans liseré.
//  - Opale (classique) : grand iris en verre opale, reflets irisés, pupille nette ;
//    la palette suit l'humeur (texture d'iris reconstruite pendant la transition).
//  - Gardien : sphère de verre lumineuse, anneau fin et un point de lumière.
// ---------------------------------------------------------------------------
constexpr uint16_t PRO_BG = rgb(6, 6, 12), PRO_LID = rgb(4, 4, 8), PRO_PUPIL = rgb(3, 3, 7);
constexpr uint16_t GUARD_BG = rgb(7, 4, 10), WHITE = rgb(255, 255, 255);
constexpr int HALO_N = (2 * 240 * 240 >> 5) + 1;  // halo indexé par r² / 32
constexpr float GUARD_RING_R = IRIS_R * 1.48f;

struct ClassicPalette {
  float inner[3], mid[3], outer[3];
};
// Même ordre que Mood : neutre, content, triste, fâché, surpris, endormi.
const ClassicPalette CLASSIC_PAL[] = {
    {{126, 226, 240}, {58, 122, 236}, {104, 78, 214}},
    {{150, 240, 214}, {70, 150, 236}, {120, 96, 220}},
    {{96, 160, 230}, {36, 70, 190}, {52, 44, 150}},
    {{255, 140, 200}, {190, 60, 200}, {110, 30, 150}},
    {{170, 240, 250}, {80, 150, 246}, {120, 100, 230}},
    {{100, 170, 190}, {50, 90, 170}, {70, 60, 150}},
};
volatile int moodIndex = 0;
ClassicPalette classic = CLASSIC_PAL[0];  // palette affichée (glisse vers celle de l'humeur)
uint8_t* playF;          // [angle][anneau] : jeu de couleurs de l'opale (0..255), précalculé
int8_t fiberF[ANG];      // fines stries selon l'angle (-127..127)
uint8_t volC[IRIS_D];    // Opale : volume (ombre douce) selon la distance, 0..255
uint8_t haloC[HALO_N], haloG[HALO_N];  // halo autour de l'œil selon r², 0..32
uint8_t dotA[81];        // Gardien : point de lumière selon d², 0..32
uint8_t glassA[64];      // Gardien : reflet de verre selon la distance elliptique (×64), 0..32
constexpr int WIN_X = -21, WIN_Y = -24;  // Opale : reflet « fenêtre » (haut gauche)
constexpr int SPOT_X = 19, SPOT_Y = 21;  // Opale : petit point (bas droite)
constexpr int RING_R2 = (int)(GUARD_RING_R * GUARD_RING_R);
constexpr int RING_W = (int)(2 * GUARD_RING_R * 1.1f);  // ±1,1 px exprimés en r²
constexpr int TICK_IN = (int)GUARD_RING_R + 3, TICK_OUT = (int)GUARD_RING_R + 9;
constexpr uint16_t GUARD_LID = rgb(4, 2, 5);
uint16_t guardLut[IRIS_D];  // couleur de la sphère selon la distance (reconstruite à chaque image)
uint16_t haloColor = rgb(58, 122, 236);
uint16_t guardPeach = rgb(255, 201, 181);
float alert = 0;            // Gardien : 0 rose, 1 rouge (fâché)

float smoothEdge(float dist, float width) { return constrain(1 - fabsf(dist) / width, 0.f, 1.f); }

// Tables fixes : stries, jeu de couleurs, volume, halos (calculées une fois au démarrage).
void buildProTables() {
  for (int a = 0; a < ANG; a++) {
    const float ang = a * 2 * PI / ANG - PI;
    fiberF[a] = (int8_t)(127 * sinf(ang * 96 + sinf(ang * 7) * 2));
    for (int r = 0; r < RINGS; r++) {
      const float u = (r + 0.5f) / RINGS;
      playF[a * RINGS + r] = (uint8_t)(255 * (0.5f + 0.5f * sinf(ang * 3 + u * 4)));
    }
  }
  for (int d2 = 0; d2 < IRIS_D; d2++) {
    const float t = (float)d2 / IRIS_D;
    volC[d2] = (uint8_t)(255 * smoothstep(0.2f, 0.95f, t));
  }
  for (int i = 0; i < 81; i++) dotA[i] = (uint8_t)(32 * powf(1 - sqrtf((float)i) / 9, 2.2f));
  for (int i = 0; i < 64; i++) glassA[i] = (uint8_t)(32 * 0.55f * powf(1 - sqrtf(i / 64.f), 1.5f));
  for (int i = 0; i < HALO_N; i++) {
    const float out = max(0.f, sqrtf((float)(i << 5)) - IRIS_R);
    haloC[i] = (uint8_t)(32 * 0.10f * expf(-powf(out / 40.f, 1.6f)) + 0.5f);
    haloG[i] = (uint8_t)(32 * 0.16f * expf(-powf(out / 46.f, 1.6f)) + 0.5f);
  }
}

// Texture de l'iris « Opale » pour la palette actuelle : dégradé centre → milieu → bord,
// jeu de couleurs, stries, collerette lumineuse et bord bleu nuit.
void buildClassicTex() {
  const float limbus[3] = {8, 12, 34};
  float play[3], bright[3];
  for (int i = 0; i < 3; i++) {
    play[i] = (classic.inner[i] + classic.outer[i]) / 2;
    bright[i] = min(255.f, classic.inner[i] * 1.15f);
  }
  for (int r = 0; r < RINGS; r++) {
    const float u = (r + 0.5f) / RINGS;
    float base[3];
    if (u < 0.45f) lerp3(classic.inner, classic.mid, smoothstep(0, 0.45f, u), base);
    else lerp3(classic.mid, classic.outer, smoothstep(0.45f, 0.9f, u), base);
    const float playK = 0.22f * smoothstep(0.1f, 0.5f, u) / 255.f;
    const float collar = smoothEdge(u - 0.1f, 0.06f) * 0.35f;
    const float limb = smoothstep(0.84f, 1.f, u) * 0.92f;
    const float fiberAmp = 0.08f * (1 - u * 0.6f) / 127.f;
    for (int a = 0; a < ANG; a++) {
      float col[3];
      lerp3(base, play, playK * playF[a * RINGS + r], col);
      const float fiber = 1 + fiberAmp * fiberF[a];
      for (int i = 0; i < 3; i++) col[i] *= fiber;
      lerp3(col, bright, collar, col);
      lerp3(col, limbus, limb, col);
      irisTex[a * RINGS + r] = rgbf(col[0], col[1], col[2]);
    }
  }
}

// Sphère du Gardien selon la distance : blanc chaud → pêche → rose → bordeaux, liseré fin.
// Reconstruite à chaque image (64 octets de calcul) : le cœur respire, le rouge monte si fâché.
void buildGuardLut(float breathe) {
  const float rose0[3] = {255, 107, 143}, rose1[3] = {255, 54, 64};
  const float wine0[3] = {58, 10, 30}, wine1[3] = {46, 4, 8};
  const float peach0[3] = {255, 201, 181}, peach1[3] = {255, 150, 120};
  const float white[3] = {255, 246, 240}, rim[3] = {255, 214, 226};
  float rose[3], wine[3], peach[3];
  lerp3(rose0, rose1, alert, rose);
  lerp3(wine0, wine1, alert, wine);
  lerp3(peach0, peach1, alert, peach);
  for (int d2 = 0; d2 < IRIS_D; d2++) {
    const float t = (float)d2 / IRIS_D;
    float s[3];
    if (t < 0.22f) lerp3(white, peach, smoothstep(0.06f + 0.06f * breathe, 0.22f, t), s);
    else if (t < 0.62f) lerp3(peach, rose, smoothstep(0.22f, 0.62f, t), s);
    else lerp3(rose, wine, smoothstep(0.62f, 0.95f, t), s);
    lerp3(s, rim, smoothEdge(t - 0.975f, 0.03f) * 0.95f, s);
    guardLut[d2] = rgbf(s[0], s[1], s[2]);
  }
  haloColor = rgbf(rose[0], rose[1], rose[2]);
  guardPeach = rgbf(peach[0], peach[1], peach[2]);
}

// ---------------------------------------------------------------------------
// Mode « appareil photo » : l'iris devient un objectif (diaphragme à 6 lamelles,
// bague crantée), déclic + flash à la prise de vue, point rouge pendant le direct.
// ---------------------------------------------------------------------------
constexpr uint16_t GLASS_IN = rgb(6, 10, 28);
constexpr uint16_t GLASS_OUT = rgb(52, 40, 120);
constexpr uint16_t BLADE_A = rgb(46, 50, 62);
constexpr uint16_t BLADE_B = rgb(34, 37, 47);
constexpr uint16_t BLADE_LINE = rgb(14, 15, 20);
constexpr uint16_t RING = rgb(24, 26, 32);
constexpr uint16_t RING_TICK = rgb(150, 156, 170);
constexpr uint16_t FLASH = rgb(255, 255, 255);
constexpr uint16_t REC = rgb(235, 30, 40);
constexpr int BLADES_OUT = IRIS_D * 84 / 100;  // au-delà : bague extérieure
constexpr int REC_X = 178, REC_Y = 56, REC_R2 = 7 * 7;  // voyant sur l'œil droit

uint16_t hexScale[ANG];  // bord d'un hexagone régulier selon l'angle (8.8)

struct {
  volatile int8_t request = -1;  // 0 visée, 1 déclic, 2 fin
  volatile bool recording = false;
  bool aiming = false;
  float amount = 0;              // fondu œil -> objectif
  uint32_t aimSince = 0, shootAt = 0, doneAt = 0;
} lensAnim;

// Sommeil (la nuit, sans personne) : paupières closes, écran assombri.
volatile bool sleepWanted = false;
float sleepAmount = 0;  // 0 éveillé .. 1 endormi
int dimLevel = 32;      // luminosité globale /32

struct {
  int alpha = 0;     // 0..32 : part de l'objectif dans l'iris
  int open2 = 0;     // rayon de l'ouverture du diaphragme (unités distance*2)
  int rot = 0;       // rotation des lamelles (unités d'angle)
  int ringRot = 0;   // rotation de la bague crantée
  int flash = 0;     // 0..32
  int rec = 0;       // 0..32 : intensité du voyant rouge (0 = éteint)
} lens;

void buildHexScale() {
  for (int k = 0; k < ANG; k++) {
    float th = fmodf(k * 2 * PI / ANG, PI / 3);
    hexScale[k] = (uint16_t)(256 * cosf(PI / 6) / cosf(th - PI / 6));
  }
}

void animateLens(uint32_t now, float dt) {
  switch (lensAnim.request) {
    case 0:
      if (!lensAnim.aiming) lensAnim.aimSince = now;
      lensAnim.aiming = true;
      lensAnim.doneAt = 0;
      break;
    case 1:
      lensAnim.aiming = true;
      lensAnim.shootAt = now;
      lensAnim.doneAt = now + 1500;  // fin automatique si le pont ne la demande pas
      break;
    case 2:
      lensAnim.doneAt = now + 350;  // laisse finir le flash
      break;
  }
  lensAnim.request = -1;
  if (lensAnim.doneAt && (int32_t)(now - lensAnim.doneAt) >= 0) {
    lensAnim.aiming = false;
    lensAnim.doneAt = 0;
  }
  if (lensAnim.aiming && now - lensAnim.aimSince > 40000) lensAnim.aiming = false;  // sécurité

  lensAnim.amount += ((lensAnim.aiming ? 1.f : 0.f) - lensAnim.amount) * min(1.f, dt * 6);
  lens.alpha = (int)(lensAnim.amount * 32 + 0.5f);

  // Mise au point : le diaphragme « respire » ; au déclic il se ferme puis se rouvre.
  float open = 0.62f + 0.10f * sinf(now * 0.0055f) + 0.04f * sinf(now * 0.017f);
  float flash = 0;
  uint32_t t = lensAnim.shootAt ? now - lensAnim.shootAt : 100000;
  if (t < 70) open *= 1 - t / 70.f;
  else if (t < 120) open = 0;
  else if (t < 300) open *= (t - 120) / 180.f;
  if (t >= 70 && t < 520) flash = expf(-(t - 70) / 140.f);
  lens.open2 = (int)(open * BLADES_OUT);
  lens.rot = (int)((1 - open) * 40) & (ANG - 1);
  lens.ringRot = (now / 18) & (ANG - 1);
  lens.flash = (int)(flash * 32);

  lens.rec = lensAnim.recording ? 14 + (int)(18 * (0.5f + 0.5f * sinf(now * 0.004f))) : 0;
}

// Couleur de l'objectif pour un point de l'iris (d2 = distance*2, a = angle).
inline uint16_t lensPixel(int d2, int a) {
  const int ka = (a + lens.rot) & (ANG - 1);
  if (d2 >= BLADES_OUT) {  // bague crantée
    return (((a + lens.ringRot) & 15) < 3) ? RING_TICK : RING;
  }
  if (d2 < ((lens.open2 * hexScale[ka]) >> 8)) {  // verre, au fond de l'ouverture
    return blend565(GLASS_IN, GLASS_OUT, lens.open2 ? min(32, d2 * 32 / lens.open2) : 0);
  }
  const int pos = (ka * 6) & (ANG - 1);  // position dans la lamelle
  if (pos < 12) return BLADE_LINE;
  const uint16_t base = (((ka * 6) >> 8) & 1) ? BLADE_A : BLADE_B;
  return scale565(base, 22 + (d2 * 10) / BLADES_OUT);
}

uint32_t frames = 0;  // compté par la tâche de l'œil gauche uniquement
uint32_t fpsStart = 0;
float fpsValue = 0;

float randRange(float a, float b) { return a + (b - a) * (esp_random() / 4294967295.f); }

void startBlink(int e, bool wink, uint32_t now) {
  Blink& b = anim.blink[e];
  b = Blink{};
  b.active = true;
  b.wink = wink;
  b.start = now;
  if (wink) {  // clin d'œil : appuyé et tenu un peu plus longtemps
    b.closeMs = 110;
    b.holdMs = 260;
    b.openMs = 200;
  }
}

float blinkAmount(Blink& b, uint32_t now) {
  if (!b.active) return 0;
  uint32_t t = now - b.start;
  if (t < b.closeMs) {
    float x = (float)t / b.closeMs;
    return x * x * (3 - 2 * x);
  }
  t -= b.closeMs;
  if (t < b.holdMs) return 1;
  t -= b.holdMs;
  if (t < b.openMs) {
    float x = 1 - (float)t / b.openMs;
    return x * x;
  }
  b.active = false;
  return 0;
}

void animate() {
  uint32_t now = millis();
  float dt = min(0.1f, (now - anim.lastMs) / 1000.f);
  anim.lastMs = now;

  if (anim.autoGaze && now >= anim.nextSaccade) {
    if (randRange(0, 1) < 0.25f) {  // micro-saccade
      anim.tgx += randRange(-0.08f, 0.08f);
      anim.tgy += randRange(-0.06f, 0.06f);
    } else {
      float a = randRange(0, 2 * PI), r = powf(randRange(0, 1), 1.5f) * 0.85f;
      anim.tgx = cosf(a) * r;
      anim.tgy = sinf(a) * r * 0.75f;
    }
    anim.nextSaccade = now + (uint32_t)randRange(350, 2800);
  }

  bool idle = !anim.blink[0].active && !anim.blink[1].active;
  if (anim.winkRequest >= 0) {
    startBlink(anim.winkRequest, true, now);
    anim.winkRequest = -1;
  } else if (anim.blinkRequest || (idle && now >= anim.nextBlink)) {
    anim.blinkRequest = false;
    startBlink(0, false, now);
    startBlink(1, false, now);
    if (anim.doubleBlink) {
      anim.doubleBlink = false;
      anim.nextBlink = now + 380;
    } else {
      anim.doubleBlink = randRange(0, 1) < 0.15f;
      anim.nextBlink = now + (uint32_t)randRange(2200, 6500);
    }
  }

  float g = min(1.f, dt * 28);  // saccade ≈ 35 ms
  anim.gx += (anim.tgx - anim.gx) * g;
  anim.gy += (anim.tgy - anim.gy) * g;
  float m = min(1.f, dt * 7);
  anim.pose.openU += (anim.target.openU - anim.pose.openU) * m;
  anim.pose.openL += (anim.target.openL - anim.pose.openL) * m;
  anim.pose.tilt += (anim.target.tilt - anim.pose.tilt) * m;
  float pupilTarget = anim.pupilOverride >= 0 ? anim.pupilOverride : anim.target.pupil;
  pupilTarget += 0.04f * sinf(now * 0.0007f) + 0.02f * sinf(now * 0.0023f);  // hippus
  anim.pupil += (pupilTarget - anim.pupil) * min(1.f, dt * 3);

  animateLens(now, dt);
  const int mi = moodIndex;
  alert += ((mi == (int)Mood::Angry ? 1.f : 0.f) - alert) * min(1.f, dt * 2.5f);  // fâché : vire au rouge
  if (style == Style::Guard) {
    buildGuardLut(0.5f + 0.5f * sinf(now * 0.0021f));  // le cœur respire
  } else {
    // Opale : la palette glisse vers celle de l'humeur ; texture refaite seulement pendant la transition.
    if (irisColor == IrisColor::Opal) {
      const ClassicPalette& want = CLASSIC_PAL[mi];
      float* cur[3] = {classic.inner, classic.mid, classic.outer};
      const float* tgt[3] = {want.inner, want.mid, want.outer};
      const float k = min(1.f, dt * 3);
      bool changed = false;
      for (int j = 0; j < 3; j++) {
        for (int i = 0; i < 3; i++) {
          const float d = tgt[j][i] - cur[j][i];
          if (d == 0) continue;
          cur[j][i] = fabsf(d) < 1 ? tgt[j][i] : cur[j][i] + d * k;
          changed = true;
        }
      }
      if (changed) buildClassicTex();
    }
    haloColor = rgbf(classic.mid[0], classic.mid[1], classic.mid[2]);
  }
  const float dotAng = now * 0.00055f;

  if (sceneRequested) {
    sceneNow = sceneWanted;
    sceneInfo = infoWanted;
    sceneStart = now;
    sceneUntil = sceneWantedMs ? now + sceneWantedMs : 0;
    sceneRequested = false;
  }
  if (sceneNow == Scene::Timer && sceneInfo.timerMs && now - sceneStart >= sceneInfo.timerMs) {
    sceneUntil = now;  // minuteur écoulé : retour aux yeux, qui clignent
    anim.blinkRequest = true;
  }
  if (sceneNow != Scene::None && sceneUntil && (int32_t)(now - sceneUntil) >= 0) sceneNow = Scene::None;
  // Sommeil : les paupières se ferment lentement et l'écran s'assombrit ; réveil plus vif.
  const float sleepTarget = sleepWanted ? 1.f : 0.f;
  sleepAmount += (sleepTarget - sleepAmount) * min(1.f, dt * (sleepWanted ? 0.8f : 3.f));
  dimLevel = 32 - (int)(sleepAmount * 22);
  float closed[2] = {max(blinkAmount(anim.blink[0], now), sleepAmount),
                     max(blinkAmount(anim.blink[1], now), sleepAmount)};
  const float lookAhead = 1 - lensAnim.amount;  // en mode objectif, les yeux visent droit devant
  for (int e = 0; e < 2; e++) {
    EyeFrame& f = frame[e];
    float gx = constrain((anim.gx + 0.05f * INNER[e]) * lookAhead, -1.f, 1.f);  // convergence
    float gy = constrain(anim.gy * lookAhead, -1.f, 1.f);
    f.icx = CX + (int)lroundf(gx * GAZE_X_PX);
    f.icy = CY + (int)lroundf(gy * GAZE_Y_PX);
    f.dotX = f.icx + cosf(dotAng + e * 0.6f) * GUARD_RING_R;
    f.dotY = f.icy + sinf(dotAng + e * 0.6f) * GUARD_RING_R;
    // Un disque vu de biais s'aplatit : l'iris rétrécit dans le sens du regard.
    f.fx = (int)(256 / (1 - 0.22f * gx * gx));
    f.fy = (int)(256 / (1 - 0.22f * gy * gy));
    f.irisHalfW = IRIS_R * 256 / f.fx + 1;
    f.irisHalfH = IRIS_R * 256 / f.fy + 1;
    int pr = 18 + (int)(constrain(anim.pupil, 0.f, 1.f) * 16);
    f.pupil2 = pr * 2;
    f.ringScale = (RINGS << 8) / (IRIS_D - f.pupil2);

    // Pendant un clin d'œil, l'autre œil plisse un peu (la joue remonte).
    int other = 1 - e;
    float squint = anim.blink[other].wink ? closed[other] * 0.35f : 0;
    float openL = anim.pose.openL * (1 - squint);
    float openU = anim.pose.openU * (1 - squint * 0.3f);
    for (int x = 0; x < W; x++) {
      float u = (x - CX) / 128.f;
      float c = lidCurve[x];
      float lower = LID_CLOSE_Y + openL * LOWER_OPEN_H * c + gy * 10;
      float upper = LID_CLOSE_Y - openU * UPPER_OPEN_H * c + anim.pose.tilt * u * INNER[e] * 45
                    + gy * 18;
      upper = min(upper, lower);
      // Clignement : les paupières se rejoignent un peu sous le milieu (celle du bas remonte).
      float meet = LID_CLOSE_Y + (lower - LID_CLOSE_Y) * 0.35f;
      upper += (meet - upper) * closed[e];
      lower += (meet - lower) * closed[e];
      f.upper[x] = (int16_t)lroundf(upper * 16);
      f.lower[x] = (int16_t)lroundf(lower * 16);
    }
  }
}

// ---------------------------------------------------------------------------
// Rendu
// ---------------------------------------------------------------------------
void renderStrip(int e, int y0, uint16_t* out) {
  const EyeFrame& f = frame[e];
  const bool guard = style == Style::Guard;
  const uint16_t bg = guard ? GUARD_BG : PRO_BG, lid = guard ? GUARD_LID : PRO_LID;
  const uint8_t* halo = guard ? haloG : haloC;
  const uint16_t haloCol = haloColor;
  const int dotX = (int)lroundf(f.dotX), dotY = (int)lroundf(f.dotY);
  const int lensAlpha = lens.alpha, flash = lens.flash, dim = dimLevel;

  for (int y = y0; y < y0 + STRIP_H; y++) {
    uint16_t* o = out + (y - y0) * W;
    const int half = spanHalf[y];
    const int x0 = CX - half, x1 = CX + half;
    memset(o, 0, x0 * sizeof(uint16_t));
    memset(o + x1, 0, (W - x1) * sizeof(uint16_t));

    const int y16 = y << 4;
    const int ry = y - f.icy, ry2 = ry * ry, ary = abs(ry);
    const uint16_t* polarRow = nullptr;
    if (ry > -f.irisHalfH && ry < f.irisHalfH) {
      const int iy = ((ry * f.fy) >> 8) + IRIS_R;
      if ((unsigned)iy < IRIS_D) polarRow = polar + iy * IRIS_D;
    }
    const int irisX0 = f.icx - f.irisHalfW, irisW = 2 * f.irisHalfW;
    const int dotDy = y - dotY, dotDy2 = dotDy * dotDy;
    const bool dotRow = guard && dotDy2 < 81;
    const bool tickRowH = guard && ary < 2;                          // repères gauche / droite
    const bool tickRowV = guard && ary > TICK_IN && ary < TICK_OUT;  // repères haut / bas
    const int gy = ry + 26, gy2 = gy * gy;                           // reflet de verre du Gardien
    const int winY = abs(ry - WIN_Y), spotY = ry - SPOT_Y;
    const int recDy = y - REC_Y;
    const bool recRow = e == 1 && lens.rec && recDy * recDy < REC_R2;

    for (int x = x0; x < x1; x++) {
      const int coverU = f.upper[x] - y16;       // > 0 : la paupière du haut couvre la ligne
      const int coverL = y16 + 16 - f.lower[x];  // > 0 : la paupière du bas couvre la ligne
      uint16_t c;
      if (coverU >= 16 || coverL >= 16) {
        c = lid;  // paupière noire, sans liseré : l'écran « se ferme »
      } else {
        const int rx = x - f.icx, r2 = rx * rx + ry2;
        c = blend565(bg, haloCol, halo[min(r2 >> 5, HALO_N - 1)]);  // halo discret
        if (guard) {
          const int rd = abs(r2 - RING_R2);  // anneau fin
          if (rd < RING_W) c = blend565(c, haloCol, 13 * (RING_W - rd) / RING_W);
          const int arx = abs(rx);
          if ((tickRowH && arx > TICK_IN && arx < TICK_OUT) || (tickRowV && arx < 2))
            c = blend565(c, guardPeach, 18);
          if (dotRow) {  // point de lumière qui fait le tour de l'anneau
            const int qx = x - dotX, dd = qx * qx + dotDy2;
            if (dd < 81) c = blend565(c, WHITE, dotA[dd]);
          }
        }
        if (polarRow && (unsigned)(x - irisX0) < (unsigned)irisW) {
          const int ix = ((rx * f.fx) >> 8) + IRIS_R;
          if ((unsigned)ix < IRIS_D) {
            const uint16_t p = polarRow[ix];
            const int d2 = p >> 8;
            if (d2 < IRIS_D) {
              uint16_t s;
              if (guard) {  // sphère de verre : dégradé radial + reflet elliptique
                s = guardLut[d2];
                const int gx = rx + 22, gx2 = gx * gx;
                if (gx2 < 441 && gy2 < 154) {
                  const int q = gx2 * 64 / 441 + gy2 * 64 / 154;
                  if (q < 64) s = blend565(s, WHITE, glassA[q]);
                }
              } else {
                if (d2 < f.pupil2) {
                  s = PRO_PUPIL;
                } else {
                  s = irisTex[(p & 0xFF) * RINGS + (((d2 - f.pupil2) * f.ringScale) >> 8)];
                  if (d2 < f.pupil2 + 3) s = blend565(PRO_PUPIL, s, (d2 - f.pupil2) * 10 + 2);
                }
                const int proj = (rx * 154 + ry * 205) >> 8;  // volume : ombre à l'opposé du reflet
                if (proj > 0) {
                  const int k = (min(proj * 32 / IRIS_R, 32) * volC[d2]) >> 8;
                  s = scale565(s, 32 - ((k * 10) >> 5));
                }
                const int ax = abs(rx - WIN_X);  // reflet en forme de fenêtre
                if (ax < 10 && winY < 8) s = blend565(s, WHITE, min(25, min(10 - ax, 8 - winY) * 17));
                const int sx = rx - SPOT_X, sd = sx * sx + spotY * spotY;
                if (sd < 10) s = blend565(s, WHITE, sd < 4 ? 19 : 10);
              }
              if (lensAlpha) s = blend565(s, lensPixel(d2, p & 0xFF), lensAlpha);
              c = d2 >= IRIS_D - 2 ? blend565(c, s, (IRIS_D - d2) * 11) : s;  // bord lissé
            }
          }
        }
        // Bords des paupières lissés au sous-pixel (mouvements fluides).
        if (coverU > 0) c = blend565(c, lid, coverU * 2);
        if (coverL > 0) c = blend565(c, lid, coverL * 2);
      }
      if (dim < 32) c = scale565(c, dim);
      if (flash) c = blend565(c, FLASH, flash);
      if (recRow) {  // voyant « on te filme », bord adouci
        const int rdx = x - REC_X, d = rdx * rdx + recDy * recDy;
        if (d < REC_R2) c = blend565(c, REC, d < 25 ? lens.rec : lens.rec * (REC_R2 - d) / 24);
      }
      o[x] = (uint16_t)((c >> 8) | (c << 8));  // ordre d'octets de l'écran
    }
  }
}

void drawScene(int e) {
  scenes::draw(e, sprite[e], sceneNow, sceneInfo, millis(), sceneStart);
  lcd[e]->startWrite();
  sprite[e].pushSprite(lcd[e], 0, 0);
  lcd[e]->endWrite();
}

void drawEye(int e) {
  if (sceneNow != Scene::None && sprite[e].getBuffer()) {
    drawScene(e);
    return;
  }
  lcd[e]->startWrite();
  int k = 0;
  for (int y = 0; y < H; y += STRIP_H, k ^= 1) {
    renderStrip(e, y, strip[e][k]);
    lcd[e]->waitDMA();
    lcd[e]->pushImageDMA(0, y, W, STRIP_H, (const lgfx::swap565_t*)strip[e][k]);
  }
  lcd[e]->waitDMA();
  lcd[e]->endWrite();
}

// Un œil par cœur : l'œil gauche (cœur 0) anime puis lance l'œil droit (cœur 1),
// les deux se dessinent en même temps, puis on attend l'œil droit avant l'image suivante.
SemaphoreHandle_t frameStart, frameDone;

void eyeTask(void* arg) {
  const int e = (int)(intptr_t)arg;
  for (;;) {
    if (e == 0) {
      animate();
      xSemaphoreGive(frameStart);
      drawEye(0);
      xSemaphoreTake(frameDone, portMAX_DELAY);
      frames++;
      uint32_t now = millis();
      if (now - fpsStart >= 1000) {
        fpsValue = frames * 1000.f / (now - fpsStart);
        frames = 0;
        fpsStart = now;
      }
      vTaskDelay(1);  // laisse respirer les autres tâches du cœur 0 (WiFi, son)
    } else {
      xSemaphoreTake(frameStart, portMAX_DELAY);
      drawEye(1);
      xSemaphoreGive(frameDone);
    }
  }
}

}  // namespace

void begin(IrisColor color) {
  if (polar) return;
  playF = (uint8_t*)heap_caps_malloc(ANG * RINGS, MALLOC_CAP_SPIRAM);
  irisTex = (uint16_t*)heap_caps_malloc(ANG * RINGS * 2, MALLOC_CAP_8BIT);
  polar = (uint16_t*)heap_caps_malloc(IRIS_D * IRIS_D * 2, MALLOC_CAP_8BIT);
  for (auto& eye : strip)
    for (auto& s : eye) s = (uint16_t*)heap_caps_malloc(W * STRIP_H * 2, MALLOC_CAP_DMA);
  if (!playF || !irisTex || !polar || !strip[1][1]) {
    Serial.println("eyes: mémoire insuffisante");
    polar = nullptr;
    return;
  }

  uint32_t t0 = millis();
  buildShade();
  buildHexScale();
  buildPolar();
  buildProTables();
  buildIrisFor(color);
  for (auto& s : sprite) {  // animations : un écran complet par œil, en PSRAM
    s.setPsram(true);
    s.setColorDepth(16);
    if (!s.createSprite(W, H)) Serial.println("eyes: sprite d'animation impossible");
  }
  Serial.printf("eyes: textures prêtes en %lu ms\n", (unsigned long)(millis() - t0));

  const uint8_t rot[2] = {EYE_L_ROTATION, EYE_R_ROTATION};
  for (int e = 0; e < 2; e++) {  // gauche d'abord : son reset vaut pour les deux
    lcd[e]->init();
    lcd[e]->setRotation(rot[e]);
    lcd[e]->fillScreen(TFT_BLACK);
  }
  anim.lastMs = fpsStart = millis();

  frameStart = xSemaphoreCreateBinary();
  frameDone = xSemaphoreCreateBinary();
  xTaskCreatePinnedToCore(eyeTask, "eye_left", 4096, (void*)0, 1, nullptr, 0);
  xTaskCreatePinnedToCore(eyeTask, "eye_right", 4096, (void*)1, 1, nullptr, 1);
}

void setMood(Mood mood) {
  anim.target = MOODS[(int)mood];
  moodIndex = (int)mood;
}

void lookAt(float x, float y) {
  anim.autoGaze = false;
  anim.tgx = constrain(x, -1.f, 1.f);
  anim.tgy = constrain(y, -1.f, 1.f);
}

void autoGaze() { anim.autoGaze = true; }

void blink() { anim.blinkRequest = true; }

void wink(int eye) { anim.winkRequest = (int8_t)constrain(eye, 0, 1); }

void setPupil(float size) { anim.pupilOverride = size; }

void setIrisColor(IrisColor color) {
  if (irisTex) buildIrisFor(color);
}

void cameraAim() { lensAnim.request = 0; }
void cameraShoot() { lensAnim.request = 1; }
void cameraDone() { lensAnim.request = 2; }
void setRecording(bool on) { lensAnim.recording = on; }
void setAsleep(bool asleep) { sleepWanted = asleep; }
void setStyle(Style s) { style = s; }

void showScene(Scene s, uint32_t durationMs, const SceneInfo* info) {
  while (sceneRequested) delay(1);  // la demande précédente n'est pas encore prise
  sceneWanted = s;
  infoWanted = info ? *info : SceneInfo{};
  sceneWantedMs = durationMs;
  sceneRequested = true;
}
void endScene() { showScene(Scene::None, 0); }
Scene scene() { return sceneRequested ? sceneWanted : sceneNow; }

float fps() { return fpsValue; }

}  // namespace eyes
