#include "ui.h"

#include <M5Cardputer.h>

#include <deque>

static constexpr int kW = 240;
static constexpr int kBarH = 14;
static constexpr int kLogY = 15;
static constexpr int kLogH = 103;
static constexpr int kInputY = 119;
static constexpr int kInputH = 16;
static constexpr size_t kMaxLines = 160;

// efont covers Latin and Cyrillic, so Russian replies render correctly.
static const lgfx::IFont* kFont = &fonts::efontJA_12;

// No parent display here: M5Cardputer.Display is not bound yet during static initialisation
// (a canvas built with it as parent crashed in pushSprite), so the target is passed at each push.
static M5Canvas canvas;

static void pushLog() { canvas.pushSprite(&M5Cardputer.Display, 0, kLogY); }

struct Line {
  uint16_t color;
  String text;
};
static std::deque<Line> lines;
static int scrollBack = 0;

static size_t utf8Len(uint8_t lead) {
  if (lead < 0x80) return 1;
  if ((lead >> 5) == 0x06) return 2;
  if ((lead >> 4) == 0x0E) return 3;
  if ((lead >> 3) == 0x1E) return 4;
  return 1;
}

static int textWidth(const String& s) { return canvas.textWidth(s); }

// Word-wraps one paragraph to the log width, breaking long words between UTF-8 code points.
static void wrapParagraph(const String& para, int maxW, std::vector<String>& out) {
  String line, word;
  auto flushWord = [&]() {
    if (word.isEmpty()) return;
    String candidate = line.isEmpty() ? word : line + " " + word;
    if (textWidth(candidate) <= maxW) {
      line = candidate;
    } else {
      if (!line.isEmpty()) out.push_back(line);
      line = "";
      // The word alone may still be too wide: split it by characters.
      String piece;
      for (size_t i = 0; i < word.length();) {
        size_t n = utf8Len(word[i]);
        String ch = word.substring(i, i + n);
        if (textWidth(piece + ch) > maxW && !piece.isEmpty()) {
          out.push_back(piece);
          piece = "";
        }
        piece += ch;
        i += n;
      }
      line = piece;
    }
    word = "";
  };
  for (size_t i = 0; i < para.length(); ++i) {
    char c = para[i];
    if (c == ' ' || c == '\t') {
      flushWord();
    } else {
      word += c;
    }
  }
  flushWord();
  out.push_back(line);
}

void uiInit() {
  auto& d = M5Cardputer.Display;
  d.setRotation(1);
  d.setBrightness(110);
  d.fillScreen(TFT_BLACK);
  d.setFont(kFont);
  canvas.setColorDepth(8);
  canvas.createSprite(kW, kLogH);
  canvas.setFont(kFont);
}

void uiStatusBar(const String& left, const String& right, uint16_t leftColor) {
  auto& d = M5Cardputer.Display;
  d.fillRect(0, 0, kW, kBarH, 0x18E3);
  d.setFont(kFont);
  d.setTextColor(leftColor, 0x18E3);
  d.setTextDatum(top_left);
  d.drawString(left, 2, 1);
  d.setTextColor(TFT_LIGHTGREY, 0x18E3);
  d.setTextDatum(top_right);
  d.drawString(right, kW - 2, 1);
  d.setTextDatum(top_left);
}

void uiLogAdd(uint16_t color, const String& text) {
  std::vector<String> wrapped;
  int start = 0;
  while (true) {
    int nl = text.indexOf('\n', start);
    String para = nl < 0 ? text.substring(start) : text.substring(start, nl);
    para.replace("\r", "");
    wrapParagraph(para, kW - 4, wrapped);
    if (nl < 0) break;
    start = nl + 1;
  }
  for (auto& l : wrapped) lines.push_back({color, l});
  while (lines.size() > kMaxLines) lines.pop_front();
  scrollBack = 0;
  uiDrawLog();
}

void uiLogClear() {
  lines.clear();
  scrollBack = 0;
  uiDrawLog();
}

