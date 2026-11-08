// m5v1: voice / keyboard remote for a PC, running on M5Stack Cardputer-Adv.
//
// Modes (Tab, or Fn+Tab in KEYS): CLAUDE agent, CODEX agent, WINDOW (a chosen PC window,
// e.g. an open chat), KEYS (the Cardputer is the PC's keyboard and mouse).
//
//   G0 (hold)        push-to-talk          Fn+Space   start/stop recording
//   type + Enter     send text             Fn+O       choose PC window
//   Fn+P             quick PC commands     Fn+; / .   scroll up / down
//   Fn+N             new agent session     Fn+L       pick agent session
//   Fn+W             Wi-Fi networks        Fn+R       reconnect
//   Fn+` (Esc)       cancel current job    Fn+H       help

#include <ArduinoJson.h>
#include <M5Cardputer.h>
#include <WiFi.h>

#include "app_config.h"
#include "net.h"
#include "ui.h"

// --- audio ------------------------------------------------------------------

static constexpr uint32_t kSampleRate = 16000;
static constexpr size_t kChunk = 1600;  // 100 ms
static constexpr uint32_t kMaxRecordMs = 30000;
static constexpr uint32_t kMinRecordMs = 400;
static int16_t recBuf[3][kChunk];  // two queued in the mic driver + one being sent

static bool recording = false;
static bool recPushToTalk = false;
static size_t recCount = 0;  // chunks handed to the mic driver
static size_t recSent = 0;   // chunks uploaded
static uint32_t recStart = 0;
static VoiceUpload upload;

// --- app state --------------------------------------------------------------

enum class Screen { Main, Wifi, WifiPass, Sessions, Windows, Commands, Help };
static Screen screen = Screen::Main;

static String input;
static String jobId;
static bool jobTranscriptShown = false;
static uint32_t jobLastPoll = 0;
static int jobPollErrors = 0;
static String jobLastState;
static int jobStepsShown = 0;  // agent tool calls already printed to the log
static String jobActivity;     // latest agent tool call, shown in the input line

static String netStatus = "--";
static uint32_t lastStatusDraw = 0;
static uint32_t lastWifiCheck = 0;
static uint32_t retryMs = 10000;  // automatic reconnect interval, grows while nothing is reachable
static bool keysPending = false;  // a key press interrupted a blocking connect; handle it next
static bool abortArmed = false;
static bool reconnectPending = false;

static std::vector<String> menuItems;
static std::vector<String> menuIds;  // ssid or session id per item
static int menuSel = 0;
static String passSsid;

static String serialLine;

static String winTitle;  // PC window chosen for WINDOW mode

static const char* modeName() {
  switch (gCfg.mode) {
    case Mode::Codex: return "CODEX";
    case Mode::Window: return "ОКНО";
    case Mode::Keys: return "КЛАВ";
    default: return "CLAUDE";
  }
}

// Mode name in the bridge API. Voice in KEYS mode is dictation typed into the active window.
static const char* modeApi() {
  switch (gCfg.mode) {
    case Mode::Codex: return "codex";
    case Mode::Window: return "window";
    case Mode::Keys: return "type";
    default: return "claude";
  }
}

static const char* agentApi() { return gCfg.mode == Mode::Codex ? "codex" : "claude"; }

static const char* modeHint() {
  switch (gCfg.mode) {
    case Mode::Codex: return "Режим: агент Codex";
    case Mode::Window: return "Режим: окно ПК (Fn+O - выбрать окно)";
    case Mode::Keys: return "Режим: клавиатура и мышь ПК, голос печатается на ПК (Fn+H)";
    default: return "Режим: агент Claude Code";
  }
}

static void drawStatus() {
  String left = String(modeName()) + " ";
  left += WiFi.isConnected() ? WiFi.SSID().substring(0, 10) : String("no wifi");
  left += " " + (gServer.valid ? gServer.label : String("--"));
  if (!jobId.isEmpty()) left += " *";
  String right = String(M5Cardputer.Power.getBatteryLevel()) + "%";
  uint16_t color = gServer.valid ? TFT_GREEN : (WiFi.isConnected() ? TFT_YELLOW : TFT_RED);
  uiStatusBar(left, right, color);
  lastStatusDraw = millis();
}

