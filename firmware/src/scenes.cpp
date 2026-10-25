// Animations plein écran d'Opal Box (cœurs, horloge, météo, musique…), dessinées dans un
// sprite 240×240 par œil puis envoyées à l'écran. Rendu validé sur l'aperçu web
// (même géométrie) avant d'arriver ici.
#include "scenes.h"

#include <Arduino.h>
#include <math.h>
#include <time.h>

namespace eyes {
namespace scenes {
namespace {

using Sprite = lgfx::LGFX_Sprite;
constexpr int CX = 120, CY = 120;
constexpr float TAU = 6.2831853f;

uint16_t hex(uint32_t c) { return lgfx::color565(c >> 16, (c >> 8) & 0xFF, c & 0xFF); }
uint16_t mix(uint32_t a, uint32_t b, float t) {
  t = constrain(t, 0.f, 1.f);
  auto ch = [&](int sh) { return (int)(((a >> sh) & 0xFF) * (1 - t) + ((b >> sh) & 0xFF) * t); };
  return lgfx::color565(ch(16), ch(8), ch(0));
}
uint16_t hsl(float h, float s, float l) {  // h en degrés
  auto f = [&](float n) {
    float k = fmodf(n + h / 30.f, 12.f), a = s * min(l, 1 - l);
    return (int)(255 * (l - a * max(-1.f, min(min(k - 3, 9 - k), 1.f))));
  };
  return lgfx::color565(f(0), f(8), f(4));
}
float randf(float a, float b) { return a + (b - a) * (esp_random() / 4294967295.f); }
float smooth(float a, float b, float x) {
  float t = constrain((x - a) / (b - a), 0.f, 1.f);
  return t * t * (3 - 2 * t);
}

void text(Sprite& s, const char* str, int x, int y, const lgfx::IFont* font, uint16_t color) {
  s.setFont(font);
  s.setTextDatum(textdatum_t::middle_center);
  s.setTextColor(color);
  s.drawString(str, x, y);
}

// Cœur : deux lobes + pointe.
void heart(Sprite& s, float x, float y, float size, uint16_t c) {
  const float r = size * 0.30f;
  s.fillSmoothCircle(x - size * 0.26f, y - size * 0.10f, r, c);
  s.fillSmoothCircle(x + size * 0.26f, y - size * 0.10f, r, c);
  s.fillTriangle(x - size * 0.545f, y - size * 0.02f, x + size * 0.545f, y - size * 0.02f, x,
                 y + size * 0.55f, c);
}

// Particules par œil (petits cœurs, gouttes, Z…), remises à zéro à chaque animation.
struct Particle {
  float x, y, v, size, life;
  bool on;
};
Particle parts[2][28];
uint32_t partsFor[2] = {0, 0};

Particle* spawn(int e) {
  for (auto& p : parts[e])
    if (!p.on) return &(p = Particle{0, 0, 0, 0, 1, true});
  return nullptr;
}

// Matrix : colonnes de caractères qui tombent.
struct Column {
  float y, v;
  char ch[12];
};
Column cols[2][20];

// Étourdi : distance et angle de chaque pixel, précalculés une fois (PSRAM).
uint16_t* polarMap = nullptr;

void reset(int e, Scene scene) {
  for (auto& p : parts[e]) p.on = false;
  if (scene == Scene::Matrix) {
    for (auto& c : cols[e]) {
      c.y = randf(-240, 0);
      c.v = randf(1.5f, 4.f);
      for (auto& k : c.ch) k = "0123456789ABCDEFOPAL"[esp_random() % 20];
    }
  }
}

// ── Les animations ─────────────────────────────────────────────────────────

void hearts(Sprite& s, int e, uint32_t t) {
  s.fillScreen(hex(0x12030a));
  const float beat = 1 + 0.12f * max(sinf(t * 0.009f), 0.f) + 0.06f * max(sinf(t * 0.009f - 1.2f), 0.f);
  if (randf(0, 1) < 0.06f)
    if (Particle* p = spawn(e)) *p = {randf(50, 190), 250, randf(0.6f, 1.4f), randf(14, 30), 1, true};
  for (auto& p : parts[e]) {
    if (!p.on) continue;
    p.y -= p.v * 1.6f;
    if (p.y < -30) p.on = false;
    else heart(s, p.x + 6 * sinf(p.y / 14), p.y, p.size, hex(0x8a3858));
  }
  heart(s, CX, CY + 8, 172 * beat, hex(0x4a0a22));  // halo
  heart(s, CX, CY + 8, 150 * beat, hex(0xff2d6f));
  s.fillSmoothCircle(CX - 30, CY - 22, 12 * beat, hex(0xff9ab9));
}

void clock(Sprite& s, int e, uint32_t now) {
  s.fillScreen(hex(0x05070c));
  struct tm tm;
  const bool ok = getLocalTime(&tm, 0);
  const float sec = ok ? tm.tm_sec + (now % 1000) / 1000.f : 0;
  s.fillArc(CX, CY, 104, 112, 0, 360, hex(0x141a26));
  if (sec > 0.05f) s.fillArc(CX, CY, 104, 112, -90, -90 + 360 * sec / 60, e ? hex(0x7fd3e6) : hex(0xe8c26a));
  char big[4] = "--";
  if (ok) snprintf(big, sizeof(big), "%02d", e ? tm.tm_min : tm.tm_hour);
  text(s, big, CX, CY + 2, &fonts::DejaVu72, hex(0xe8e6e1));
  static const char* const DAYS[] = {"DIM", "LUN", "MAR", "MER", "JEU", "VEN", "SAM"};
  char small[16] = "MIN";
  if (!e) snprintf(small, sizeof(small), "%s %d", ok ? DAYS[tm.tm_wday] : "", ok ? tm.tm_mday : 0);
  text(s, small, CX, CY + 62, &fonts::DejaVu18, hex(0x9a9ca6));
}

void cloud(Sprite& s, float cx, float cy, uint16_t c) {
  s.fillSmoothCircle(cx - 38, cy + 10, 26, c);
  s.fillSmoothCircle(cx - 6, cy - 6, 34, c);
  s.fillSmoothCircle(cx + 30, cy + 8, 26, c);
  s.fillSmoothCircle(cx, cy + 18, 26, c);
}

void weather(Sprite& s, int e, uint32_t t, const SceneInfo& in) {
  s.fillScreen(hex(0x06101c));
  if (e == 1) {
    char temp[8];
    snprintf(temp, sizeof(temp), "%d", in.temp);
    text(s, temp, CX - 6, CY - 12, &fonts::DejaVu72, hex(0xe8e6e1));
    s.setFont(&fonts::DejaVu72);
    const int w = s.textWidth(temp);
    s.fillSmoothCircle(CX - 6 + w / 2 + 12, CY - 40, 8, hex(0xe8e6e1));
    s.fillSmoothCircle(CX - 6 + w / 2 + 12, CY - 40, 4, hex(0x06101c));
    char line[56];
    snprintf(line, sizeof(line), "%s%s%s", in.desc, in.place[0] ? " - " : "", in.place);
    text(s, line, CX, CY + 46, &fonts::DejaVu18, hex(0x9fc4e8));
    snprintf(line, sizeof(line), "min %d   max %d", in.tmin, in.tmax);
    text(s, line, CX, CY + 72, &fonts::DejaVu12, hex(0x9a9ca6));
    return;
  }
  const bool sun = !strcmp(in.icon, "sun"), cloudy = !sun && strcmp(in.icon, "fog");
  if (sun || !strcmp(in.icon, "cloud")) {  // soleil (seul, ou derrière le nuage)
    const float sx = sun ? CX : CX + 22, sy = sun ? CY : CY - 26, a0 = t * 0.0006f;
    for (int i = 0; i < 8; i++) {
      const float a = a0 + i * TAU / 8;
      s.drawWideLine(sx + cosf(a) * 46, sy + sinf(a) * 46, sx + cosf(a) * 60, sy + sinf(a) * 60, 3,
                     hex(0xffcf4a));
    }
    s.fillSmoothCircle(sx, sy, 40, hex(0x5a4010));
    s.fillSmoothCircle(sx, sy, 34, hex(0xffcf4a));
  }
  const float cx = CX - 8 + 6 * sinf(t * 0.0012f);
  if (cloudy) cloud(s, cx, CY + 20, !strcmp(in.icon, "storm") ? hex(0x8a93a6) : hex(0xdfe7f2));
  if (!strcmp(in.icon, "fog")) {
    for (int i = 0; i < 4; i++) {
      const float off = 10 * sinf(t * 0.001f + i);
      s.drawWideLine(CX - 70 + off, CY - 36 + i * 24, CX + 70 + off, CY - 36 + i * 24, 5, hex(0xaab4c4));
    }
  }
  const bool rain = !strcmp(in.icon, "rain") || !strcmp(in.icon, "storm"), snow = !strcmp(in.icon, "snow");
  if (rain || snow) {
    if (randf(0, 1) < 0.5f)
      if (Particle* p = spawn(e)) *p = {randf(cx - 50, cx + 50), CY + 52, snow ? 1.2f : 3.6f, 0, 1, true};
    for (auto& p : parts[e]) {
      if (!p.on) continue;
      p.y += p.v;
      if (p.y > 230) p.on = false;
      else if (snow) s.fillSmoothCircle(p.x + 3 * sinf(p.y / 9), p.y, 3, hex(0xffffff));
      else s.drawWideLine(p.x, p.y, p.x - 3, p.y + 9, 1.5f, hex(0x6ab4ff));
    }
  }
  if (!strcmp(in.icon, "storm") && (t / 120) % 17 < 2) {  // éclair
    s.fillTriangle(cx, CY + 40, cx + 18, CY + 40, cx - 8, CY + 90, hex(0xffe066));
    s.fillTriangle(cx - 2, CY + 62, cx + 16, CY + 62, cx + 4, CY + 110, hex(0xffe066));
  }
}

void music(Sprite& s, int e, uint32_t t) {
  s.fillScreen(hex(0x0b0710));
  if (e == 0) {  // vinyle
    s.fillSmoothCircle(CX, CY, 104, hex(0x16141a));
    for (int r = 46; r < 102; r += 5) s.drawCircle(CX, CY, r, hex(0x2a2730));
    const float a = t * 0.229f;  // degrés
    s.fillArc(CX, CY, 74, 86, a, a + 28, hex(0x3a3640));
    s.fillArc(CX, CY, 74, 86, a + 180, a + 208, hex(0x3a3640));
    s.fillSmoothCircle(CX, CY, 40, hex(0xe0406a));
    const float m = t * 0.004f;
    s.fillSmoothCircle(CX + cosf(m) * 26, CY + sinf(m) * 26, 5, hex(0xffffff));
    s.fillSmoothCircle(CX, CY, 5, hex(0x0b0710));
    return;
  }
  for (int i = 0; i < 32; i++) {  // égaliseur circulaire
    const float a = i / 32.f * TAU - TAU / 4;
    const float v = 0.25f + 0.75f * fabsf(sinf(t * 0.006f + i * 0.7f) * sinf(t * 0.0023f + i * 1.3f));
    const float r0 = 46, r1 = r0 + 60 * v;
    s.drawWideLine(CX + cosf(a) * r0, CY + sinf(a) * r0, CX + cosf(a) * r1, CY + sinf(a) * r1, 3,
                   hsl(300 + i * 4, 0.9f, 0.55f + v * 0.15f));
  }
  // Note de musique
  s.fillSmoothCircle(CX - 8, CY + 12, 9, hex(0xe8e6e1));
  s.fillRect(CX - 1, CY - 22, 4, 34, hex(0xe8e6e1));
  s.fillTriangle(CX + 3, CY - 22, CX + 16, CY - 12, CX + 3, CY - 8, hex(0xe8e6e1));
}

void thinking(Sprite& s, int e, uint32_t t) {
  s.fillScreen(hex(0x05060a));
  static const uint32_t COLORS[] = {0x7fd3e6, 0xb58cff, 0xff8fb1};
  static const uint32_t DIM[] = {0x1c3a42, 0x2c2242, 0x42262f};
  for (int i = 0; i < 3; i++) {
    const float r = 34 + i * 30;
    const float a = t * (0.003f - i * 0.0007f) * (i % 2 ? -1 : 1) + e * PI;
    s.drawCircle(CX, CY, r, hex(0x12161e));
    const float x = CX + cosf(a) * r, y = CY + sinf(a) * r;
    s.fillSmoothCircle(x, y, 15 - i, hex(DIM[i]));
    s.fillSmoothCircle(x, y, 9 - i, hex(COLORS[i]));
  }
  s.fillSmoothCircle(CX, CY, 10 + 3 * sinf(t * 0.008f), hex(0xe8e6e1));
}

void timer(Sprite& s, int e, uint32_t t, const SceneInfo& in) {
  s.fillScreen(hex(0x0d0605));
  const uint32_t total = in.timerMs ? in.timerMs : 25 * 60000;
  const uint32_t left = t < total ? total - t : 0;
  s.fillArc(CX, CY, 97, 111, 0, 360, hex(0x2a1410));
  if (left) s.fillArc(CX, CY, 97, 111, -90, -90 + 360.f * left / total, hex(0xff6a3d));
  if (e == 0) {
    text(s, in.label[0] ? in.label : "FOCUS", CX, CY - 8, &fonts::DejaVu24, hex(0xff6a3d));
    text(s, "minuteur", CX, CY + 22, &fonts::DejaVu18, hex(0x9a9ca6));
  } else {
    char mmss[8];
    const uint32_t sec = (left + 999) / 1000;
    snprintf(mmss, sizeof(mmss), "%lu:%02lu", (unsigned long)(sec / 60), (unsigned long)(sec % 60));
    text(s, mmss, CX, CY + 4, &fonts::DejaVu56, hex(0xe8e6e1));
  }
}

void sleeping(Sprite& s, int e, uint32_t t) {
  s.fillScreen(hex(0x03040a));
  const int y = CY - 30 + 3 * sinf(t * 0.002f);
  s.fillArc(CX, y, 49, 56, 45, 135, hex(0x5c6aa0));
  s.fillSmoothCircle(CX + cosf(45 * DEG_TO_RAD) * 52.5f, y + sinf(45 * DEG_TO_RAD) * 52.5f, 3.5f, hex(0x5c6aa0));
  s.fillSmoothCircle(CX + cosf(135 * DEG_TO_RAD) * 52.5f, y + sinf(135 * DEG_TO_RAD) * 52.5f, 3.5f, hex(0x5c6aa0));
  if (e == 1 && randf(0, 1) < 0.025f)
    if (Particle* p = spawn(e)) *p = {CX + 20.f, CY - 10.f, 0.6f, 18, 1, true};
  for (auto& p : parts[e]) {
    if (!p.on) continue;
    p.y -= p.v;
    p.x += 0.35f;
    p.size += 0.08f;
    p.life -= 0.004f;
    if (p.life <= 0) { p.on = false; continue; }
    const lgfx::IFont* f = p.size < 24 ? &fonts::DejaVu18 : p.size < 34 ? &fonts::DejaVu24 : &fonts::DejaVu40;
    text(s, "Z", p.x, p.y, f, mix(0x03040a, 0xa0afff, p.life));
  }
}

void message(Sprite& s, int e, uint32_t t, const SceneInfo& in) {
  s.fillScreen(hex(0x060a10));
  if (e == 0) {
    const int b = -fabsf(sinf(t * 0.006f)) * 14;
    s.fillEllipse(CX, CY + 66, 50 + b, 7, hex(0x14262e));
    s.fillSmoothRoundRect(CX - 58, CY - 38 + b, 116, 78, 10, hex(0xe8e6e1));
    s.drawWideLine(CX - 54, CY - 32 + b, CX, CY + 8 + b, 2.5f, hex(0x8a93a6));
    s.drawWideLine(CX + 54, CY - 32 + b, CX, CY + 8 + b, 2.5f, hex(0x8a93a6));
    return;
  }
  const float k = 1 + 0.06f * sinf(t * 0.01f);
  s.fillSmoothCircle(CX, CY - 8, 72 * k, hex(0x3a0c16));
  s.fillSmoothCircle(CX, CY - 8, 62 * k, hex(0xff3b5c));
  char n[6];
  snprintf(n, sizeof(n), "%d", in.count > 0 ? in.count : 1);
  text(s, n, CX, CY - 6, &fonts::DejaVu72, hex(0xffffff));
  text(s, in.label[0] ? in.label : "nouveaux messages", CX, CY + 80, &fonts::DejaVu18, hex(0x9a9ca6));
}

// Spirale hypnotique : calcul par pixel sur la carte polaire, très rapide.
void dizzy(Sprite& s, int e, uint32_t t) {
  if (!polarMap) {
    polarMap = (uint16_t*)heap_caps_malloc(240 * 240 * 2, MALLOC_CAP_SPIRAM);
    if (!polarMap) return;
    for (int y = 0; y < 240; y++)
      for (int x = 0; x < 240; x++) {
        const float dx = x - CX + 0.5f, dy = y - CY + 0.5f;
        const int r = min(255, (int)sqrtf(dx * dx + dy * dy));
        const int a = (int)((atan2f(dy, dx) / TAU + 0.5f) * 256) & 255;
        polarMap[y * 240 + x] = (r << 8) | a;
      }
  }
  uint16_t* buf = (uint16_t*)s.getBuffer();
  const uint16_t ink = hex(0x1a1a22), paper = hex(0xf2efe6);
  const uint16_t inkSw = (ink >> 8) | (ink << 8), paperSw = (paper >> 8) | (paper << 8);
  const int rot = (int)(t * 0.25f) * (e ? -1 : 1);
  for (int i = 0; i < 240 * 240; i++) {
    const int p = polarMap[i], r = p >> 8, a = p & 255;
    // Bras de spirale d'Archimède : phase = rayon - angle (en 1/256 de tour).
    const int phase = (r * 256 / 20 - a + rot) & 255;
    buf[i] = (phase < 80 && r > 3) ? inkSw : paperSw;
  }
}

void matrix(Sprite& s, int e) {
  s.fillScreen(hex(0x000000));
  s.setFont(&fonts::Font2);
  s.setTextDatum(textdatum_t::middle_center);
  for (int i = 0; i < 20; i++) {
    Column& c = cols[e][i];
    c.y += c.v * 1.6f;
    if (c.y > 260 + 12 * 14) {
      c.y = randf(-80, 0);
      c.v = randf(1.5f, 4.f);
    }
    if (randf(0, 1) < 0.15f) c.ch[esp_random() % 12] = "0123456789ABCDEFOPAL"[esp_random() % 20];
    for (int k = 0; k < 12; k++) {
      const int y = c.y - k * 14;
      if (y < -10 || y > 250) continue;
      s.setTextColor(k == 0 ? hex(0xc8ffd8) : mix(0x000000, 0x1fdc6a, 1 - k / 12.f));
      const char str[2] = {c.ch[k], 0};
      s.drawString(str, 6 + i * 12, y);
    }
  }
}

void boot(Sprite& s, int e, uint32_t t) {
  s.fillScreen(hex(0x05060a));
  const float p = (t % 4000) / 4000.f, fill = smooth(0, 0.7f, p);
  s.fillArc(CX, CY, 97, 103, 0, 360, hex(0x141820));
  const int segs = (int)(72 * fill);
  for (int i = 0; i < segs; i++) {  // dégradé bleu → turquoise → violet
    const float k = i / 72.f;
    const uint16_t c = k < 0.5f ? mix(0x4a8cf0, 0x3cd6c8, k * 2) : mix(0x3cd6c8, 0xb05cf0, k * 2 - 1);
    s.fillArc(CX, CY, 97, 103, -90 + i * 5, -90 + i * 5 + 5.5f, c);
  }
  text(s, e ? "BOX" : "OPAL", CX, CY, &fonts::DejaVu40, mix(0x05060a, 0xe8e6e1, smooth(0.65f, 0.85f, p)));
}

}  // namespace

void draw(int e, Sprite& s, Scene scene, const SceneInfo& info, uint32_t now, uint32_t start) {
  if (partsFor[e] != start) {  // nouvelle animation : particules remises à zéro
    partsFor[e] = start;
    reset(e, scene);
  }
  const uint32_t t = now - start;
  switch (scene) {
    case Scene::Hearts: hearts(s, e, t); break;
    case Scene::Clock: clock(s, e, now); break;
    case Scene::Weather: weather(s, e, t, info); break;
    case Scene::Music: music(s, e, t); break;
    case Scene::Thinking: thinking(s, e, t); break;
    case Scene::Timer: timer(s, e, t, info); break;
    case Scene::Sleep: sleeping(s, e, t); break;
    case Scene::Message: message(s, e, t, info); break;
    case Scene::Dizzy: dizzy(s, e, t); break;
    case Scene::Matrix: matrix(s, e); break;
    case Scene::Boot: boot(s, e, t); break;
    default: s.fillScreen(0); break;
  }
}

}  // namespace scenes
}  // namespace eyes