void uiLogScroll(int delta) {
  int visible = kLogH / canvas.fontHeight();
  int maxBack = std::max<int>(0, lines.size() - visible);
  scrollBack = std::min(maxBack, std::max(0, scrollBack + delta));
  uiDrawLog();
}

void uiDrawLog() {
  int lh = canvas.fontHeight();
  int visible = kLogH / lh;
  canvas.fillSprite(TFT_BLACK);
  int end = static_cast<int>(lines.size()) - scrollBack;
  int begin = std::max(0, end - visible);
  int y = 0;
  for (int i = begin; i < end; ++i) {
    canvas.setTextColor(lines[i].color, TFT_BLACK);
    canvas.drawString(lines[i].text, 2, y);
    y += lh;
  }
  if (scrollBack > 0) {
    canvas.fillTriangle(kW - 8, kLogH - 2, kW - 2, kLogH - 2, kW - 5, kLogH - 7, TFT_YELLOW);
  }
  pushLog();
}

void uiDrawInput(const String& text, uint16_t color) {
  auto& d = M5Cardputer.Display;
  d.fillRect(0, kInputY - 1, kW, kInputH + 1, TFT_BLACK);
  d.drawFastHLine(0, kInputY - 1, kW, 0x39E7);
  d.setFont(kFont);
  d.setTextColor(color, TFT_BLACK);
  // Keep the tail of long input visible.
  String shown = text;
  while (shown.length() > 0 && d.textWidth(shown) > kW - 4) {
    shown = shown.substring(utf8Len(shown[0]));
  }
  d.drawString(shown, 2, kInputY + 2);
}

void uiDrawLevel(float level, uint32_t elapsedMs) {
  auto& d = M5Cardputer.Display;
  d.fillRect(0, kInputY, kW, kInputH, TFT_BLACK);
  d.fillCircle(8, kInputY + 8, 5, TFT_RED);
  d.setFont(kFont);
  d.setTextColor(TFT_WHITE, TFT_BLACK);
  char buf[16];
  snprintf(buf, sizeof(buf), "%lu.%lus", elapsedMs / 1000, (elapsedMs / 100) % 10);
  d.drawString(buf, 18, kInputY + 2);
  int barX = 64, barW = kW - barX - 4;
  int fill = static_cast<int>(std::min(1.0f, level) * barW);
  d.drawRect(barX, kInputY + 3, barW, 10, TFT_DARKGREY);
  d.fillRect(barX + 1, kInputY + 4, fill > 2 ? fill - 2 : 0, 8, level > 0.8f ? TFT_ORANGE : TFT_GREEN);
}

void uiDrawMenu(const String& title, const std::vector<String>& items, int selected) {
  int lh = canvas.fontHeight();
  int visible = kLogH / lh - 1;
  int first = std::max(0, std::min(selected - visible / 2, static_cast<int>(items.size()) - visible));
  canvas.fillSprite(TFT_BLACK);
  canvas.setTextColor(TFT_YELLOW, TFT_BLACK);
  canvas.drawString(title, 2, 0);
  int y = lh;
  for (int i = first; i < static_cast<int>(items.size()) && i < first + visible; ++i) {
    bool sel = i == selected;
    if (sel) canvas.fillRect(0, y, kW, lh, 0x2945);
    canvas.setTextColor(sel ? TFT_WHITE : TFT_LIGHTGREY, sel ? 0x2945 : TFT_BLACK);
    canvas.drawString(items[i], 4, y);
    y += lh;
  }
  pushLog();
}

void uiDrawLines(const String& title, const std::vector<String>& text) {
  int lh = canvas.fontHeight();
  canvas.fillSprite(TFT_BLACK);
  canvas.setTextColor(TFT_YELLOW, TFT_BLACK);
  canvas.drawString(title, 2, 0);
  canvas.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  int y = lh;
  for (const auto& l : text) {
    if (y + lh > kLogH) break;
    canvas.drawString(l, 2, y);
    y += lh;
  }
  pushLog();
}