static void drawIdleInput() {
  if (recording) return;
  if (!jobId.isEmpty()) {
    if (jobLastState == "confirm") {
      uiDrawInput("Enter - выполнить, Esc - отмена", TFT_YELLOW);
    } else if (jobLastState == "approve") {
      uiDrawInput("Enter-да  A-всегда  Esc-нет", TFT_YELLOW);
    } else if (jobLastState == "transcribing") {
      uiDrawInput("Распознаю речь...", TFT_CYAN);
    } else if (!jobActivity.isEmpty()) {
      uiDrawInput("> " + jobActivity, TFT_CYAN);
    } else {
      uiDrawInput("Работаю... (Esc - отмена)", TFT_CYAN);
    }
  } else if (!gServer.valid) {
    uiDrawInput("> " + netStatus, TFT_DARKGREY);
  } else if (gCfg.mode == Mode::Keys) {
    uiDrawInput("> клавиши идут на ПК, G0 - диктовка", TFT_DARKGREY);
  } else if (input.isEmpty() && gCfg.mode == Mode::Window) {
    uiDrawInput(winTitle.isEmpty() ? "> Fn+O - выбрать окно" : "> в окно: " + winTitle, TFT_DARKGREY);
  } else if (input.isEmpty()) {
    uiDrawInput("> держи G0 и говори, или пиши", TFT_DARKGREY);
  } else {
    uiDrawInput("> " + input + "_", TFT_WHITE);
  }
}

static void showHelp() {
  screen = Screen::Help;
  if (gCfg.mode == Mode::Keys) {
    uiDrawLines("КЛАВ: клавиши ПК (любая - назад)", {
        "Tab - режим! Opt+Tab - Tab на ПК",
        "буквы/Enter/Del, Ctrl/Alt+буква",
        "Fn+; . , / стрелки, Fn+` Esc",
        "Opt+; . , / мышь, Opt+Enter клик",
        "Opt+Space прав., Opt+[ ] скролл",
        "G0 - диктовка в окно ПК",
    });
    return;
  }
  uiDrawLines("Клавиши (любая - назад)", {
      "Tab - CLAUDE/CODEX/ОКНО/КЛАВ",
      "G0 / Fn+Space - голос, Enter - ок",
      "Fn+O окно ПК, Fn+P команды ПК",
      "Запрос агента: Enter/A/Esc",
      "Fn+N/Fn+L сессии, Fn+; . скролл",
      "Fn+W Wi-Fi, Fn+R, Fn+` отмена",
  });
}

static void backToMain() {
  screen = Screen::Main;
  uiDrawLog();
  drawIdleInput();
  drawStatus();
}

// --- sound ------------------------------------------------------------------

// Short tones so the device can stay in a pocket: the agent needs an answer, or finished.
// The speaker shares the codec with the mic, so it is only switched on for the beep.
static void beep(uint16_t freq, uint32_t ms, int times = 1) {
  if (recording) return;
  M5Cardputer.Speaker.begin();
  M5Cardputer.Speaker.setVolume(90);
  for (int i = 0; i < times; ++i) {
    M5Cardputer.Speaker.tone(freq, ms);
    delay(ms + 60);
  }
  M5Cardputer.Speaker.end();
}

// --- connection -------------------------------------------------------------

// Polled while Wi-Fi connects (up to 15 s per network): a new key press or G0 stops the
// attempt so the keyboard never goes dead when the saved network is out of range.
// Keys still held from the command that started the connect (Fn+R) do not count.
static bool keyAbort() {
  M5Cardputer.update();
  bool pressed = M5Cardputer.Keyboard.isPressed() || M5Cardputer.BtnA.isPressed();
  if (!pressed) {
    abortArmed = true;
    return false;
  }
  if (!abortArmed) return false;
  keysPending = true;
  return true;
}

static void connectAll() {
  uiDrawInput("Подключаюсь к Wi-Fi... (клавиша - стоп)", TFT_YELLOW);
  String wifiStatus;
  gServer = Endpoint();
  abortArmed = false;
  if (!wifiConnect(wifiStatus, 15000, keyAbort)) {
    netStatus = wifiStatus;
    uiLogAdd(TFT_RED, wifiStatus);
    drawStatus();
    drawIdleInput();
    return;
  }
  uiLogAdd(TFT_DARKGREY, "Wi-Fi: " + wifiStatus + " " + WiFi.localIP().toString());
  if (gCfg.token.isEmpty()) {
    netStatus = "нет токена";
    uiLogAdd(TFT_RED, "Токен моста не задан: tools/provision.py");
    drawStatus();
    drawIdleInput();
    return;
  }
  uiDrawInput("Ищу мост...", TFT_YELLOW);
  int code = resolveServer(netStatus);
  uiLogAdd(code == 200 ? TFT_GREEN : TFT_RED, "Мост: " + netStatus);
  drawStatus();
  drawIdleInput();
}

// --- jobs -------------------------------------------------------------------

static void startJob(const String& response) {
  JsonDocument doc;
  if (deserializeJson(doc, response) != DeserializationError::Ok || !doc["job"].is<const char*>()) {
    uiLogAdd(TFT_RED, "Ошибка ответа моста");
    return;
  }
  jobId = doc["job"].as<String>();
  jobLastPoll = millis();
  jobPollErrors = 0;
  jobLastState = "";
  jobStepsShown = 0;
  jobActivity = "";
  drawStatus();
  drawIdleInput();
}

