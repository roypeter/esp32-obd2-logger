#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <SD.h>
#include <LittleFS.h>
#include <SPI.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define SD_CS 15
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_ADDR 0x3C
#define OLED_UPDATE_MS 500

#include "driver/twai.h"

struct PidStats;

Adafruit_SSD1306 oled(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);
unsigned long lastOledUpdate = 0;

// Storage abstraction — SD card preferred, LittleFS fallback
FS* storage = nullptr;
bool useSD = false;

uint64_t storageTotalBytes() {
  return useSD ? SD.totalBytes() : LittleFS.totalBytes();
}
uint64_t storageUsedBytes() {
  return useSD ? SD.usedBytes() : LittleFS.usedBytes();
}

// --- Configuration ---
const char* ssid = "obd2logger";
const char* password = "logger@ka01"; // Must be at least 8 characters

#define RX_PIN 4
#define TX_PIN 5
#define RESPONSE_TIMEOUT_MS 50
#define SLOW_POLL_RATIO 5  // 1 slow PID every 5 polls (pattern: F F F F S)

WebServer server(80);
SemaphoreHandle_t fsMutex;

// Fast PIDs — change rapidly during driving (~4Hz per PID)
const uint8_t fast_pids[] = {
  0x0C, 0x0D, 0x0B, 0x04, 0x0E, 0x47,  // RPM, Speed, MAP, Load, Timing, Throttle
  0x5A, 0x4C, 0x61, 0x62,               // Pedal, CmdThr, DemTorque, ActTorque
  0x06, 0x07, 0x15, 0x44, 0x56,         // STFT, LTFT, O2S2, CmdEq, SecLTFT
  0x5E,                                  // Fuel Rate
  0x34, 0x43                             // O2S1 WR Lambda+Current, Absolute Load
};
const int NUM_FAST = sizeof(fast_pids) / sizeof(fast_pids[0]);

// Slow PIDs — change slowly, polled less often (~1Hz per PID)
const uint8_t slow_pids[] = {
  0x05, 0x0F, 0x33,                     // Coolant, IAT, Baro
  0x3C, 0x5C, 0x2F,                     // CatTemp, Oil Temp, Fuel Level
  0x42, 0x46, 0x63,                     // Voltage, Ambient Temp, Ref Torque
  0x2E                                  // Commanded Evap Purge
};
const int NUM_SLOW = sizeof(slow_pids) / sizeof(slow_pids[0]);

// Per-PID timeout tracking and auto-disable
#define MAX_PIDS 32
#define PID_DISABLE_THRESHOLD 10  // disable after N consecutive timeouts
struct PidStats {
  uint8_t pid;
  unsigned long timeouts;
  unsigned long responses;
  uint8_t consecutiveTimeouts;
  bool disabled;
};
PidStats pidStats[MAX_PIDS];
int numTrackedPids = 0;

PidStats* getPidStats(uint8_t pid) {
  for (int i = 0; i < numTrackedPids; i++) {
    if (pidStats[i].pid == pid) return &pidStats[i];
  }
  if (numTrackedPids < MAX_PIDS) {
    PidStats* s = &pidStats[numTrackedPids++];
    s->pid = pid; s->timeouts = 0; s->responses = 0;
    s->consecutiveTimeouts = 0; s->disabled = false;
    return s;
  }
  return nullptr;
}

bool isPidDisabled(uint8_t pid) {
  PidStats* s = getPidStats(pid);
  return s && s->disabled;
}

// Response-driven polling state
bool requestPending = false;
unsigned long requestSentTime = 0;
uint8_t pendingPid = 0;
int pollCounter = 0;
int fastIndex = 0;
int slowIndex = 0;

// Latest values
int rpm = 0;
int speed_kmh = 0;
int coolant_temp = 0;
int manifold_kpa = 0;
int intake_temp = 0;
int engine_load = 0;
float timing_advance = 0;
int throttle_pos = 0;
int accel_pedal = 0;
int cmd_throttle = 0;
int demand_torque = 0;
int actual_torque = 0;
float short_fuel_trim = 0;
float long_fuel_trim = 0;
int baro_kpa = 0;
float o2_s2_voltage = 0;
float o2_s2_stft = 0;
float cmd_equiv_ratio = 0;
int catalyst_temp = 0;
float o2_secondary_ltft = 0;
int oil_temp = 0;
int fuel_level = 0;
float fuel_rate = 0;        // L/h
float module_voltage = 0;   // V
int ambient_temp = 0;       // °C
int ref_torque = 0;         // N·m
float o2s1_lambda = 0;      // O2S1 WR equivalence ratio (lambda)
float o2s1_current = 0;     // O2S1 WR current (mA)
float absolute_load = 0;    // Absolute load (%)
int evap_purge = 0;         // Commanded evap purge (%)
int estimated_gear = 0; // 0 = neutral/clutch, 1-6 = gear
unsigned long lastLog = 0;

// Session-based logging
int sessionNum = 0;
char sessionFile[24]; // e.g. "/data/s001.csv"
const char* CSV_HEADER = "Timestamp_ms,RPM,Speed_kmh,Coolant_C,OilTemp_C,MAP_kPa,IAT_C,Load_pct,TimingAdv_deg,Throttle_pct,AccelPedal_pct,CmdThrottle_pct,DemandTorque_pct,ActualTorque_pct,STFT_pct,LTFT_pct,Baro_kPa,O2S2_V,O2S2_STFT_pct,CmdEquivRatio,CatalystTemp_C,O2_SecLTFT_pct,Gear,FuelLevel_pct,FuelRate_Lph,ModuleVoltage_V,AmbientTemp_C,RefTorque_Nm,O2S1_Lambda,O2S1_Current_mA,AbsLoad_pct,EvapPurge_pct";

