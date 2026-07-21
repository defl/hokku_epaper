// Shared WiFi station connect for the ESP32 hokku firmwares.
//
// Dual-network connect (config.wifi_ssid/pass[0..1]) with a BSSID + channel
// fast-reconnect cache persisted across deep sleep in hokku_state, and WPA2/WPA3
// (PMF-capable) auth. Board-independent — reads config + hokku_state only.
#pragma once

#include <stdbool.h>

/* Set by wifi_connect() on each successful connect: true iff the fast-reconnect
 * cache path (cached BSSID + channel) actually worked. Read by the frame-state
 * builder to surface the cache hit-rate to the server. */
extern bool last_wifi_used_cache;

/* Connect to WiFi. Tries the configured networks in the configured order,
 * applying the persisted BSSID cache for the network that last succeeded and
 * falling back to a full scan on a cache miss. Returns true once an IP is
 * obtained, false if all attempts fail. Non-fatal on driver errors (logs and
 * returns false) so a long-lived awake regime doesn't crash. */
bool wifi_connect(void);

/* Disconnect and stop the WiFi driver. */
void wifi_shutdown(void);
