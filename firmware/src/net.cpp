#include "net.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiUdp.h>

#include <algorithm>
#include <vector>

#include "app_config.h"

Endpoint gServer;

static constexpr uint16_t kDiscoveryPort = 8766;
static constexpr uint16_t kDiscoveryReplyPort = 8767;

String Endpoint::url() const {
  String u = tls ? "https://" : "http://";
  u += host;
  if (port != (tls ? 443 : 80)) u += ":" + String(port);
  return u + base;
}

Endpoint parseUrl(const String& raw) {
  Endpoint ep;
  String s = raw;
  s.trim();
  if (s.startsWith("https://")) {
    ep.tls = true;
    s = s.substring(8);
  } else if (s.startsWith("http://")) {
    s = s.substring(7);
  } else {
    return ep;
  }
  int slash = s.indexOf('/');
  String hostPort = slash >= 0 ? s.substring(0, slash) : s;
  ep.base = slash >= 0 ? s.substring(slash) : "";
  while (ep.base.endsWith("/")) ep.base.remove(ep.base.length() - 1);
  int colon = hostPort.lastIndexOf(':');
  if (colon >= 0) {
    ep.host = hostPort.substring(0, colon);
    ep.port = hostPort.substring(colon + 1).toInt();
  } else {
    ep.host = hostPort;
    ep.port = ep.tls ? 443 : 80;
  }
  ep.valid = !ep.host.isEmpty() && ep.port > 0;
  return ep;
}

static std::unique_ptr<NetworkClient> makeClient(const Endpoint& ep) {
  if (ep.tls) {
    auto c = std::make_unique<WiFiClientSecure>();
    // The tunnel URL is authenticated by the bridge token; certificate pinning is left out so
    // that any tunnel provider works. See README ("Remote access") for the trade-off.
    c->setInsecure();
    return c;
  }
  return std::make_unique<WiFiClient>();
}

// --- Wi-Fi ----------------------------------------------------------------

// Why the last attempt failed, as reported by the Wi-Fi driver (wifi_err_reason_t).
static volatile int lastReason = 0;

static void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) lastReason = info.wifi_sta_disconnected.reason;
}

static String reasonText(int r) {
  switch (r) {
    case 2:
    case 15:
    case 202:
    case 204:
      return "неверный пароль";
    case 201:
      return "сеть не найдена";
    case 200:
      return "слабый сигнал";
    case 5:
      return "точка доступа переполнена";
    case 203:
      return "отказ в подключении";
    case 20:
    case 29:
    case 210:
    case 211:
      return "неподдерживаемая защита (нужен WPA2)";
    default:
      return "код " + String(r);
  }
}

static const char* authName(wifi_auth_mode_t m) {
  switch (m) {
    case WIFI_AUTH_OPEN: return "open";
    case WIFI_AUTH_WEP: return "WEP";
    case WIFI_AUTH_WPA_PSK: return "WPA";
    case WIFI_AUTH_WPA2_PSK: return "WPA2";
    case WIFI_AUTH_WPA_WPA2_PSK: return "WPA/WPA2";
    case WIFI_AUTH_WPA3_PSK: return "WPA3";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2/WPA3";
    default: return "other";
  }
}

bool wifiConnect(String& status, uint32_t timeoutMs, bool (*abortFn)()) {
  static bool handlerSet = false;
  if (!handlerSet) {
    WiFi.onEvent(onWifiEvent);
    handlerSet = true;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  if (gCfg.wifi.empty()) {
    status = "Нет сохранённых сетей (Fn+W)";
    return false;
  }

  struct Candidate {
    int rssi;
    const WifiCred* cred;
  };
  std::vector<Candidate> candidates;
  int n = WiFi.scanNetworks();
  for (int i = 0; i < n; ++i) {
    const WifiCred* cred = cfgFindWifi(WiFi.SSID(i));
    if (!cred) continue;
    Serial.printf("WIFI seen '%s' rssi=%d ch=%d auth=%s\n", WiFi.SSID(i).c_str(), WiFi.RSSI(i),
                  WiFi.channel(i), authName(WiFi.encryptionType(i)));
    bool seen = false;
    for (auto& c : candidates) seen |= c.cred == cred;
    if (!seen) candidates.push_back({WiFi.RSSI(i), cred});
  }
  WiFi.scanDelete();
  std::sort(candidates.begin(), candidates.end(),
            [](const Candidate& a, const Candidate& b) { return a.rssi > b.rssi; });
  if (candidates.empty()) {
    // Nothing known is visible; the network may be hidden, so try the most recent one blindly.
    candidates.push_back({-100, &gCfg.wifi.front()});
  }

  String failures;
  for (const auto& c : candidates) {
    lastReason = 0;
    Serial.printf("WIFI connect '%s' (password %u chars)\n", c.cred->ssid.c_str(), (unsigned)c.cred->pass.length());
    WiFi.begin(c.cred->ssid.c_str(), c.cred->pass.c_str());
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
      if (abortFn && abortFn()) {
        WiFi.disconnect();
        status = "Подключение прервано";
        return false;
      }
      delay(50);
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("WIFI ok '%s' ip=%s\n", c.cred->ssid.c_str(), WiFi.localIP().toString().c_str());
      status = c.cred->ssid;
      return true;
    }
    int reason = lastReason;
    Serial.printf("WIFI fail '%s' reason=%d status=%d\n", c.cred->ssid.c_str(), reason, (int)WiFi.status());
    if (!failures.isEmpty()) failures += "; ";
    failures += c.cred->ssid + ": " + (reason ? reasonText(reason) : String("нет ответа"));
    WiFi.disconnect();
  }
  status = "Wi-Fi не подключён - " + failures;
  return false;
}

// --- bridge discovery -----------------------------------------------------