// Running averages for OLED stats page
float avgStft = 0, avgTiming = 0, avgMap = 0;
unsigned long avgSamples = 0;

// Session tracking for OLED page 3
int maxBoost = 0;
unsigned long sessionStartMs = 0;
float totalDistKm = 0;    // accumulated distance
float totalFuelL = 0;     // accumulated fuel consumed

// OLED page timing
#define OLED_STATS_INTERVAL 30000  // show stats every 30s
#define OLED_STATS_DURATION 5000   // show each stats page for 5s
unsigned long lastStatsShow = 0;
int oledStatsPage = 0;  // 0=live, 1=avgs, 2=session stats

// --- Debug ring buffer log ---
#define LOG_LINES 50
#define LOG_LINE_LEN 120
char logBuf[LOG_LINES][LOG_LINE_LEN];
int logIndex = 0;
unsigned long txCount = 0, rxCount = 0, rxTimeoutCount = 0;
unsigned long lastStatusLog = 0;

void addLog(const char* fmt, ...) {
  char* line = logBuf[logIndex % LOG_LINES];
  int off = snprintf(line, LOG_LINE_LEN, "[%lu] ", millis());
  va_list args;
  va_start(args, fmt);
  vsnprintf(line + off, LOG_LINE_LEN - off, fmt, args);
  va_end(args);
  Serial.println(line);
  logIndex++;
}

// Estimated RPM/Speed ratio boundaries for Nexon 1.2T petrol 6-speed manual.
// Calibrate these after a test drive by noting RPM at a steady speed in each gear.
// ratio = RPM / Speed_kmh
const float GEAR_RATIO_MIN[] = { 80,  42, 28, 20, 15, 12 }; // lower bound for gears 1-6
const float GEAR_RATIO_MAX[] = { 160, 79, 41, 27, 22, 17 }; // upper bound for gears 1-6

// Web server task running on Core 0
void webServerTask(void *parameter) {
  for (;;) {
    server.handleClient();
    vTaskDelay(1); // yield to WiFi stack on Core 0
  }
}

void updateStats() {
  avgSamples++;
  float n = (float)avgSamples;
  avgStft += (short_fuel_trim - avgStft) / n;
  avgTiming += (timing_advance - avgTiming) / n;
  avgMap += (manifold_kpa - avgMap) / n;

  if (manifold_kpa > maxBoost) maxBoost = manifold_kpa;

  // Accumulate distance and fuel (called every 500ms = OLED_UPDATE_MS)
  float dt_h = OLED_UPDATE_MS / 3600000.0;  // interval in hours
  totalDistKm += speed_kmh * dt_h;
  totalFuelL += fuel_rate * dt_h;
}

void drawOledLive() {
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);

  // Row 1: RPM | IAT
  oled.setTextSize(2);
  oled.setCursor(0, 0);
  oled.print(rpm);
  oled.setCursor(80, 0);
  oled.print(intake_temp);

  // Row 2: TMG | MAP
  oled.setCursor(0, 22);
  oled.print(timing_advance, 1);
  oled.setCursor(80, 22);
  oled.print(manifold_kpa);

  // Row 3: CLT | LOAD
  oled.setCursor(0, 44);
  oled.print(coolant_temp);
  oled.setCursor(80, 44);
  oled.print(engine_load);

  oled.display();
}

void drawOledStats() {
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(2);

  oled.setCursor(0, 0);
  oled.print("S ");
  oled.print(avgStft, 1);

  oled.setCursor(0, 22);
  oled.print("T ");
  oled.print(avgTiming, 1);

  oled.setCursor(0, 44);
  oled.print("M ");
  oled.print(avgMap, 0);

  oled.display();
}

void drawOledSession() {
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(2);

  // Max boost
  oled.setCursor(0, 0);
  oled.print(maxBoost);

  // Session time hh:mm
  unsigned long elapsed = (millis() - sessionStartMs) / 1000;
  int hh = elapsed / 3600;
  int mm = (elapsed % 3600) / 60;
  oled.setCursor(0, 22);
  char timeBuf[8];
  snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d", hh, mm);
  oled.print(timeBuf);

  // Fuel mileage km/L
  oled.setCursor(0, 44);
  if (totalFuelL > 0.01) {
    oled.print(totalDistKm / totalFuelL, 1);
  } else {
    oled.print("--");
  }

  oled.display();
}

void updateOled() {
  updateStats();

  unsigned long now = millis();

  // Page rotation: 0=live, 1=avgs page, 2=session page
  if (oledStatsPage > 0) {
    unsigned long pageStart = lastStatsShow + (oledStatsPage - 1) * OLED_STATS_DURATION;
    if (now - pageStart >= OLED_STATS_DURATION) {
      oledStatsPage++;
      if (oledStatsPage > 2) oledStatsPage = 0;  // back to live
    }
    if (oledStatsPage == 1) { drawOledStats(); return; }
    if (oledStatsPage == 2) { drawOledSession(); return; }
  }

  if (now - lastStatsShow >= OLED_STATS_INTERVAL && avgSamples > 0) {
    oledStatsPage = 1;
    lastStatsShow = now;
    drawOledStats();
    return;
  }

  drawOledLive();
}

