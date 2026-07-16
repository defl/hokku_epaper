// e1004_hokku.ino  ?? Method 2a
// Seeed reTerminal E1004 (13.3" Spectra 6) as a client of the hokku photo server.
//
// Flow:  wake -> WiFi -> GET http://<server>/hokku/screen/  (X-Screen-Name header)
//        -> read 960,000-byte packed panel buffer
//        -> remap hokku native nibbles -> GxEPD2 encoding (byte LUT)
//        -> writeNative() the two 600-wide halves into the panel framebuffer
//        -> refresh() (~40 s)  -> sleep for X-Sleep-Seconds, repeat.
//
// The hokku wire format (server display.py):
//   image is 1200(w) x 1600(h), split into two physical 600x1600 halves.
//   bytes[0       .. 479999] = LEFT  half  (1600 rows x 300 bytes, 2 px/byte)
//   bytes[480000  .. 959999] = RIGHT half
//   per byte: high nibble = even column, low nibble = odd column.
//   nibble palette: 0x0 black, 0x1 white, 0x2 yellow, 0x3 red, 0x5 blue, 0x6 green
//   ...which is IDENTICAL to the T133A01 native palette.
//
// Board: XIAO_ESP32S3,  PSRAM: OPI PSRAM  (two 960 KB PSRAM buffers needed).

#include <SPI.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_sleep.h>
#include <esp_heap_caps.h>
#include <GxEPD2_7C.h>
#include "GxEPD2_T133A01_1200x1600.h"

// ---------- USER CONFIG ----------
static const char*    WIFI_SSID   = "YOUR_WIFI_SSID";
static const char*    WIFI_PASS   = "YOUR_WIFI_PASSWORD";
static const char*    SERVER_HOST = "192.168.1.100";
static const uint16_t SERVER_PORT = 8080;
static const char*    SCREEN_NAME = "E1004";   // shows as its own screen in the hokku dashboard
static const uint32_t DEFAULT_SLEEP_S = 1800;             // fallback if server sends no X-Sleep-Seconds
#define DEEP_SLEEP 1     // 0 = stay awake & loop (easy dev/reflash). 1 = deep sleep (battery deployment).
// ---------------------------------

// Pin mapping (from the official E1004 example ??dual-chip panel)
#define EPD_SCK_PIN    7
#define EPD_MISO_PIN   8
#define EPD_MOSI_PIN   9
#define EPD_CS_PIN     10
#define EPD_DC_PIN     11
#define EPD_CS1_PIN    2
#define EPD_RES_PIN    38
#define EPD_BUSY_PIN   13
#define EPD_ENABLE_PIN 12

static const uint32_t EXPECT_BYTES = 960000;   // 1200*1600/2
static const uint32_t HALF_BYTES   = 480000;   // one 600x1600 half

// Battery sense (E-series documented values; E1004 assumed same ??VERIFY via serial).
#define BAT_ADC_PIN     1     // ADC1_CH0 (A0) ??battery voltage (through 2x divider)
#define BAT_ENABLE_PIN  21    // must be HIGH to enable the divider, else reads garbage
#define BAT_DIVIDER     2.0f

SPIClass hspi(HSPI);
GxEPD2_T133A01_1200x1600 epd(EPD_CS_PIN, EPD_DC_PIN, EPD_RES_PIN,
                             EPD_BUSY_PIN, EPD_CS1_PIN, EPD_ENABLE_PIN);

static uint8_t* g_buf = nullptr;     // 960 KB PSRAM image buffer
static uint8_t  g_byteLUT[256];      // hokku-native byte -> GxEPD2-encoded byte
static uint32_t g_sleepS = DEFAULT_SLEEP_S;

// Build the byte lookup table: each nibble hokku-native -> GxEPD2 color index.
// GxEPD2 color7 index: 0 black, 1 white, 2 green, 3 blue, 4 red, 5 yellow, 6 orange.
// The driver maps those back to native at send time, so we invert here.
static void buildByteLUT() {
  uint8_t inv[16];
  for (int i = 0; i < 16; i++) inv[i] = 1;   // default white
  inv[0x0] = 0;  // black
  inv[0x1] = 1;  // white
  inv[0x2] = 5;  // yellow
  inv[0x3] = 4;  // red
  inv[0x5] = 3;  // blue
  inv[0x6] = 2;  // green
  for (int b = 0; b < 256; b++)
    g_byteLUT[b] = (inv[(b >> 4) & 0x0F] << 4) | inv[b & 0x0F];
}

// Read battery voltage in mV (averaged). Returns 0 if it looks invalid.
static int readBatteryMv() {
  pinMode(BAT_ENABLE_PIN, OUTPUT);
  digitalWrite(BAT_ENABLE_PIN, HIGH);
  delay(50);
  analogSetPinAttenuation(BAT_ADC_PIN, ADC_11db);
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) { sum += analogReadMilliVolts(BAT_ADC_PIN); delay(2); }
  digitalWrite(BAT_ENABLE_PIN, LOW);
  int pin_mv = sum / 16;
  int batt_mv = (int)(pin_mv * BAT_DIVIDER);
  Serial.printf("[batt] pin=%d mV  -> battery=%d mV\n", pin_mv, batt_mv);
  if (batt_mv < 2500 || batt_mv > 4500) return 0;   // sanity gate
  return batt_mv;
}

