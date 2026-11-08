#pragma once

#include <Arduino.h>
#include <vector>

// Persistent settings, stored in NVS (survive reflashing the app, erased by a full chip erase).

struct WifiCred {
  String ssid;
  String pass;
};

// What text and voice are sent to. Values are stored in NVS (0 and 1 keep their old meaning:
// Claude agent and a PC window).
enum class Mode : uint8_t { Claude = 0, Window = 1, Codex = 2, Keys = 3 };
static constexpr uint8_t kModeCount = 4;

struct AppConfig {
  std::vector<WifiCred> wifi;  // known networks, tried strongest-first
  String token;                // bridge bearer token
  String lanUrl;               // optional fixed LAN URL; empty = UDP discovery
  String remoteUrl;            // optional URL reachable from any network (tunnel)
  Mode mode = Mode::Claude;
};

extern AppConfig gCfg;

static constexpr size_t kMaxWifi = 8;

void cfgLoad();
void cfgSave();
void cfgAddWifi(String ssid, String pass);  // by value: may be given fields of an existing entry
void cfgRemoveWifi(const String& ssid);
const WifiCred* cfgFindWifi(const String& ssid);

// Applies a JSON object received over USB serial (see tools/provision.py).
bool cfgApplyJson(const String& json, String& err);
String cfgInfoJson();