void setup() {
  Serial.begin(115200);
  
  // 1. Initialize storage (SD card preferred, LittleFS fallback) and mutex
  fsMutex = xSemaphoreCreateMutex();

  bool sdOk = SD.begin(SD_CS);
  if (sdOk) {
    storage = &SD;
    useSD = true;
    addLog("SD card mounted OK — Size: %llu MB", SD.cardSize() / (1024 * 1024));
  } else {
    addLog("SD card not found, trying internal flash (LittleFS)");
    if (LittleFS.begin(true)) {  // true = format on first use
      storage = &LittleFS;
      addLog("LittleFS mounted OK (~1.5 MB available, logs fill up fast)");
    } else {
      addLog("LittleFS mount FAILED — no storage available");
    }
  }

  if (storage) {
    addLog("Storage: %llu KB used / %llu KB total",
           storageUsedBytes() / 1024, storageTotalBytes() / 1024);

    // Create data folder if it doesn't exist
    if (!storage->exists("/data")) storage->mkdir("/data");

    // Read session counter, increment, and save
    File sf = storage->open("/data/session.txt", "r");
    if (sf) {
      sessionNum = sf.parseInt();
      sf.close();
    }
    sessionNum++;
    sf = storage->open("/data/session.txt", "w");
    if (sf) { sf.print(sessionNum); sf.close(); }
    snprintf(sessionFile, sizeof(sessionFile), "/data/s%03d.csv", sessionNum);
    addLog("Session %d -> %s", sessionNum, sessionFile);

    // Create new session CSV with headers
    File file = storage->open(sessionFile, "w");
    if (file) { file.println(CSV_HEADER); file.close(); }
  }

  // 2. Initialize OLED and show SD card status
  if (oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Wire.setClock(400000);
    addLog("OLED init OK");
    oled.clearDisplay();
    oled.setTextColor(SSD1306_WHITE);
    oled.setTextSize(1);
    oled.setCursor(0, 0);
    oled.println("Nexon CAN Logger");
    oled.println();
    if (storage) {
      uint64_t totalKB = storageTotalBytes() / 1024;
      uint64_t usedKB = storageUsedBytes() / 1024;
      uint64_t freeKB = totalKB - usedKB;
      oled.printf("%s  Session #%d\n", useSD ? "SD" : "LittleFS", sessionNum);
      if (totalKB >= 1024) {
        oled.printf("Total: %llu MB\n", totalKB / 1024);
        oled.printf("Used:  %llu MB\n", usedKB / 1024);
        oled.printf("Free:  %llu MB", freeKB / 1024);
      } else {
        oled.printf("Total: %llu KB\n", totalKB);
        oled.printf("Used:  %llu KB\n", usedKB);
        oled.printf("Free:  %llu KB", freeKB);
      }
    } else {
      oled.println("Storage: NONE");
    }
    oled.display();
    delay(3000);
  } else {
    addLog("OLED init FAILED");
  }

  if (!storage) return;

  sessionStartMs = millis();

  // 2. Initialize WiFi Access Point
  WiFi.softAP(ssid, password);
  addLog("WiFi AP started, IP: %s", WiFi.softAPIP().toString().c_str());

  // 3. Set Up Web Server Routes

  // Reusable gauge macro for dashboard
  #define GAUGE(label, id, color) \
    "<div style='background:#16213e;border-radius:12px;padding:20px 30px;min-width:120px;'>" \
    "<div style='font-size:0.9em;color:#aaa;'>" label "</div>" \
    "<div id='" id "' style='font-size:2.2em;font-weight:bold;color:" color ";'>--</div></div>"
  #define GAUGE_UNIT(label, id, color, unit) \
    "<div style='background:#16213e;border-radius:12px;padding:20px 30px;min-width:120px;'>" \
    "<div style='font-size:0.9em;color:#aaa;'>" label "</div>" \
    "<div><span id='" id "' style='font-size:2.2em;font-weight:bold;color:" color ";'>--</span>" \
    "<span style='color:#aaa;'> " unit "</span></div></div>"

  server.on("/", HTTP_GET, []() {
    String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
      "<body style='font-family:sans-serif;text-align:center;padding:20px;background:#1a1a2e;color:#eee;'>"
      "<h2>Nexon CAN Logger</h2>"
      "<div style='font-size:0.9em;color:#aaa;margin-bottom:15px;'>Session #" + String(sessionNum) + " &mdash; " + String(sessionFile) + "</div>"
      "<div style='display:flex;justify-content:center;gap:15px;flex-wrap:wrap;margin:25px 0;'>"
        GAUGE("RPM", "rpm", "#e94560")
        GAUGE_UNIT("Speed", "spd", "#0f3460", "km/h")
        GAUGE_UNIT("Coolant", "tmp", "#e9b044", "&deg;C")
        GAUGE_UNIT("Oil Temp", "oil", "#e98044", "&deg;C")
        GAUGE_UNIT("Boost/MAP", "map", "#44e9a5", "kPa")
        GAUGE_UNIT("IAT", "iat", "#44b8e9", "&deg;C")
        GAUGE_UNIT("Load", "load", "#e96044", "%")
        GAUGE_UNIT("Timing", "tmg", "#c844e9", "&deg;")
        GAUGE_UNIT("Throttle", "thr", "#e9e044", "%")
        GAUGE_UNIT("Pedal", "pdl", "#60e944", "%")
        GAUGE_UNIT("Cmd Thr", "cthr", "#44e9c8", "%")
        GAUGE_UNIT("Dem Torque", "dtq", "#e97844", "%")
        GAUGE_UNIT("Act Torque", "atq", "#e94478", "%")
        GAUGE_UNIT("STFT", "stft", "#a0e944", "%")
        GAUGE_UNIT("LTFT", "ltft", "#44e960", "%")
        GAUGE_UNIT("Baro", "baro", "#9e44e9", "kPa")
        GAUGE_UNIT("O2 S2", "o2s2v", "#e98c44", "V")
        GAUGE_UNIT("O2 S2 STFT", "o2s2t", "#e9c044", "%")
        GAUGE("Cmd EQ", "ceq", "#44e9e9")
        GAUGE_UNIT("Cat Temp", "cat", "#e95544", "&deg;C")
        GAUGE_UNIT("O2 Sec LTFT", "o2lt", "#d4e944", "%")
        GAUGE("Gear", "gear", "#fff")
        GAUGE_UNIT("Fuel", "fuel", "#4ae944", "%")
        GAUGE_UNIT("Fuel Rate", "frate", "#e94490", "L/h")
        GAUGE_UNIT("Voltage", "mvolt", "#44e9e9", "V")
        GAUGE_UNIT("Ambient", "amb", "#e9a844", "&deg;C")
        GAUGE_UNIT("Ref Torque", "rtq", "#9e60e9", "N&middot;m")
        GAUGE("O2S1 Lambda", "o2lam", "#e9d044")
        GAUGE_UNIT("O2S1 Current", "o2ma", "#d0e944", "mA")
        GAUGE_UNIT("Abs Load", "absld", "#e96090", "%")
        GAUGE_UNIT("Evap Purge", "evap", "#60c0e9", "%")
      "</div>"
      "<div style='max-width:400px;margin:20px auto;'>"
        "<div style='display:flex;justify-content:space-between;font-size:0.85em;color:#aaa;margin-bottom:4px;'>"
          "<span>Storage</span><span id='stxt'>--</span></div>"
        "<div style='background:#333;border-radius:6px;height:12px;overflow:hidden;'>"
          "<div id='sbar' style='background:#007BFF;height:100%;width:0%;border-radius:6px;transition:width 0.5s;'></div>"
        "</div>"
      "</div>"
      "<a href='/download' style='display:inline-block;padding:12px 22px;background:#007BFF;color:white;text-decoration:none;border-radius:5px;margin:5px;'>Download Current</a>"
      "<a href='/sessions' style='display:inline-block;padding:12px 22px;background:#28a745;color:white;text-decoration:none;border-radius:5px;margin:5px;'>All Sessions</a>"
      "<a href='/log' style='display:inline-block;padding:12px 22px;background:#6c757d;color:white;text-decoration:none;border-radius:5px;margin:5px;'>Debug Log</a>"
      "<a href='/timeouts' style='display:inline-block;padding:12px 22px;background:#e9a844;color:#1a1a2e;text-decoration:none;border-radius:5px;margin:5px;'>PID Status</a>"
      "<script>"
        "setInterval(()=>fetch('/data').then(r=>r.json()).then(d=>{"
          "document.getElementById('rpm').textContent=d.rpm;"
          "document.getElementById('spd').textContent=d.speed;"
          "document.getElementById('tmp').textContent=d.coolant;"
          "document.getElementById('oil').textContent=d.oil;"
          "document.getElementById('map').textContent=d.map;"
          "document.getElementById('iat').textContent=d.iat;"
          "document.getElementById('load').textContent=d.load;"
          "document.getElementById('tmg').textContent=d.timing;"
          "document.getElementById('thr').textContent=d.throttle;"
          "document.getElementById('pdl').textContent=d.pedal;"
          "document.getElementById('cthr').textContent=d.cmdthr;"
          "document.getElementById('dtq').textContent=d.demtq;"
          "document.getElementById('atq').textContent=d.acttq;"
          "document.getElementById('stft').textContent=d.stft;"
          "document.getElementById('ltft').textContent=d.ltft;"
          "document.getElementById('baro').textContent=d.baro;"
          "document.getElementById('o2s2v').textContent=d.o2s2v;"
          "document.getElementById('o2s2t').textContent=d.o2s2t;"
          "document.getElementById('ceq').textContent=d.cmdeq;"
          "document.getElementById('cat').textContent=d.cattemp;"
          "document.getElementById('o2lt').textContent=d.o2lt;"
          "document.getElementById('gear').textContent=d.gear>0?d.gear:'N';"
          "document.getElementById('fuel').textContent=d.fuel;"
          "document.getElementById('frate').textContent=d.frate;"
          "document.getElementById('mvolt').textContent=d.mvolt;"
          "document.getElementById('amb').textContent=d.amb;"
          "document.getElementById('rtq').textContent=d.rtq;"
          "document.getElementById('o2lam').textContent=d.o2s1lam;"
          "document.getElementById('o2ma').textContent=d.o2s1ma;"
          "document.getElementById('absld').textContent=d.absload;"
          "document.getElementById('evap').textContent=d.evap;"
          "var pct=Math.round(d.storage_used*100/d.storage_total);"
          "document.getElementById('sbar').style.width=pct+'%';"
          "document.getElementById('sbar').style.background=pct>90?'#DC3545':pct>70?'#e9b044':'#007BFF';"
          "document.getElementById('stxt').textContent=(d.storage_used/1024).toFixed(1)+' / '+(d.storage_total/1024).toFixed(1)+' MB ('+pct+'%)';"
        "}).catch(()=>{}),1000);"
      "</script>"
      "</body></html>";
    server.send(200, "text/html", html);
  });

  server.on("/data", HTTP_GET, []() {
    uint64_t totalBytes = storageTotalBytes();
    uint64_t usedBytes = storageUsedBytes();
    String json = "{\"rpm\":" + String(rpm) + ",\"speed\":" + String(speed_kmh) + ",\"coolant\":" + String(coolant_temp)
      + ",\"oil\":" + String(oil_temp)
      + ",\"map\":" + String(manifold_kpa) + ",\"iat\":" + String(intake_temp)
      + ",\"load\":" + String(engine_load) + ",\"timing\":" + String(timing_advance, 1)
      + ",\"throttle\":" + String(throttle_pos)
      + ",\"pedal\":" + String(accel_pedal) + ",\"cmdthr\":" + String(cmd_throttle)
      + ",\"demtq\":" + String(demand_torque) + ",\"acttq\":" + String(actual_torque)
      + ",\"stft\":" + String(short_fuel_trim, 1) + ",\"ltft\":" + String(long_fuel_trim, 1)
      + ",\"baro\":" + String(baro_kpa)
      + ",\"o2s2v\":" + String(o2_s2_voltage, 3) + ",\"o2s2t\":" + String(o2_s2_stft, 1)
      + ",\"cmdeq\":" + String(cmd_equiv_ratio, 3)
      + ",\"cattemp\":" + String(catalyst_temp)
      + ",\"o2lt\":" + String(o2_secondary_ltft, 1)
      + ",\"gear\":" + String(estimated_gear)
      + ",\"fuel\":" + String(fuel_level)
      + ",\"frate\":" + String(fuel_rate, 1)
      + ",\"mvolt\":" + String(module_voltage, 2)
      + ",\"amb\":" + String(ambient_temp)
      + ",\"rtq\":" + String(ref_torque)
      + ",\"o2s1lam\":" + String(o2s1_lambda, 3)
      + ",\"o2s1ma\":" + String(o2s1_current, 2)
      + ",\"absload\":" + String(absolute_load, 1)
      + ",\"evap\":" + String(evap_purge)
      + ",\"session\":" + String(sessionNum)
      + ",\"storage_used\":" + String((unsigned long)(usedBytes / 1024))
      + ",\"storage_total\":" + String((unsigned long)(totalBytes / 1024)) + "}";
    server.send(200, "application/json", json);
  });

  // Download a session: /download (current) or /download?s=3
  server.on("/download", HTTP_GET, []() {
    int s = server.hasArg("s") ? server.arg("s").toInt() : sessionNum;
    char path[24];
    snprintf(path, sizeof(path), "/data/s%03d.csv", s);
    if (xSemaphoreTake(fsMutex, pdMS_TO_TICKS(1000))) {
      File downloadFile = storage->open(path, "r");
      if (!downloadFile) {
        xSemaphoreGive(fsMutex);
        server.send(404, "text/plain", "File not found!");
        return;
      }
      char fname[32];
      snprintf(fname, sizeof(fname), "nexon_s%03d.csv", s);
      server.sendHeader("Content-Disposition", String("attachment; filename=\"") + fname + "\"");
      server.streamFile(downloadFile, "text/csv");
      downloadFile.close();
      xSemaphoreGive(fsMutex);
    } else {
      server.send(503, "text/plain", "Busy, try again.");
    }
  });

  // Delete a session: /delete?s=3
  server.on("/delete", HTTP_GET, []() {
    if (!server.hasArg("s")) { server.send(400, "text/plain", "Missing ?s=N"); return; }
    int s = server.arg("s").toInt();
    if (s == sessionNum) { server.send(400, "text/plain", "Cannot delete active session"); return; }
    char path[24];
    snprintf(path, sizeof(path), "/data/s%03d.csv", s);
    if (xSemaphoreTake(fsMutex, pdMS_TO_TICKS(1000))) {
      if (storage->exists(path)) {
        storage->remove(path);
        xSemaphoreGive(fsMutex);
        server.sendHeader("Location", "/sessions");
        server.send(302);
      } else {
        xSemaphoreGive(fsMutex);
        server.send(404, "text/plain", "File not found");
      }
    } else {
      server.send(503, "text/plain", "Busy, try again.");
    }
  });

  // Sessions list page with pagination (10 per page, newest first)
  server.on("/sessions", HTTP_GET, []() {
    int page = server.hasArg("p") ? server.arg("p").toInt() : 1;
    int perPage = 10;
    int totalSessions = sessionNum;
    int totalPages = (totalSessions + perPage - 1) / perPage;
    if (page < 1) page = 1;
    if (page > totalPages) page = totalPages;

    // Sessions on this page: newest first
    int startS = totalSessions - (page - 1) * perPage;
    int endS = startS - perPage + 1;
    if (endS < 1) endS = 1;

    String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
      "<body style='font-family:sans-serif;padding:20px;background:#1a1a2e;color:#eee;max-width:600px;margin:0 auto;'>"
      "<h2 style='text-align:center;'>Sessions</h2>"
      "<div style='text-align:center;margin-bottom:15px;'>"
      "<a href='/' style='color:#007BFF;'>Back to Dashboard</a></div>"
      "<table style='width:100%;border-collapse:collapse;'>"
      "<tr style='border-bottom:1px solid #333;'>"
      "<th style='padding:10px;text-align:left;color:#aaa;'>Session</th>"
      "<th style='padding:10px;text-align:right;color:#aaa;'>Size</th>"
      "<th style='padding:10px;text-align:right;color:#aaa;'>Actions</th></tr>";

    if (xSemaphoreTake(fsMutex, pdMS_TO_TICKS(1000))) {
      for (int s = startS; s >= endS; s--) {
        char path[24];
        snprintf(path, sizeof(path), "/data/s%03d.csv", s);
        File f = storage->open(path, "r");
        if (f) {
          size_t sz = f.size();
          f.close();
          String sizeStr;
          if (sz < 1024) sizeStr = String(sz) + " B";
          else if (sz < 1024 * 1024) sizeStr = String(sz / 1024) + " KB";
          else sizeStr = String(sz / (1024 * 1024)) + " MB";

          bool active = (s == sessionNum);
          html += "<tr style='border-bottom:1px solid #222;'>"
            "<td style='padding:10px;'>" + String(active ? "<b>" : "") + "s" + String(s, DEC) + (active ? " (active)</b>" : "") + "</td>"
            "<td style='padding:10px;text-align:right;'>" + sizeStr + "</td>"
            "<td style='padding:10px;text-align:right;'>"
            "<a href='/download?s=" + String(s) + "' style='color:#007BFF;margin-right:10px;'>DL</a>";
          if (!active) {
            html += "<a href='/delete?s=" + String(s) + "' style='color:#DC3545;' onclick='return confirm(\"Delete session " + String(s) + "?\")'>Del</a>";
          }
          html += "</td></tr>";
        }
      }
      xSemaphoreGive(fsMutex);
    }

    html += "</table>";

    // Pagination
    if (totalPages > 1) {
      html += "<div style='text-align:center;margin-top:20px;'>";
      if (page > 1)
        html += "<a href='/sessions?p=" + String(page - 1) + "' style='color:#007BFF;margin:0 10px;'>&laquo; Newer</a>";
      html += "<span style='color:#aaa;'>Page " + String(page) + " / " + String(totalPages) + "</span>";
      if (page < totalPages)
        html += "<a href='/sessions?p=" + String(page + 1) + "' style='color:#007BFF;margin:0 10px;'>Older &raquo;</a>";
      html += "</div>";
    }

    html += "</body></html>";
    server.send(200, "text/html", html);
  });

  server.on("/log", HTTP_GET, []() {
    String out;
    out.reserve(LOG_LINES * LOG_LINE_LEN);
    int total = logIndex < LOG_LINES ? logIndex : LOG_LINES;
    int start = logIndex < LOG_LINES ? 0 : logIndex - LOG_LINES;
    for (int i = start; i < start + total; i++) {
      out += logBuf[i % LOG_LINES];
      out += '\n';
    }
    server.send(200, "text/plain", out);
  });

  server.on("/timeouts", HTTP_GET, []() {
    String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
      "<body style='font-family:sans-serif;padding:20px;background:#1a1a2e;color:#eee;max-width:600px;margin:0 auto;'>"
      "<h2 style='text-align:center;'>PID Status</h2>"
      "<div style='text-align:center;margin-bottom:15px;'>"
      "<a href='/' style='color:#007BFF;'>Back to Dashboard</a></div>"
      "<table style='width:100%;border-collapse:collapse;'>"
      "<tr style='border-bottom:1px solid #333;'>"
      "<th style='padding:8px;text-align:left;color:#aaa;'>PID</th>"
      "<th style='padding:8px;text-align:right;color:#aaa;'>OK</th>"
      "<th style='padding:8px;text-align:right;color:#aaa;'>Timeouts</th>"
      "<th style='padding:8px;text-align:right;color:#aaa;'>Status</th></tr>";
    for (int i = 0; i < numTrackedPids; i++) {
      PidStats* s = &pidStats[i];
      const char* status = s->disabled ? "<span style='color:#DC3545;'>Disabled</span>" : "<span style='color:#28a745;'>Active</span>";
      char pidHex[8];
      snprintf(pidHex, sizeof(pidHex), "0x%02X", s->pid);
      html += "<tr style='border-bottom:1px solid #222;'>"
        "<td style='padding:8px;'>" + String(pidHex) + "</td>"
        "<td style='padding:8px;text-align:right;'>" + String(s->responses) + "</td>"
        "<td style='padding:8px;text-align:right;'>" + String(s->timeouts) + "</td>"
        "<td style='padding:8px;text-align:right;'>" + String(status) + "</td></tr>";
    }
    html += "</table>"
      "<div style='text-align:center;margin-top:20px;font-size:0.85em;color:#aaa;'>"
      "PIDs auto-disable after " + String(PID_DISABLE_THRESHOLD) + " consecutive timeouts</div>"
      "</body></html>";
    server.send(200, "text/html", html);
  });

  server.begin();

  // Launch web server on Core 0
  xTaskCreatePinnedToCore(
    webServerTask,  // task function
    "WebServer",    // name
    4096,           // stack size (bytes)
    NULL,           // parameter
    1,              // priority
    NULL,           // task handle
    0               // Core 0
  );

  // 4. Initialize TWAI (CAN Bus) for Standard OBD2 (500 kbps)
  twai_general_config_t g_config = TWAI_GENERAL_CONFIG_DEFAULT((gpio_num_t)TX_PIN, (gpio_num_t)RX_PIN, TWAI_MODE_NORMAL);
  twai_timing_config_t t_config = TWAI_TIMING_CONFIG_500KBITS();
  twai_filter_config_t f_config = TWAI_FILTER_CONFIG_ACCEPT_ALL();

  esp_err_t err = twai_driver_install(&g_config, &t_config, &f_config);
  if (err == ESP_OK) {
    addLog("TWAI driver installed OK");
  } else {
    addLog("TWAI driver install FAILED: 0x%X", err);
  }
  err = twai_start();
  if (err == ESP_OK) {
    addLog("TWAI driver started OK");
  } else {
    addLog("TWAI start FAILED: 0x%X", err);
  }

}