static bool connectWiFi(uint32_t timeout_ms = 20000) {
  if (WiFi.status() == WL_CONNECTED) return true;
  Serial.printf("[wifi] connecting to %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeout_ms) {
    delay(250); Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] connected, IP=%s\n", WiFi.localIP().toString().c_str());
    return true;
  }
  Serial.println("[wifi] FAILED");
  return false;
}

// Fetch + display one image. Returns seconds to sleep before the next call.
static uint32_t doUpdate() {
  uint32_t sleepS = DEFAULT_SLEEP_S;
  if (!connectWiFi()) return 60;   // retry soon on WiFi failure

  if (!g_buf) {
    g_buf = (uint8_t*)ps_malloc(EXPECT_BYTES);
    if (!g_buf) { Serial.println("[mem] ps_malloc 960KB FAILED"); return 300; }
  }

  WiFiClient client;
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + "/hokku/screen/";
  Serial.printf("[http] GET %s\n", url.c_str());
  http.begin(client, url);
  http.addHeader("X-Screen-Name", SCREEN_NAME);
  int batt_mv = readBatteryMv();
  if (batt_mv > 0) http.addHeader("X-Battery-mV", String(batt_mv));
  const char* collect[] = {"X-Sleep-Seconds"};
  http.collectHeaders(collect, 1);
  http.setTimeout(20000);

  int code = http.GET();
  Serial.printf("[http] status=%d  len=%d\n", code, http.getSize());

  if (http.hasHeader("X-Sleep-Seconds")) {
    long s = http.header("X-Sleep-Seconds").toInt();
    if (s > 0) sleepS = (uint32_t)s;
    Serial.printf("[http] X-Sleep-Seconds=%lu\n", (unsigned long)sleepS);
  }

  if (code != 200) {
    Serial.println("[http] no image this cycle (will retry after sleep)");
    http.end();
    return sleepS;
  }

  // Read the 960,000-byte body into PSRAM.
  WiFiClient* st = http.getStreamPtr();
  uint32_t got = 0;
  uint32_t deadline = millis() + 30000;
  while (got < EXPECT_BYTES && millis() < deadline) {
    size_t avail = st->available();
    if (avail) {
      uint32_t want = EXPECT_BYTES - got;
      int r = st->readBytes(g_buf + got, avail < want ? avail : want);
      if (r > 0) { got += r; deadline = millis() + 30000; }
    } else {
      if (!http.connected()) break;
      delay(2);
    }
  }
  http.end();
  Serial.printf("[http] received %lu / %lu bytes\n", (unsigned long)got, (unsigned long)EXPECT_BYTES);
  if (got != EXPECT_BYTES) { Serial.println("[http] short read, skipping draw"); return sleepS; }

  // Remap hokku-native bytes -> GxEPD2 encoding in place.
  for (uint32_t i = 0; i < EXPECT_BYTES; i++) g_buf[i] = g_byteLUT[g_buf[i]];

  // Push the two halves into the panel framebuffer, then refresh.
  Serial.println("[epd] writing framebuffer + refresh (~40s)...");
  uint32_t t0 = millis();
  epd.writeNative(g_buf,             nullptr, 0,   0, 600, 1600);  // left  half -> cols 0..599
  epd.writeNative(g_buf + HALF_BYTES, nullptr, 600, 0, 600, 1600); // right half -> cols 600..1199
  epd.refresh(false);
  epd.hibernate();
  Serial.printf("[epd] done in %lu ms\n", (unsigned long)(millis() - t0));
  return sleepS;
}

void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\n[E1004-hokku] boot");

  buildByteLUT();
  hspi.begin(EPD_SCK_PIN, EPD_MISO_PIN, EPD_MOSI_PIN, -1);
  epd.selectSPI(hspi, SPISettings(10000000, MSBFIRST, SPI_MODE0));
  epd.init(115200);

  g_sleepS = doUpdate();

#if DEEP_SLEEP
  Serial.printf("[sleep] deep sleep %lu s\n", (unsigned long)g_sleepS);
  WiFi.disconnect(true);
  esp_sleep_enable_timer_wakeup((uint64_t)g_sleepS * 1000000ULL);
  esp_deep_sleep_start();   // resets and re-runs setup() on wake
#endif
}

void loop() {
#if !DEEP_SLEEP
  Serial.printf("[sleep] awake-wait %lu s then re-fetch\n", (unsigned long)g_sleepS);
  delay(g_sleepS * 1000UL);
  g_sleepS = doUpdate();
#endif
}