static void sendText(const String& text) {
  if (!gServer.valid) {
    uiLogAdd(TFT_RED, "Нет связи с мостом (Fn+R)");
    return;
  }
  JsonDocument doc;
  doc["text"] = text;
  doc["mode"] = modeApi();
  String body, resp;
  serializeJson(doc, body);
  uiLogAdd(TFT_CYAN, "Вы: " + text);
  jobTranscriptShown = true;
  int code = apiRequest("POST", "/api/text", body, resp);
  if (code == 200) {
    startJob(resp);
  } else {
    uiLogAdd(TFT_RED, "Ошибка отправки: " + String(code));
  }
}

static void pollJob() {
  String resp;
  int code = apiRequest("GET", "/api/jobs/" + jobId, "", resp, 5000);
  jobLastPoll = millis();
  if (code != 200) {
    if (code == 404 || ++jobPollErrors > 30) {
      uiLogAdd(TFT_RED, "Связь с задачей потеряна (" + String(code) + ")");
      jobId = "";
      drawStatus();
      drawIdleInput();
    }
    return;
  }
  jobPollErrors = 0;
  JsonDocument doc;
  if (deserializeJson(doc, resp) != DeserializationError::Ok) return;
  String state = doc["state"].as<String>();
  String transcript = doc["transcript"].as<String>();
  if (!jobTranscriptShown && !transcript.isEmpty()) {
    uiLogAdd(TFT_CYAN, "Вы: " + transcript);
    jobTranscriptShown = true;
  }
  // The bridge sends only the latest steps; print the ones not shown yet.
  int stepCount = doc["step_count"] | 0;
  JsonArray steps = doc["steps"].as<JsonArray>();
  int firstIndex = stepCount - static_cast<int>(steps.size());
  bool redraw = state != jobLastState;
  for (int i = std::max(jobStepsShown, firstIndex); i < stepCount; ++i) {
    jobActivity = steps[i - firstIndex].as<String>();
    uiLogAdd(TFT_DARKGREY, "* " + jobActivity);
    redraw = true;
  }
  jobStepsShown = std::max(jobStepsShown, stepCount);
  if (state == "approve" && jobLastState != "approve") {
    uiLogAdd(TFT_YELLOW, "? Разрешить: " + doc["approval"].as<String>());
    beep(1760, 70, 2);
  } else if (state == "confirm" && jobLastState != "confirm") {
    beep(1320, 60);
  }
  if (redraw) {
    jobLastState = state;
    drawIdleInput();
  }
  if (state == "done") {
    uiLogAdd(TFT_WHITE, doc["reply"].as<String>());
    beep(1047, 90);
  } else if (state == "error") {
    uiLogAdd(TFT_RED, "Ошибка: " + doc["error"].as<String>());
    beep(330, 250);
  } else if (state == "cancelled") {
    uiLogAdd(TFT_ORANGE, "Отменено");
  } else {
    return;
  }
  jobId = "";
  drawStatus();
  drawIdleInput();
}

static void confirmJob() {
  String resp;
  int code = apiRequest("POST", "/api/jobs/" + jobId + "/confirm", "", resp);
  if (code == 200) {
    jobLastState = "running";
    drawIdleInput();
  } else {
    uiLogAdd(TFT_RED, "Не удалось подтвердить: " + String(code));
  }
}

// Answer the agent's permission question: allow once, allow this tool until the bridge
// restarts (always), or deny.
static void approveJob(bool allow, bool always) {
  JsonDocument doc;
  doc["allow"] = allow;
  doc["always"] = always;
  String body, resp;
  serializeJson(doc, body);
  int code = apiRequest("POST", "/api/jobs/" + jobId + "/approve", body, resp);
  if (code == 200) {
    uiLogAdd(allow ? TFT_GREEN : TFT_ORANGE, allow ? (always ? "Разрешено всегда" : "Разрешено") : "Запрещено");
    jobLastState = "running";
    drawIdleInput();
  } else {
    uiLogAdd(TFT_RED, "Не удалось ответить: " + String(code));
  }
}

static void cancelJob() {
  if (jobId.isEmpty()) return;
  String resp;
  apiRequest("POST", "/api/jobs/" + jobId + "/cancel", "", resp);
  uiDrawInput("Отменяю...", TFT_ORANGE);
}

// --- recording --------------------------------------------------------------

// The mic's I2S WS (GPIO43) is also UART0 TX. Without resetting GPIO43/46 first, every
// sample read the same value (-8) on Cardputer-Adv; with the reset the codec's data arrives.
static bool micBegin() {
  M5Cardputer.Speaker.end();  // speaker and mic share the codec
  gpio_reset_pin(GPIO_NUM_43);
  gpio_reset_pin(GPIO_NUM_46);
  delay(5);
  return M5Cardputer.Mic.begin();
}