uint8_t getNextPid() {
  pollCounter++;
  if (pollCounter % SLOW_POLL_RATIO == 0) {
    for (int i = 0; i < NUM_SLOW; i++) {
      uint8_t pid = slow_pids[slowIndex];
      slowIndex = (slowIndex + 1) % NUM_SLOW;
      if (!isPidDisabled(pid)) return pid;
    }
    // All slow PIDs disabled, fall through to fast
  }
  for (int i = 0; i < NUM_FAST; i++) {
    uint8_t pid = fast_pids[fastIndex];
    fastIndex = (fastIndex + 1) % NUM_FAST;
    if (!isPidDisabled(pid)) return pid;
  }
  return 0x00; // all disabled (shouldn't happen)
}

void sendObd2Request(uint8_t pid) {
  twai_message_t message;
  message.identifier = 0x7DF; // Standard OBD2 broadcast ID
  message.extd = 0;           // Standard 11-bit ID
  message.data_length_code = 8;
  message.data[0] = 0x02;     // 2 additional data bytes follow
  message.data[1] = 0x01;     // Service 01 (Show current data)
  message.data[2] = pid;
  for (int i = 3; i < 8; i++) message.data[i] = 0x00;

  esp_err_t txErr = twai_transmit(&message, pdMS_TO_TICKS(10));
  if (txErr == ESP_OK) {
    txCount++;
    requestPending = true;
    requestSentTime = millis();
    pendingPid = pid;
  } else {
    addLog("TX fail PID 0x%02X err=0x%X", pid, txErr);
  }
}

