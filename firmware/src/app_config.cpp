#include "app_config.h"

#include <ArduinoJson.h>
#include <Preferences.h>
#include <WiFi.h>

AppConfig gCfg;

static const char* kNs = "m5v1";

void cfgLoad() {
  Preferences p;
  p.begin(kNs, true);
  gCfg.token = p.getString("token", "");
  gCfg.lanUrl = p.getString("lan", "");
  gCfg.remoteUrl = p.getString("remote", "");
  gCfg.mode = static_cast<Mode>(p.getUChar("mode", 0) ? 1 : 0);
  String wifiJson = p.getString("wifi", "[]");
  p.end();

  gCfg.wifi.clear();
  JsonDocument doc;
  if (deserializeJson(doc, wifiJson) == DeserializationError::Ok) {
    for (JsonObject o : doc.as<JsonArray>()) {
      gCfg.wifi.push_back({o["s"].as<String>(), o["p"].as<String>()});
    }
  }
}

void cfgSave() {
  JsonDocument doc;
  JsonArray arr = doc.to<JsonArray>();
  for (const auto& w : gCfg.wifi) {
    JsonObject o = arr.add<JsonObject>();
    o["s"] = w.ssid;
    o["p"] = w.pass;
  }
  String wifiJson;
  serializeJson(doc, wifiJson);

  Preferences p;
  p.begin(kNs, false);
  p.putString("token", gCfg.token);
  p.putString("lan", gCfg.lanUrl);
  p.putString("remote", gCfg.remoteUrl);
  p.putUChar("mode", static_cast<uint8_t>(gCfg.mode));
  p.putString("wifi", wifiJson);
  p.end();
}

const WifiCred* cfgFindWifi(const String& ssid) {
  for (const auto& w : gCfg.wifi) {
    if (w.ssid == ssid) return &w;
  }
  return nullptr;
}

void cfgRemoveWifi(const String& ssid) {
  for (auto it = gCfg.wifi.begin(); it != gCfg.wifi.end(); ++it) {
    if (it->ssid == ssid) {
      gCfg.wifi.erase(it);
      return;
    }
  }
}

// Takes copies: callers pass fields of an entry in gCfg.wifi (re-selecting a known network),
// and cfgRemoveWifi below destroys that entry. With references this saved an empty password.
void cfgAddWifi(String ssid, String pass) {
  cfgRemoveWifi(ssid);
  gCfg.wifi.insert(gCfg.wifi.begin(), {ssid, pass});  // newest first
  if (gCfg.wifi.size() > kMaxWifi) gCfg.wifi.resize(kMaxWifi);
}

static bool validUrl(const String& u) {
  return u.isEmpty() || u.startsWith("http://") || u.startsWith("https://");
}

bool cfgApplyJson(const String& json, String& err) {
  JsonDocument doc;
  if (deserializeJson(doc, json) != DeserializationError::Ok || !doc.is<JsonObject>()) {
    err = "bad json";
    return false;
  }
  if (doc["remote_url"].is<const char*>() && !validUrl(doc["remote_url"].as<String>())) {
    err = "remote_url must start with http:// or https://";
    return false;
  }
  if (doc["lan_url"].is<const char*>() && !validUrl(doc["lan_url"].as<String>())) {
    err = "lan_url must start with http:// or https://";
    return false;
  }

  if (doc["clear_wifi"] | false) gCfg.wifi.clear();
  for (JsonObject o : doc["wifi"].as<JsonArray>()) {
    String ssid = o["ssid"].as<String>();
    if (ssid.isEmpty() || ssid.length() > 32) {
      err = "bad ssid";
      return false;
    }
    cfgAddWifi(ssid, o["pass"].as<String>());
  }
  if (doc["token"].is<const char*>()) gCfg.token = doc["token"].as<String>();
  if (doc["remote_url"].is<const char*>()) gCfg.remoteUrl = doc["remote_url"].as<String>();
  if (doc["lan_url"].is<const char*>()) gCfg.lanUrl = doc["lan_url"].as<String>();
  cfgSave();
  return true;
}

String cfgInfoJson() {
  JsonDocument doc;
  doc["version"] = M5V1_VERSION;
  JsonArray nets = doc["wifi"].to<JsonArray>();
  for (const auto& w : gCfg.wifi) nets.add(w.ssid);  // never print passwords
  doc["token_set"] = !gCfg.token.isEmpty();
  doc["lan_url"] = gCfg.lanUrl;
  doc["remote_url"] = gCfg.remoteUrl;
  doc["connected"] = WiFi.isConnected();
  if (WiFi.isConnected()) {
    doc["ssid"] = WiFi.SSID();
    doc["ip"] = WiFi.localIP().toString();
    doc["rssi"] = WiFi.RSSI();
  }
  String out;
  serializeJson(doc, out);
  return out;
}