static float chunkLevel(const int16_t* data) {
  int peak = 0;
  for (size_t i = 0; i < kChunk; ++i) peak = std::max(peak, abs(static_cast<int>(data[i])));
  return peak / 12000.0f;
}

static void startRecording(bool pushToTalk) {
  if (!jobId.isEmpty()) {
    uiLogAdd(TFT_ORANGE, "Подожди ответа или Esc");
    return;
  }
  if (!gServer.valid) {
    uiLogAdd(TFT_RED, "Нет связи с мостом (Fn+R)");
    return;
  }
  String query = String("?mode=") + modeApi();
  if (!upload.begin(query, kSampleRate)) {
    uiLogAdd(TFT_RED, "Не удалось открыть соединение");
    return;
  }
  micBegin();
  recording = true;
  recPushToTalk = pushToTalk;
  recCount = recSent = 0;
  recStart = millis();
  uiDrawLevel(0, 0);
}

static void sendChunk(size_t index) {
  const int16_t* data = recBuf[index % 3];
  if (!upload.write(reinterpret_cast<const uint8_t*>(data), kChunk * sizeof(int16_t))) return;
  recSent = index + 1;
  uiDrawLevel(chunkLevel(data), millis() - recStart);
}

static void pumpRecording() {
  // record() blocks until one of the driver's two slots frees up, so when it returns,
  // the chunk queued two calls ago is complete and can be uploaded.
  if (M5Cardputer.Mic.record(recBuf[recCount % 3], kChunk, kSampleRate)) {
    ++recCount;
    if (recCount >= 3) sendChunk(recCount - 3);
  }
}

static void stopRecording() {
  recording = false;
  uint32_t waitStart = millis();
  while (M5Cardputer.Mic.isRecording() && millis() - waitStart < 500) delay(1);
  M5Cardputer.Mic.end();
  uint32_t duration = millis() - recStart;

  if (!upload.active()) {
    uiLogAdd(TFT_RED, "Связь прервалась при записи");
    drawIdleInput();
    return;
  }
  if (duration < kMinRecordMs) {
    upload.abort();
    uiLogAdd(TFT_DARKGREY, "Слишком коротко - держи G0 пока говоришь");
    drawIdleInput();
    return;
  }
  for (size_t i = recSent; i < recCount; ++i) sendChunk(i);
  uiDrawInput("Отправляю...", TFT_CYAN);
  String resp;
  int code = upload.finish(resp);
  if (code == 200) {
    jobTranscriptShown = false;
    startJob(resp);
  } else {
    uiLogAdd(TFT_RED, "Ошибка загрузки голоса: " + String(code));
    drawIdleInput();
  }
}

// --- menus ------------------------------------------------------------------

static void openWifiMenu() {
  screen = Screen::Wifi;
  uiDrawLines("Wi-Fi", {"Сканирую сети..."});
  int n = WiFi.scanNetworks();
  menuItems.clear();
  menuIds.clear();
  for (int i = 0; i < n && menuItems.size() < 20; ++i) {
    String ssid = WiFi.SSID(i);
    if (ssid.isEmpty()) continue;
    bool dup = false;
    for (auto& s : menuIds) dup |= s == ssid;
    if (dup) continue;
    menuIds.push_back(ssid);
    menuItems.push_back(String(cfgFindWifi(ssid) ? "* " : "  ") + ssid + " (" + WiFi.RSSI(i) + ")");
  }
  WiFi.scanDelete();
  menuSel = 0;
  uiDrawMenu("Wi-Fi: Enter-выбрать Del-забыть Esc", menuItems, menuSel);
}

static void openPasswordPrompt(const String& ssid) {
  screen = Screen::WifiPass;
  passSsid = ssid;
  const WifiCred* saved = cfgFindWifi(ssid);
  input = saved ? saved->pass : String();
  uiDrawLines("Пароль для " + ssid, {saved ? "Сохранённый пароль ниже" : "Введи пароль",
                                      "Enter - подключиться", "Del - стереть символ",
                                      "Aa+буква - заглавная", "Fn+` (Esc) - отмена"});
  uiDrawInput("> " + input + "_", TFT_WHITE);
}

static void openSessionsMenu() {
  if (!gServer.valid) {
    uiLogAdd(TFT_RED, "Нет связи с мостом");
    return;
  }
  String resp;
  if (apiRequest("GET", String("/api/sessions?agent=") + agentApi(), "", resp) != 200) {
    uiLogAdd(TFT_RED, "Не удалось получить сессии");
    return;
  }
  JsonDocument doc;
  deserializeJson(doc, resp);
  menuItems = {"+ Новая сессия"};
  menuIds = {""};
  for (JsonObject s : doc["sessions"].as<JsonArray>()) {
    menuItems.push_back(String(s["current"] | false ? "> " : "  ") + s["title"].as<String>());
    menuIds.push_back(s["id"].as<String>());
  }
  screen = Screen::Sessions;
  menuSel = 0;
  uiDrawMenu(String("Сессии ") + agentApi() + ": Enter / Esc", menuItems, menuSel);
}