void parseObd2Response(twai_message_t &rx_msg) {
  uint8_t pid = rx_msg.data[2];

  switch (pid) {
    case 0x0C: // RPM: ((A*256)+B)/4
      rpm = ((rx_msg.data[3] * 256) + rx_msg.data[4]) / 4;
      break;
    case 0x0D: // Vehicle Speed: A km/h
      speed_kmh = rx_msg.data[3];
      break;
    case 0x05: // Coolant Temp: A - 40 °C
      coolant_temp = rx_msg.data[3] - 40;
      break;
    case 0x0B: // Intake Manifold Pressure: A kPa
      manifold_kpa = rx_msg.data[3];
      break;
    case 0x0F: // Intake Air Temp: A - 40 °C
      intake_temp = rx_msg.data[3] - 40;
      break;
    case 0x04: // Engine Load: A * 100 / 255 %
      engine_load = (rx_msg.data[3] * 100) / 255;
      break;
    case 0x0E: // Timing Advance: A / 2 - 64 degrees
      timing_advance = rx_msg.data[3] / 2.0 - 64.0;
      break;
    case 0x47: // Absolute Throttle Position: A * 100 / 255 %
      throttle_pos = (rx_msg.data[3] * 100) / 255;
      break;
    case 0x5A: // Relative Accelerator Pedal Position: A * 100 / 255 %
      accel_pedal = (rx_msg.data[3] * 100) / 255;
      break;
    case 0x4C: // Commanded Throttle Actuator: A * 100 / 255 %
      cmd_throttle = (rx_msg.data[3] * 100) / 255;
      break;
    case 0x61: // Driver's Demand Engine Torque: A - 125 %
      demand_torque = rx_msg.data[3] - 125;
      break;
    case 0x62: // Actual Engine Torque: A - 125 %
      actual_torque = rx_msg.data[3] - 125;
      break;
    case 0x06: // Short Term Fuel Trim Bank 1: (A - 128) * 100 / 128 %
      short_fuel_trim = (rx_msg.data[3] - 128) * 100.0 / 128.0;
      break;
    case 0x07: // Long Term Fuel Trim Bank 1: (A - 128) * 100 / 128 %
      long_fuel_trim = (rx_msg.data[3] - 128) * 100.0 / 128.0;
      break;
    case 0x33: // Barometric Pressure: A kPa
      baro_kpa = rx_msg.data[3];
      break;
    case 0x15: // O2 Sensor 2 Bank 1: Voltage = A / 200, STFT = (B-128)*100/128
      o2_s2_voltage = rx_msg.data[3] / 200.0;
      o2_s2_stft = (rx_msg.data[4] - 128) * 100.0 / 128.0;
      break;
    case 0x44: // Fuel/Air Commanded Equivalence Ratio: ((A*256)+B) / 32768
      cmd_equiv_ratio = ((rx_msg.data[3] * 256) + rx_msg.data[4]) * 2.0 / 65536.0;
      break;
    case 0x3C: // Catalyst Temp Bank 1 Sensor 1: ((A*256)+B)/10 - 40 °C
      catalyst_temp = ((rx_msg.data[3] * 256) + rx_msg.data[4]) / 10 - 40;
      break;
    case 0x56: // Long Term Secondary O2 Trim Bank 1: (A-128)*100/128 %
      o2_secondary_ltft = (rx_msg.data[3] - 128) * 100.0 / 128.0;
      break;
    case 0x5C: // Engine Oil Temperature: A - 40 °C
      oil_temp = rx_msg.data[3] - 40;
      break;
    case 0x2F: // Fuel Tank Level Input: A * 100 / 255 %
      fuel_level = (rx_msg.data[3] * 100) / 255;
      break;
    case 0x5E: // Engine fuel rate: ((A*256)+B) / 20  L/h
      fuel_rate = ((rx_msg.data[3] * 256) + rx_msg.data[4]) / 20.0;
      break;
    case 0x42: // Control module voltage: ((A*256)+B) / 1000  V
      module_voltage = ((rx_msg.data[3] * 256) + rx_msg.data[4]) / 1000.0;
      break;
    case 0x46: // Ambient air temperature: A - 40  °C
      ambient_temp = rx_msg.data[3] - 40;
      break;
    case 0x63: // Engine reference torque: (A*256)+B  N·m
      ref_torque = (rx_msg.data[3] * 256) + rx_msg.data[4];
      break;
    case 0x34: // O2S1 WR: Lambda = ((A*256)+B)/32768, Current = ((C*256)+D)/256 - 128
      o2s1_lambda = ((rx_msg.data[3] * 256) + rx_msg.data[4]) / 32768.0;
      o2s1_current = ((rx_msg.data[5] * 256) + rx_msg.data[6]) / 256.0 - 128.0;
      break;
    case 0x43: // Absolute load: ((A*256)+B)*100/255  %
      absolute_load = ((rx_msg.data[3] * 256) + rx_msg.data[4]) * 100.0 / 255.0;
      break;
    case 0x2E: // Commanded evap purge: A*100/255  %
      evap_purge = (rx_msg.data[3] * 100) / 255;
      break;
  }
}

