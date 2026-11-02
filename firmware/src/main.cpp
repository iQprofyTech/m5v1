// m5v1: voice / keyboard remote for Claude on a PC, running on M5Stack Cardputer-Adv.
//
//   G0 (hold)        push-to-talk          Fn+Space   start/stop recording
//   type + Enter     send text             Tab        switch Agent / Desktop mode
//   Fn+; / Fn+.      scroll up / down      Fn+N       new agent session
//   Fn+L             pick agent session    Fn+W       Wi-Fi networks
//   Fn+R             reconnect             Fn+` (Esc) cancel current job
//   Fn+H             help

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

enum class Screen { Main, Wifi, WifiPass, Sessions, Help };
static Screen screen = Screen::Main;

static String input;
static String jobId;
static bool jobTranscriptShown = false;
static uint32_t jobLastPoll = 0;
static int jobPollErrors = 0;
static String jobLastState;

static String netStatus = "--";
static uint32_t lastStatusDraw = 0;
static uint32_t lastWifiCheck = 0;
static bool reconnectPending = false;

static std::vector<String> menuItems;
static std::vector<String> menuIds;  // ssid or session id per item
static int menuSel = 0;
static String passSsid;

static String serialLine;

static const char* modeName() { return gCfg.mode == Mode::Agent ? "AGENT" : "DESK"; }

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
    String s = jobLastState == "transcribing" ? "Распознаю речь..." : "Claude работает... (Esc - отмена)";
    uiDrawInput(s, TFT_CYAN);
  } else if (input.isEmpty()) {
    uiDrawInput(gServer.valid ? "> держи G0 и говори, или пиши" : "> " + netStatus, TFT_DARKGREY);
  } else {
    uiDrawInput("> " + input + "_", TFT_WHITE);
  }
}

static void showHelp() {
  screen = Screen::Help;
  uiDrawLines("Клавиши (любая - назад)", {
      "G0 удерж. / Fn+Space - голос",
      "Enter - отправить текст",
      "Tab - режим Agent/Desktop",
      "Fn+; Fn+. - прокрутка",
      "Fn+N новая сессия, Fn+L список",
      "Fn+W Wi-Fi, Fn+R переподкл.",
      "Fn+` (Esc) - отменить задачу",
  });
}

static void backToMain() {
  screen = Screen::Main;
  uiDrawLog();
  drawIdleInput();
  drawStatus();
}

// --- connection -------------------------------------------------------------

static void connectAll() {
  uiDrawInput("Подключаюсь к Wi-Fi...", TFT_YELLOW);
  String wifiStatus;
  gServer = Endpoint();
  if (!wifiConnect(wifiStatus)) {
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
  doc["mode"] = gCfg.mode == Mode::Agent ? "agent" : "desktop";
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
  if (state != jobLastState) {
    jobLastState = state;
    drawIdleInput();
  }
  if (state == "done") {
    uiLogAdd(TFT_WHITE, doc["reply"].as<String>());
  } else if (state == "error") {
    uiLogAdd(TFT_RED, "Ошибка: " + doc["error"].as<String>());
  } else if (state == "cancelled") {
    uiLogAdd(TFT_ORANGE, "Отменено");
  } else {
    return;
  }
  jobId = "";
  drawStatus();
  drawIdleInput();
}

static void cancelJob() {
  if (jobId.isEmpty()) return;
  String resp;
  apiRequest("POST", "/api/jobs/" + jobId + "/cancel", "", resp);
  uiDrawInput("Отменяю...", TFT_ORANGE);
}

// --- recording --------------------------------------------------------------

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
  String query = gCfg.mode == Mode::Agent ? "?mode=agent" : "?mode=desktop";
  if (!upload.begin(query, kSampleRate)) {
    uiLogAdd(TFT_RED, "Не удалось открыть соединение");
    return;
  }
  M5Cardputer.Speaker.end();  // speaker and mic share the codec
  M5Cardputer.Mic.begin();
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
  input = "";
  uiDrawLines("Пароль для " + ssid, {"Введи пароль и Enter", "Esc - отмена"});
  uiDrawInput("> _", TFT_WHITE);
}

static void openSessionsMenu() {
  if (!gServer.valid) {
    uiLogAdd(TFT_RED, "Нет связи с мостом");
    return;
  }
  String resp;
  if (apiRequest("GET", "/api/sessions", "", resp) != 200) {
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
  uiDrawMenu("Сессии агента: Enter / Esc", menuItems, menuSel);
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
  int code = apiRequest("POST", "/api/sessions/select", body, resp);
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
  const String title = screen == Screen::Wifi ? "Wi-Fi: Enter-выбрать Del-забыть Esc" : "Сессии агента: Enter / Esc";

  if (k.enter && count > 0) {
    if (screen == Screen::Sessions) {
      selectSession(menuIds[menuSel]);
      return;
    }
    const String& ssid = menuIds[menuSel];
    if (cfgFindWifi(ssid)) {
      cfgAddWifi(ssid, cfgFindWifi(ssid)->pass);  // move to the front
      cfgSave();
      backToMain();
      reconnectPending = true;
    } else {
      openPasswordPrompt(ssid);
    }
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
      cancelJob();
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
    }
    return;
  }
  if (recording) {
    if (!recPushToTalk) stopRecording();  // any key ends a toggled recording
    return;
  }
  if (k.tab) {
    gCfg.mode = gCfg.mode == Mode::Agent ? Mode::Desktop : Mode::Agent;
    cfgSave();
    uiLogAdd(TFT_YELLOW, gCfg.mode == Mode::Agent ? "Режим: агент Claude Code" : "Режим: чат Claude Desktop");
    drawStatus();
    return;
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
  if (!M5Cardputer.Keyboard.isChange() || !M5Cardputer.Keyboard.isPressed()) return;
  Keyboard_Class::KeysState k = M5Cardputer.Keyboard.keysState();
  switch (screen) {
    case Screen::Main:
      handleMainKeys(k);
      break;
    case Screen::Wifi:
    case Screen::Sessions:
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

// --- USB serial provisioning -----------------------------------------------

static void handleSerialLine(const String& line) {
  if (line == "PING") {
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

  if (millis() - lastWifiCheck > 10000) {
    lastWifiCheck = millis();
    if (!WiFi.isConnected() && jobId.isEmpty() && screen == Screen::Main && !gCfg.wifi.empty()) {
      connectAll();
    } else if (WiFi.isConnected() && !gServer.valid && !gCfg.token.isEmpty() && jobId.isEmpty() &&
               screen == Screen::Main) {
      resolveServer(netStatus);
      drawIdleInput();
    }
  }
  if (screen == Screen::Main && millis() - lastStatusDraw > 5000) drawStatus();
  delay(5);
}