// --- PC windows and commands --------------------------------------------------

static void openWindowsMenu() {
  if (!gServer.valid) {
    uiLogAdd(TFT_RED, "Нет связи с мостом");
    return;
  }
  String resp;
  if (apiRequest("GET", "/api/windows", "", resp) != 200) {
    uiLogAdd(TFT_RED, "Не удалось получить окна ПК");
    return;
  }
  JsonDocument doc;
  deserializeJson(doc, resp);
  long selected = doc["selected"] | 0L;
  menuItems.clear();
  menuIds.clear();
  for (JsonObject w : doc["windows"].as<JsonArray>()) {
    long id = w["id"] | 0L;
    menuItems.push_back(String(id == selected ? "> " : "  ") + w["app"].as<String>() + ": " + w["title"].as<String>());
    menuIds.push_back(String(id));
  }
  screen = Screen::Windows;
  menuSel = 0;
  uiDrawMenu("Окно ПК: Enter / Esc", menuItems, menuSel);
}

static void selectWindow(int index) {
  JsonDocument doc;
  doc["id"] = menuIds[index].toInt();
  String body, resp;
  serializeJson(doc, body);
  int code = apiRequest("POST", "/api/windows/select", body, resp);
  String label = menuItems[index].substring(2);
  backToMain();
  if (code == 200) {
    winTitle = label;
    if (gCfg.mode != Mode::Window && gCfg.mode != Mode::Keys) {
      gCfg.mode = Mode::Window;
      cfgSave();
    }
    uiLogAdd(TFT_GREEN, "Окно: " + label);
    drawStatus();
    drawIdleInput();
  } else {
    uiLogAdd(TFT_RED, "Окно не выбрано: " + String(code));
  }
}

static void openCommandsMenu() {
  if (!gServer.valid) {
    uiLogAdd(TFT_RED, "Нет связи с мостом");
    return;
  }
  String resp;
  if (apiRequest("GET", "/api/commands", "", resp) != 200) {
    uiLogAdd(TFT_RED, "Не удалось получить команды");
    return;
  }
  JsonDocument doc;
  deserializeJson(doc, resp);
  menuItems.clear();
  menuIds.clear();
  for (JsonVariant c : doc["commands"].as<JsonArray>()) {
    menuItems.push_back(c.as<String>());
    menuIds.push_back(c.as<String>());
  }
  screen = Screen::Commands;
  menuSel = 0;
  uiDrawMenu("Команды ПК: Enter / Esc", menuItems, menuSel);
}

static void runCommand(int index) {
  JsonDocument doc;
  doc["name"] = menuIds[index];
  String body, resp;
  serializeJson(doc, body);
  int code = apiRequest("POST", "/api/commands/run", body, resp);
  if (code == 200) {
    uiDrawMenu("Выполнено: " + menuIds[index], menuItems, menuSel);  // stay for repeats (volume)
  } else {
    uiDrawMenu("Ошибка " + String(code) + ": " + menuIds[index], menuItems, menuSel);
  }
}

// --- KEYS mode: the Cardputer as the PC's keyboard and mouse -------------------

static bool hasChar(const Keyboard_Class::KeysState& k, char c);

static void sendInput(JsonDocument& doc) {
  String body, resp;
  serializeJson(doc, body);
  int code = apiRequest("POST", "/api/input", body, resp, 3000);
  if (code != 200) uiDrawInput("ПК не принял ввод: " + String(code), TFT_RED);
}

static void sendKey(const char* key, bool ctrl = false, bool alt = false, bool shift = false) {
  JsonDocument doc;
  doc["key"] = key;
  JsonArray mods = doc["mods"].to<JsonArray>();
  if (ctrl) mods.add("ctrl");
  if (alt) mods.add("alt");
  if (shift) mods.add("shift");
  sendInput(doc);
}

static void sendMouse(int dx, int dy, const char* click = "", int scroll = 0) {
  JsonDocument doc;
  doc["dx"] = dx;
  doc["dy"] = dy;
  doc["click"] = click;
  doc["scroll"] = scroll;
  sendInput(doc);
}