void loop() {
  // --- Step 1: Send next request if none pending ---
  if (!requestPending) {
    uint8_t nextPid = getNextPid();
    sendObd2Request(nextPid);
  }

  // --- Step 2: Timeout pending request after RESPONSE_TIMEOUT_MS ---
  if (requestPending && (millis() - requestSentTime >= RESPONSE_TIMEOUT_MS)) {
    requestPending = false;
    rxTimeoutCount++;
    PidStats* s = getPidStats(pendingPid);
    if (s) {
      s->timeouts++;
      s->consecutiveTimeouts++;
      if (!s->disabled && s->consecutiveTimeouts >= PID_DISABLE_THRESHOLD) {
        s->disabled = true;
        addLog("PID 0x%02X disabled after %d timeouts", s->pid, PID_DISABLE_THRESHOLD);
      }
    }
  }

  // --- Step 3: Try to receive a response ---
  twai_message_t rx_msg;
  esp_err_t rxErr = twai_receive(&rx_msg, pdMS_TO_TICKS(5));
  if (rxErr == ESP_OK) {
    rxCount++;
    if (rx_msg.identifier == 0x7E8 && rx_msg.data[1] == 0x41) {
      parseObd2Response(rx_msg);
      PidStats* s = getPidStats(rx_msg.data[2]);
      if (s) { s->responses++; s->consecutiveTimeouts = 0; }
      if (requestPending && rx_msg.data[2] == pendingPid) {
        requestPending = false;
      }
    }
  }

  // --- Step 4: Estimate gear and log at 250ms interval ---
  if (millis() - lastLog >= 250) {
    lastLog = millis();

    // Estimate gear from RPM/Speed ratio
    if (speed_kmh >= 5 && rpm > 0) {
      float ratio = (float)rpm / speed_kmh;
      estimated_gear = 0;
      for (int i = 0; i < 6; i++) {
        if (ratio >= GEAR_RATIO_MIN[i] && ratio <= GEAR_RATIO_MAX[i]) {
          estimated_gear = i + 1;
          break;
        }
      }
    } else {
      estimated_gear = 0; // Neutral / clutch / stationary
    }

    // Periodic debug status log (every 5s)
    if (millis() - lastStatusLog >= 5000) {
      lastStatusLog = millis();
      addLog("TX:%lu RX:%lu RX_timeout:%lu", txCount, rxCount, rxTimeoutCount);
      twai_status_info_t status;
      if (twai_get_status_info(&status) == ESP_OK) {
        addLog("TWAI state:%d txErr:%lu rxErr:%lu txQ:%lu rxQ:%lu",
               status.state, status.tx_error_counter, status.rx_error_counter,
               status.msgs_to_tx, status.msgs_to_rx);
      }
    }

    unsigned long timestamp = millis();

    uint64_t freeBytes = storageTotalBytes() - storageUsedBytes();
    if (freeBytes > 512) {
      if (xSemaphoreTake(fsMutex, pdMS_TO_TICKS(50))) {
        File file = storage->open(sessionFile, "a");
        if (file) {
          file.printf("%lu,%d,%d,%d,%d,%d,%d,%d,%.1f,%d,%d,%d,%d,%d,%.1f,%.1f,%d,%.3f,%.1f,%.3f,%d,%.1f,%d,%d,%.1f,%.2f,%d,%d,%.3f,%.2f,%.1f,%d\n",
            timestamp, rpm, speed_kmh, coolant_temp, oil_temp, manifold_kpa, intake_temp, engine_load,
            timing_advance, throttle_pos, accel_pedal, cmd_throttle, demand_torque, actual_torque,
            short_fuel_trim, long_fuel_trim, baro_kpa, o2_s2_voltage, o2_s2_stft,
            cmd_equiv_ratio, catalyst_temp, o2_secondary_ltft, estimated_gear, fuel_level,
            fuel_rate, module_voltage, ambient_temp, ref_torque,
            o2s1_lambda, o2s1_current, absolute_load, evap_purge);
          file.close();
        }
        xSemaphoreGive(fsMutex);
      }
    } else {
      Serial.println("WARNING: Storage full! Logging stopped. Download CSV and clear data.");
    }

    Serial.printf("RPM:%d Spd:%d Clt:%d Oil:%d MAP:%d IAT:%d Load:%d%% Tmg:%.1f Thr:%d%% Pedal:%d%% CmdThr:%d%% DmTq:%d%% AcTq:%d%% STFT:%.1f LTFT:%.1f Baro:%d O2S2:%.3fV/%.1f%% CmdEq:%.3f Cat:%dC SecLTFT:%.1f Gear:%d Fuel:%d%% FRate:%.1f Volt:%.2f Amb:%d RefTq:%d Lam:%.3f O2mA:%.2f AbsLd:%.1f Evap:%d%%\n",
                   rpm, speed_kmh, coolant_temp, oil_temp, manifold_kpa, intake_temp, engine_load,
                   timing_advance, throttle_pos, accel_pedal, cmd_throttle, demand_torque,
                   actual_torque, short_fuel_trim, long_fuel_trim, baro_kpa,
                   o2_s2_voltage, o2_s2_stft,
                   cmd_equiv_ratio, catalyst_temp, o2_secondary_ltft, estimated_gear, fuel_level,
                   fuel_rate, module_voltage, ambient_temp, ref_torque,
                   o2s1_lambda, o2s1_current, absolute_load, evap_purge);

    // Update OLED display every 500ms
    if (millis() - lastOledUpdate >= OLED_UPDATE_MS) {
      lastOledUpdate = millis();
      updateOled();
    }
  }
}