static bool discoverLan(Endpoint& out) {
  WiFiUDP udp;
  if (!udp.begin(kDiscoveryReplyPort)) return false;
  const IPAddress targets[] = {WiFi.broadcastIP(), IPAddress(255, 255, 255, 255)};
  for (int attempt = 0; attempt < 3; ++attempt) {
    for (const auto& ip : targets) {
      udp.beginPacket(ip, kDiscoveryPort);
      udp.print("M5BRIDGE?");
      udp.endPacket();
    }
    uint32_t start = millis();
    while (millis() - start < 700) {
      if (udp.parsePacket() > 0) {
        char buf[128];
        int len = udp.read(buf, sizeof(buf) - 1);
        buf[len > 0 ? len : 0] = 0;
        String reply(buf);
        if (reply.startsWith("M5BRIDGE ")) {
          out.valid = true;
          out.tls = false;
          out.host = udp.remoteIP().toString();
          out.port = reply.substring(9).toInt();
          out.base = "";
          udp.stop();
          return out.port > 0;
        }
      }
      delay(10);
    }
  }
  udp.stop();
  return false;
}

int resolveServer(String& status) {
  std::vector<Endpoint> candidates;
  if (!gCfg.lanUrl.isEmpty()) {
    Endpoint ep = parseUrl(gCfg.lanUrl);
    ep.label = "LAN";
    if (ep.valid) candidates.push_back(ep);
  }
  Endpoint found;
  if (WiFi.isConnected() && discoverLan(found)) {
    found.label = "LAN";
    candidates.push_back(found);
  }
  if (!gCfg.remoteUrl.isEmpty()) {
    Endpoint ep = parseUrl(gCfg.remoteUrl);
    ep.label = "NET";
    if (ep.valid) candidates.push_back(ep);
  }
  if (candidates.empty()) {
    gServer = Endpoint();
    status = "Мост не найден в сети";
    return -1;
  }

  int best = -1;
  for (const auto& ep : candidates) {
    gServer = ep;
    String resp;
    int code = apiRequest("GET", "/api/status", "", resp, 5000);
    if (code == 200) {
      status = ep.label + " " + ep.host;
      return 200;
    }
    if (code == 401) best = 401;
  }
  gServer = Endpoint();
  status = best == 401 ? "Неверный токен моста" : "Мост недоступен";
  return best;
}

// --- HTTP -----------------------------------------------------------------

int apiRequest(const char* method, const String& path, const String& body, String& response,
               uint32_t timeoutMs) {
  if (!gServer.valid || !WiFi.isConnected()) return -10;
  auto client = makeClient(gServer);
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(timeoutMs);
  if (!http.begin(*client, gServer.url() + path)) return -11;
  http.addHeader("Authorization", "Bearer " + gCfg.token);
  if (!body.isEmpty()) http.addHeader("Content-Type", "application/json");
  int code = http.sendRequest(method, body);
  response = code > 0 ? http.getString() : String();
  http.end();
  return code;
}

static bool writeAll(Client& c, const uint8_t* data, size_t len) {
  uint32_t start = millis();
  while (len > 0) {
    size_t n = c.write(data, len);
    if (n == 0) {
      if (!c.connected() || millis() - start > 5000) return false;
      delay(2);
      continue;
    }
    data += n;
    len -= n;
  }
  return true;
}

static bool writeStr(Client& c, const String& s) {
  return writeAll(c, reinterpret_cast<const uint8_t*>(s.c_str()), s.length());
}

bool VoiceUpload::begin(const String& query, uint32_t sampleRate) {
  if (!gServer.valid || !WiFi.isConnected()) return false;
  client_ = makeClient(gServer);
  if (!client_->connect(gServer.host.c_str(), gServer.port)) {
    client_.reset();
    return false;
  }
  String host = gServer.host;
  if (gServer.port != (gServer.tls ? 443 : 80)) host += ":" + String(gServer.port);
  String req = "POST " + gServer.base + "/api/voice" + query + " HTTP/1.1\r\n";
  req += "Host: " + host + "\r\n";
  req += "Authorization: Bearer " + gCfg.token + "\r\n";
  req += "Content-Type: application/octet-stream\r\n";
  req += "X-Sample-Rate: " + String(sampleRate) + "\r\n";
  req += "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n";
  if (!writeStr(*client_, req)) {
    abort();
    return false;
  }
  return true;
}

bool VoiceUpload::write(const uint8_t* data, size_t len) {
  if (!client_ || len == 0) return client_ != nullptr;
  char hdr[12];
  snprintf(hdr, sizeof(hdr), "%X\r\n", static_cast<unsigned>(len));
  bool ok = writeStr(*client_, hdr) && writeAll(*client_, data, len) && writeStr(*client_, "\r\n");
  if (!ok) abort();
  return ok;
}

int VoiceUpload::finish(String& response) {
  if (!client_) return -10;
  if (!writeStr(*client_, "0\r\n\r\n")) {
    abort();
    return -12;
  }
  String raw;
  uint8_t buf[256];
  uint32_t start = millis();
  while (millis() - start < 15000) {
    int avail = client_->available();
    if (avail > 0) {
      int n = client_->read(buf, std::min<int>(avail, sizeof(buf)));
      if (n > 0) raw.concat(reinterpret_cast<const char*>(buf), n);
    } else if (!client_->connected()) {
      break;
    } else {
      delay(5);
    }
  }
  abort();
  if (!raw.startsWith("HTTP/1.")) return -13;
  int code = raw.substring(9, 12).toInt();
  int bodyAt = raw.indexOf("\r\n\r\n");
  response = bodyAt >= 0 ? raw.substring(bodyAt + 4) : String();
  return code;
}

void VoiceUpload::abort() {
  if (client_) client_->stop();
  client_.reset();
}
