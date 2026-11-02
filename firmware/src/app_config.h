#pragma once

#include <Arduino.h>
#include <vector>

// Persistent settings, stored in NVS (survive reflashing the app, erased by a full chip erase).

struct WifiCred {
  String ssid;
  String pass;
};

enum class Mode : uint8_t { Agent = 0, Desktop = 1 };

struct AppConfig {
  std::vector<WifiCred> wifi;  // known networks, tried strongest-first
  String token;                // bridge bearer token
  String lanUrl;               // optional fixed LAN URL; empty = UDP discovery
  String remoteUrl;            // optional URL reachable from any network (tunnel)
  Mode mode = Mode::Agent;
};

extern AppConfig gCfg;

static constexpr size_t kMaxWifi = 8;

void cfgLoad();
void cfgSave();
void cfgAddWifi(const String& ssid, const String& pass);
void cfgRemoveWifi(const String& ssid);
const WifiCred* cfgFindWifi(const String& ssid);

// Applies a JSON object received over USB serial (see tools/provision.py).
bool cfgApplyJson(const String& json, String& err);
String cfgInfoJson();
