#pragma once

#include <Arduino.h>
#include <NetworkClient.h>
#include <memory>

// Wi-Fi, bridge discovery and the small HTTP client used to talk to the bridge.

struct Endpoint {
  bool valid = false;
  bool tls = false;
  String host;
  uint16_t port = 0;
  String base;   // path prefix without trailing slash
  String label;  // "LAN" / "NET"
  String url() const;
};

extern Endpoint gServer;

Endpoint parseUrl(const String& url);

// Connects to the strongest known network. status receives a short human-readable result.
// abortFn (optional) is polled while waiting; returning true stops the attempt (a key press).
bool wifiConnect(String& status, uint32_t timeoutMs = 15000, bool (*abortFn)() = nullptr);

// Finds the bridge: fixed LAN URL, then UDP discovery, then the remote URL.
// Returns HTTP status of the probe (200 on success, 401 bad token, <0 unreachable).
int resolveServer(String& status);

// JSON request to the bridge. Returns the HTTP status (or <0 on network error).
int apiRequest(const char* method, const String& path, const String& body, String& response,
               uint32_t timeoutMs = 8000);

// Streams raw PCM to /api/voice with chunked transfer encoding while recording.
class VoiceUpload {
 public:
  bool begin(const String& query, uint32_t sampleRate);
  bool write(const uint8_t* data, size_t len);
  int finish(String& response);  // HTTP status
  void abort();
  bool active() const { return client_ != nullptr; }

 private:
  std::unique_ptr<NetworkClient> client_;  // plain or TLS (NetworkClientSecure)
};