static void handleKeysMode(const Keyboard_Class::KeysState& k) {
  static constexpr int kStep = 40;
  if (k.opt) {  // mouse; Opt+Tab is the PC's Tab
    if (k.tab) sendKey("tab", false, false, k.shift);
    else if (hasChar(k, ';')) sendMouse(0, -kStep);
    else if (hasChar(k, '.')) sendMouse(0, kStep);
    else if (hasChar(k, ',')) sendMouse(-kStep, 0);
    else if (hasChar(k, '/')) sendMouse(kStep, 0);
    else if (k.enter) sendMouse(0, 0, "left");
    else if (k.space) sendMouse(0, 0, "right");
    else if (hasChar(k, '[')) sendMouse(0, 0, "", 3);
    else if (hasChar(k, ']')) sendMouse(0, 0, "", -3);
    return;
  }
  if (k.enter) return sendKey("enter", k.ctrl, k.alt, k.shift);
  if (k.del) return sendKey("backspace", k.ctrl, k.alt);
  if (k.tab) return sendKey("tab", k.ctrl, k.alt, k.shift);
  if (k.space && (k.ctrl || k.alt)) return sendKey("space", k.ctrl, k.alt);
  if ((k.ctrl || k.alt) && !k.word.empty()) {
    char c = static_cast<char>(tolower(static_cast<unsigned char>(k.word.front())));
    char name[2] = {c, 0};
    return sendKey(name, k.ctrl, k.alt, k.shift);
  }
  String text;
  for (char c : k.word) text += c;
  if (k.space && text.isEmpty()) text = " ";
  if (!text.isEmpty()) {
    JsonDocument doc;
    doc["text"] = text;
    sendInput(doc);
  }
}

static void cycleMode() {
  gCfg.mode = static_cast<Mode>((static_cast<uint8_t>(gCfg.mode) + 1) % kModeCount);
  cfgSave();
  uiLogAdd(TFT_YELLOW, modeHint());
  drawStatus();
  drawIdleInput();
}

static void selectSession(const String& id) {
  JsonDocument doc;
  if (id.isEmpty()) {
    doc["new"] = true;
  } else {
    doc["id"] = id;
  }
  String body, resp;
  serializeJson(doc, body);
  int code = apiRequest("POST", String("/api/sessions/select?agent=") + agentApi(), body, resp);
  backToMain();
  if (code == 200) {
    uiLogAdd(TFT_GREEN, id.isEmpty() ? "Новая сессия агента" : "Сессия выбрана");
  } else {
    uiLogAdd(TFT_RED, "Ошибка выбора сессии: " + String(code));
  }
}

// --- keyboard ---------------------------------------------------------------

static bool hasChar(const Keyboard_Class::KeysState& k, char c) {
  for (char w : k.word) {
    if (w == c) return true;
  }
  return false;
}

static void handleMenuKeys(const Keyboard_Class::KeysState& k) {
  if (hasChar(k, '`')) {
    backToMain();
    return;
  }
  int count = static_cast<int>(menuItems.size());
  if (hasChar(k, ';') && menuSel > 0) --menuSel;
  if (hasChar(k, '.') && menuSel < count - 1) ++menuSel;
  String title = "Wi-Fi: Enter-выбрать Del-забыть Esc";
  if (screen == Screen::Sessions) title = String("Сессии ") + agentApi() + ": Enter / Esc";
  if (screen == Screen::Windows) title = "Окно ПК: Enter / Esc";
  if (screen == Screen::Commands) title = "Команды ПК: Enter / Esc";

  if (k.enter && count > 0) {
    if (screen == Screen::Sessions) {
      selectSession(menuIds[menuSel]);
      return;
    }
    if (screen == Screen::Windows) {
      selectWindow(menuSel);
      return;
    }
    if (screen == Screen::Commands) {
      runCommand(menuSel);
      return;
    }
    // Always ask: a saved network gets its password pre-filled, so Enter alone reconnects
    // and a changed password can be corrected in place.
    openPasswordPrompt(menuIds[menuSel]);
    return;
  }
  if (k.del && screen == Screen::Wifi && count > 0 && cfgFindWifi(menuIds[menuSel])) {
    cfgRemoveWifi(menuIds[menuSel]);
    cfgSave();
    menuItems[menuSel] = "  " + menuIds[menuSel] + " (забыта)";
  }
  uiDrawMenu(title, menuItems, menuSel);
}

static void handlePasswordKeys(const Keyboard_Class::KeysState& k) {
  if (k.fn && hasChar(k, '`')) {
    input = "";
    backToMain();
    return;
  }
  if (k.enter) {
    if (!input.isEmpty() && input.length() < 8) {  // WPA2: 8..63 characters, empty = open network
      uiDrawInput("Пароль - минимум 8 символов", TFT_RED);
      return;
    }
    cfgAddWifi(passSsid, input);
    cfgSave();
    input = "";
    backToMain();
    uiLogAdd(TFT_GREEN, "Сеть " + passSsid + " сохранена");
    reconnectPending = true;
    return;
  }
  if (k.del && !input.isEmpty()) input.remove(input.length() - 1);
  for (char c : k.word) {
    if (input.length() < 63) input += c;
  }
  uiDrawInput("> " + input + "_", TFT_WHITE);
}

