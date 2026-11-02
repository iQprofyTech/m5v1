#pragma once

#include <Arduino.h>
#include <vector>

// Screen layout (240x135, landscape):
//   status bar  y 0..13
//   log / menu  y 15..117 (sprite, scrollable)
//   input line  y 119..134

void uiInit();
void uiStatusBar(const String& left, const String& right, uint16_t leftColor);

void uiLogAdd(uint16_t color, const String& text);
void uiLogClear();
void uiLogScroll(int lines);  // positive = back in history
void uiDrawLog();

void uiDrawInput(const String& text, uint16_t color);
void uiDrawLevel(float level, uint32_t elapsedMs);  // recording indicator in the input line

void uiDrawMenu(const String& title, const std::vector<String>& items, int selected);
void uiDrawLines(const String& title, const std::vector<String>& lines);
