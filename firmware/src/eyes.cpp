#include "eyes.h"

#include <Arduino.h>
#include <LovyanGFX.hpp>
#include <esp_heap_caps.h>
#include <freertos/semphr.h>
#include <math.h>

#include "config.h"

namespace eyes {
namespace {

constexpr int W = 240, H = 240, CX = 120, CY = 120;
constexpr int VISIBLE_R2 = 121 * 121;  // hors de ce cercle l'écran rond n'affiche rien
constexpr int IRIS_R = 62, IRIS_D = IRIS_R * 2;
constexpr int ANG = 256, RINGS = 64;    // texture d'iris en coordonnées polaires
constexpr int SCLERA_W = 320, SCLERA_PAD = (SCLERA_W - W) / 2;
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

constexpr uint16_t PUPIL = rgb(6, 6, 9);
constexpr uint16_t SKIN = rgb(122, 82, 68);
constexpr uint16_t LASH_UPPER = rgb(24, 14, 14);
constexpr uint16_t LASH_LOWER = rgb(85, 52, 48);
constexpr uint16_t SPECULAR = rgb(255, 255, 255);
constexpr uint16_t TEAR = rgb(232, 168, 160);  // bord humide au-dessus de la paupière du bas

struct Palette {
  float inner[3], mid[3], outer[3], limbus[3];
};

const Palette PALETTES[] = {
    {{205, 150, 65}, {115, 128, 70}, {72, 96, 60}, {22, 28, 22}},     // Hazel
    {{175, 195, 205}, {90, 140, 190}, {52, 92, 148}, {18, 28, 48}},   // Blue
    {{150, 95, 45}, {108, 66, 32}, {76, 46, 24}, {24, 14, 10}},       // Brown
    {{185, 175, 85}, {82, 145, 92}, {46, 106, 72}, {14, 34, 24}},     // Green
};

// ---------------------------------------------------------------------------
// Textures précalculées au démarrage
// ---------------------------------------------------------------------------
uint16_t* sclera;   // SCLERA_W² en PSRAM, glisse avec le regard (le globe tourne)
uint16_t* irisTex;  // [angle][anneau]
uint16_t* polar;    // pour chaque pixel autour de l'iris : (distance*2) << 8 | angle
uint8_t shadeLut[(2 * 121 * 121 >> 5) + 1];  // ombrage sphérique selon r², 0..64 (tramé)
uint8_t spanHalf[H];  // demi-largeur visible de chaque ligne de l'écran rond
float lidCurve[W];
uint16_t* strip[2][2];

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

void plotVein(float x, float y, float dir, float len, float alpha, int depth) {
  for (int s = 0; s < (int)len; s++) {
    x += cosf(dir);
    y += sinf(dir);
    dir += (rnd() - 0.5f) * 0.35f;
    float a = alpha * (1 - s / len);
    int thick = s < len * 0.3f && depth == 0 ? 2 : 1;
    for (int oy = 0; oy < thick; oy++) {
      for (int ox = 0; ox < thick; ox++) {
        int px = (int)x + ox, py = (int)y + oy;
        if (px < 0 || py < 0 || px >= SCLERA_W || py >= SCLERA_W) continue;
        uint16_t& c = sclera[py * SCLERA_W + px];
        c = blend565(c, rgb(178, 46, 52), (uint32_t)(a * 32));
      }
    }
    if (depth < 2 && rnd() < 0.025f) {
      plotVein(x, y, dir + (rnd() < 0.5f ? 0.6f : -0.6f), (len - s) * 0.6f, a * 0.8f, depth + 1);
    }
  }
}

void buildSclera() {
  const float white[3] = {240, 235, 228}, pink[3] = {222, 184, 178};
  const float half = SCLERA_W / 2.f;
  for (int y = 0; y < SCLERA_W; y++) {
    for (int x = 0; x < SCLERA_W; x++) {
      float fx = (x - half) / half, fy = (y - half) / half;
      float col[3];
      lerp3(white, pink, smoothstep(0.35f, 1.f, sqrtf(fx * fx + fy * fy)), col);
      float n = (rnd() - 0.5f) * 8;
      sclera[y * SCLERA_W + x] = rgbf(col[0] + n, col[1] + n, col[2] + n);
    }
  }
  for (int v = 0; v < 18; v++) {
    float a = rnd() * 2 * PI;
    float x = half + cosf(a) * 165, y = half + sinf(a) * 165;
    plotVein(x, y, a + PI + (rnd() - 0.5f) * 0.8f, 70 + rnd() * 80, 0.35f + rnd() * 0.25f, 0);
  }
}

void buildShade() {
  for (size_t i = 0; i < sizeof(shadeLut); i++) {
    float r = sqrtf((float)(i << 5)) / 120.f;
    shadeLut[i] = (uint8_t)constrain(64 * (1 - 0.45f * powf(r, 2.5f)), 16.f, 64.f);
  }
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
  int icx, icy, sx, sy;
  int fx, fy;                  // aplatissement de l'iris (8.8) quand l'œil tourne
  int irisHalfW, irisHalfH;    // étendue de l'iris à l'écran
  int pupil2, ringScale;
  int16_t upper[W], lower[W];  // bords des paupières en 1/16 de pixel
};
EyeFrame frame[2];

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