static void handleMainKeys(const Keyboard_Class::KeysState& k) {
  bool keys = gCfg.mode == Mode::Keys && jobId.isEmpty() && !recording;
  // Tab always switches the mode, also in KEYS (where Opt+Tab, Ctrl+Tab and Alt+Tab go to the PC),
  // so the device can never get stuck in a mode that forwards every key.
  if (k.tab && !k.ctrl && !k.alt && !k.opt && !recording) {
    cycleMode();
    return;
  }
  if (k.fn && keys) {  // arrows and Esc go to the PC
    if (hasChar(k, ';')) return sendKey("up", k.ctrl, k.alt, k.shift);
    if (hasChar(k, '.')) return sendKey("down", k.ctrl, k.alt, k.shift);
    if (hasChar(k, ',')) return sendKey("left", k.ctrl, k.alt, k.shift);
    if (hasChar(k, '/')) return sendKey("right", k.ctrl, k.alt, k.shift);
    if (hasChar(k, '`')) return sendKey("esc");
    if (k.del) return sendKey("delete");
  }
  if (k.fn) {
    if (k.space) {
      if (recording) {
        stopRecording();
      } else {
        startRecording(false);
      }
    } else if (hasChar(k, ';')) {
      uiLogScroll(3);
    } else if (hasChar(k, '.')) {
      uiLogScroll(-3);
    } else if (hasChar(k, '`')) {
      if (!jobId.isEmpty() && jobLastState == "approve") {
        approveJob(false, false);  // Esc on a question denies it; a second Esc cancels the job
      } else {
        cancelJob();
      }
    } else if (hasChar(k, 'n')) {
      selectSession("");
    } else if (hasChar(k, 'l')) {
      openSessionsMenu();
    } else if (hasChar(k, 'w')) {
      openWifiMenu();
    } else if (hasChar(k, 'r')) {
      connectAll();
    } else if (hasChar(k, 'h')) {
      showHelp();
    } else if (hasChar(k, 'o')) {
      openWindowsMenu();
    } else if (hasChar(k, 'p')) {
      openCommandsMenu();
    }
    return;
  }
  if (recording) {
    if (!recPushToTalk) stopRecording();  // any key ends a toggled recording
    return;
  }
  if (keys) {
    handleKeysMode(k);
    return;
  }
  if (k.tab) {
    cycleMode();
    return;
  }
  if (k.enter && !jobId.isEmpty() && jobLastState == "confirm" && input.isEmpty()) {
    confirmJob();
    return;
  }
  if (!jobId.isEmpty() && jobLastState == "approve" && input.isEmpty()) {
    if (k.enter) {
      approveJob(true, false);
      return;
    }
    if (hasChar(k, 'a') || hasChar(k, 'A')) {
      approveJob(true, true);
      return;
    }
  }
  if (k.enter) {
    String text = input;
    text.trim();
    input = "";
    if (!text.isEmpty()) {
      if (jobId.isEmpty()) {
        sendText(text);
      } else {
        uiLogAdd(TFT_ORANGE, "Подожди ответа или Esc");
      }
    }
    drawIdleInput();
    return;
  }
  if (k.del && !input.isEmpty()) input.remove(input.length() - 1);
  for (char c : k.word) {
    if (input.length() < 300) input += c;
  }
  drawIdleInput();
}

static void handleKeyboard() {
  bool pending = keysPending;
  keysPending = false;
  if (!(M5Cardputer.Keyboard.isChange() || pending) || !M5Cardputer.Keyboard.isPressed()) return;
  Keyboard_Class::KeysState k = M5Cardputer.Keyboard.keysState();
  switch (screen) {
    case Screen::Main:
      handleMainKeys(k);
      break;
    case Screen::Wifi:
    case Screen::Sessions:
    case Screen::Windows:
    case Screen::Commands:
      handleMenuKeys(k);
      break;
    case Screen::WifiPass:
      handlePasswordKeys(k);
      break;
    case Screen::Help:
      backToMain();
      break;
  }
}

// --- USB serial provisioning and diagnostics --------------------------------

static constexpr uint8_t kEs8311Addr = 0x18;

// "MIC [ms]": record from the built-in mic and print raw level statistics.
static void micTest(uint32_t ms) {
  if (!micBegin()) {
    Serial.println("MIC ERR begin failed");
    return;
  }
  int16_t buf[kChunk];
  int32_t peak = 0, minV = 32767, maxV = -32768;
  double sumSq = 0;
  size_t total = 0;
  for (uint32_t t = 0; t < ms; t += 100) {
    if (!M5Cardputer.Mic.record(buf, kChunk, kSampleRate)) continue;
    while (M5Cardputer.Mic.isRecording()) delay(1);
    for (size_t i = 0; i < kChunk; ++i) {
      int32_t v = buf[i];
      peak = std::max<int32_t>(peak, abs(v));
      minV = std::min(minV, v);
      maxV = std::max(maxV, v);
      sumSq += double(v) * v;
    }
    total += kChunk;
  }
  M5Cardputer.Mic.end();
  Serial.printf("MIC board=%d samples=%u peak=%ld min=%ld max=%ld rms=%.1f\n", (int)M5.getBoard(),
                (unsigned)total, (long)peak, (long)minV, (long)maxV, total ? sqrt(sumSq / total) : 0.0);
}

static void esDump() {
  String out = "ES";
  for (uint8_t reg = 0; reg <= 0x45; ++reg) {
    char b[8];
    snprintf(b, sizeof(b), " %02X", M5.In_I2C.readRegister8(kEs8311Addr, reg, 400000));
    out += b;
  }
  Serial.println(out);
}

static void handleSerialLine(const String& line) {
  if (line.startsWith("MIC")) {
    micTest(line.length() > 4 ? line.substring(4).toInt() : 1000);
  } else if (line == "ESR") {
    esDump();
  } else if (line.startsWith("ESW ")) {  // ESW <reg hex> <value hex>
    int sp = line.indexOf(' ', 4);
    uint8_t reg = strtol(line.substring(4, sp).c_str(), nullptr, 16);
    uint8_t val = strtol(line.substring(sp + 1).c_str(), nullptr, 16);
    bool ok = M5.In_I2C.writeRegister8(kEs8311Addr, reg, val, 400000);
    Serial.printf("ESW %02X=%02X %s\n", reg, val, ok ? "OK" : "FAIL");
  } else if (line == "WIFI") {  // reconnect now; progress is printed as "WIFI ..." lines
    reconnectPending = true;
    Serial.println("WIFI reconnect queued");
  } else if (line == "PING") {
    Serial.println("PONG m5v1 " M5V1_VERSION);
  } else if (line == "INFO") {
    Serial.println("INFO " + cfgInfoJson());
  } else if (line.startsWith("CFG ")) {
    String err;
    if (cfgApplyJson(line.substring(4), err)) {
      Serial.println("CFG OK");
      uiLogAdd(TFT_GREEN, "Настройки получены по USB");
      reconnectPending = true;
    } else {
      Serial.println("CFG ERR " + err);
    }
  }
}

static void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      serialLine.trim();
      handleSerialLine(serialLine);
      serialLine = "";
    } else if (serialLine.length() < 4096) {
      serialLine += c;
    }
  }
}

// --- main -------------------------------------------------------------------

void setup() {
  auto cfg = M5.config();
  M5Cardputer.begin(cfg, true);
  Serial.begin(115200);
  M5Cardputer.Speaker.end();

  cfgLoad();
  uiInit();
  uiLogAdd(TFT_YELLOW, "m5v1 " M5V1_VERSION " - Claude remote");
  uiLogAdd(TFT_DARKGREY, "Fn+H - справка");
  drawStatus();
  connectAll();
}

void loop() {
  M5Cardputer.update();
  handleSerial();

  if (recording) {
    pumpRecording();
    bool released = recPushToTalk && !M5Cardputer.BtnA.isPressed();
    if (released || millis() - recStart > kMaxRecordMs || !upload.active()) stopRecording();
  } else if (screen == Screen::Main && M5Cardputer.BtnA.wasPressed()) {
    startRecording(true);
  }

  handleKeyboard();

  if (recording) return;

  if (!jobId.isEmpty() && millis() - jobLastPoll > 800) pollJob();

  if (reconnectPending && jobId.isEmpty()) {
    reconnectPending = false;
    WiFi.disconnect();
    connectAll();
  }

  if (millis() - lastWifiCheck > retryMs && !keysPending) {
    bool tried = false;
    if (!WiFi.isConnected() && jobId.isEmpty() && screen == Screen::Main && !gCfg.wifi.empty()) {
      connectAll();
      tried = true;
    } else if (WiFi.isConnected() && !gServer.valid && !gCfg.token.isEmpty() && jobId.isEmpty() &&
               screen == Screen::Main) {
      resolveServer(netStatus);
      drawStatus();
      drawIdleInput();
      tried = true;
    }
    // Count from the end of the attempt (it can block for many seconds) and back off while
    // nothing is reachable, so the keyboard stays usable: 10 s, 20 s, 40 s ... up to 2 min.
    retryMs = (!tried || gServer.valid) ? 10000 : std::min<uint32_t>(retryMs * 2, 120000);
    lastWifiCheck = millis();
  }
  if (screen == Screen::Main && millis() - lastStatusDraw > 5000) drawStatus();
  delay(5);
}