  float closed[2] = {blinkAmount(anim.blink[0], now), blinkAmount(anim.blink[1], now)};
  for (int e = 0; e < 2; e++) {
    EyeFrame& f = frame[e];
    float gx = constrain(anim.gx + 0.05f * INNER[e], -1.f, 1.f);  // légère convergence
    float gy = constrain(anim.gy, -1.f, 1.f);
    f.icx = CX + (int)lroundf(gx * GAZE_X_PX);
    f.icy = CY + (int)lroundf(gy * GAZE_Y_PX);
    f.sx = SCLERA_PAD + (int)(gx * 30);
    f.sy = SCLERA_PAD + (int)(gy * 26);
    // Un disque vu de biais s'aplatit : l'iris rétrécit dans le sens du regard.
    f.fx = (int)(256 / (1 - 0.22f * gx * gx));
    f.fy = (int)(256 / (1 - 0.22f * gy * gy));
    f.irisHalfW = IRIS_R * 256 / f.fx + 1;
    f.irisHalfH = IRIS_R * 256 / f.fy + 1;
    int pr = 16 + (int)(constrain(anim.pupil, 0.f, 1.f) * 30);
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
inline uint16_t lidPixel(int depth, bool upper, uint32_t shade) {
  // depth : pixels entiers entre la ligne et le bord de la paupière
  if (upper) {
    if (depth < 3) return LASH_UPPER;
    if (depth >= 17 && depth < 20) return scale565(SKIN, shade * 26 >> 5);  // pli de la paupière
    return scale565(SKIN, shade);
  }
  return depth < 2 ? LASH_LOWER : scale565(SKIN, shade);
}

void renderStrip(int e, int y0, uint16_t* out) {
  const EyeFrame& f = frame[e];
  const int hx1 = f.icx - 22, hy1 = f.icy - 24;  // reflets : lumière en haut à gauche
  const int hx2 = f.icx + 16, hy2 = f.icy + 18;

  for (int y = y0; y < y0 + STRIP_H; y++) {
    uint16_t* o = out + (y - y0) * W;
    const int half = spanHalf[y];
    const int x0 = CX - half, x1 = CX + half;
    memset(o, 0, x0 * sizeof(uint16_t));
    memset(o + x1, 0, (W - x1) * sizeof(uint16_t));

    const int dy = y - CY, dy2 = dy * dy;
    const int y16 = y << 4;
    const uint16_t* scleraRow = sclera + (y + f.sy) * SCLERA_W + f.sx;
    const int ry = y - f.icy;
    const uint16_t* polarRow = nullptr;
    if (ry > -f.irisHalfH && ry < f.irisHalfH) {
      const int iy = ((ry * f.fy) >> 8) + IRIS_R;
      if ((unsigned)iy < IRIS_D) polarRow = polar + iy * IRIS_D;
    }
    const int irisX0 = f.icx - f.irisHalfW, irisW = 2 * f.irisHalfW;
    const int dh1 = y - hy1, dh2 = y - hy2;
    const int dh1sq = dh1 * dh1, dh2sq = dh2 * dh2;
    const uint32_t ditherRow = y & 1;

    for (int x = x0; x < x1; x++) {
      const int dx = x - CX;
      // Ombrage sur 64 niveaux tramé en damier vers 32 : plus de bandes visibles.
      const uint32_t shade = (shadeLut[(dx * dx + dy2) >> 5] + (ditherRow ^ (x & 1))) >> 1;
      const int up = f.upper[x], lo = f.lower[x];
      const int coverU = up - y16;         // > 0 : la paupière du haut couvre la ligne
      const int coverL = y16 + 16 - lo;    // > 0 : la paupière du bas couvre la ligne
      uint16_t c;

      if (coverU >= 16) {
        c = lidPixel((coverU - 16) >> 4, true, shade);
      } else if (coverL >= 16) {
        c = lidPixel((coverL - 16) >> 4, false, shade);
      } else {
        bool inIris = false;
        if (polarRow && (unsigned)(x - irisX0) < (unsigned)irisW) {
          const int ix = (((x - f.icx) * f.fx) >> 8) + IRIS_R;
          if ((unsigned)ix < IRIS_D) {
            const uint16_t p = polarRow[ix];
            const int d2 = p >> 8;
            if (d2 < IRIS_D) {
              inIris = true;
              if (d2 < f.pupil2) {
                c = PUPIL;
              } else {
                const int ring = ((d2 - f.pupil2) * f.ringScale) >> 8;
                c = irisTex[(p & 0xFF) * RINGS + ring];
              }
            }
          }
        }
        if (!inIris) c = scleraRow[x];
        c = scale565(c, shade);

        const int below = -coverU >> 4;  // ombre portée de la paupière supérieure
        if (below < 14) c = scale565(c, 14 + max(below, 0) * 18 / 14);
        const int above = -coverL >> 4;  // ligne humide au ras de la paupière inférieure
        if (above < 2) c = blend565(c, TEAR, 9);

        if (inIris) {
          if (dh1sq < 121) {
            const int a = x - hx1, d = a * a + dh1sq;
            if (d < 121) c = blend565(c, SPECULAR, d < 36 ? 28 : (121 - d) * 28 / 85);
          }
          if (dh2sq < 16) {
            const int a = x - hx2;
            if (a * a + dh2sq < 16) c = blend565(c, SPECULAR, 16);
          }
        }
        // Bords des paupières lissés au sous-pixel (mouvements fluides).
        if (coverU > 0) c = blend565(c, LASH_UPPER, coverU * 2);
        if (coverL > 0) c = blend565(c, LASH_LOWER, coverL * 2);
      }
      o[x] = (uint16_t)((c >> 8) | (c << 8));  // ordre d'octets de l'écran
    }
  }
}

void drawEye(int e) {
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
  sclera = (uint16_t*)heap_caps_malloc(SCLERA_W * SCLERA_W * 2, MALLOC_CAP_SPIRAM);
  irisTex = (uint16_t*)heap_caps_malloc(ANG * RINGS * 2, MALLOC_CAP_8BIT);
  polar = (uint16_t*)heap_caps_malloc(IRIS_D * IRIS_D * 2, MALLOC_CAP_8BIT);
  for (auto& eye : strip)
    for (auto& s : eye) s = (uint16_t*)heap_caps_malloc(W * STRIP_H * 2, MALLOC_CAP_DMA);
  if (!sclera || !irisTex || !polar || !strip[1][1]) {
    Serial.println("eyes: mémoire insuffisante");
    polar = nullptr;
    return;
  }

  uint32_t t0 = millis();
  buildShade();
  buildPolar();
  buildIris(PALETTES[(int)color]);
  buildSclera();
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

void setMood(Mood mood) { anim.target = MOODS[(int)mood]; }

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
  if (irisTex) buildIris(PALETTES[(int)color]);
}

float fps() { return fpsValue; }

}  // namespace eyes
