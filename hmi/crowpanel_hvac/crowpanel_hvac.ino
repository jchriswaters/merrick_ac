/*
 * crowpanel_hvac.ino
 * Elecrow CrowPanel 2.1" ESP32-S3 — HVAC HMI
 *
 * Four-screen LVGL control panel for the Uno Q HVAC controller.
 * Communicates exclusively via MQTT — no direct wiring to the controller.
 *
 *   Screen 0  STATUS     — mode, indoor/outdoor temp+humidity, compressor state
 *   Screen 1  OUTPUTS    — live state of all 9 relay outputs
 *   Screen 2  SETPOINTS  — edit all 5 threshold parameters via rotary knob
 *   Screen 3  ZONES      — theater zone enable, system mode override
 *
 * Encoder UX:
 *   Rotate (nav mode)  — cycle between screens  /  move setpoint selection
 *   Short press        — on Setpoints: enter edit mode
 *                        on Zones: toggle selected control
 *                        on Status/Outputs: no action
 *   Rotate (edit mode) — increment / decrement setpoint value
 *   Short press (edit) — publish setConfig command, return to nav mode
 *
 * ── HARDWARE SETUP ──────────────────────────────────────────────────────
 * This sketch requires the Elecrow CrowPanel 2.1" board package and its
 * bundled LVGL port. Steps before flashing:
 *
 *  1. In Arduino IDE → File → Preferences → Additional boards URL:
 *       https://raw.githubusercontent.com/elecrow-rd/Elecrow-CrowPanel-HMI-Solutions/master/package_elecrow_index.json
 *  2. Install "Elecrow CrowPanel" board package via Boards Manager.
 *  3. Run one of Elecrow's LVGL examples to confirm display and touch work.
 *  4. Replace Elecrow's example app code with this sketch.
 *
 * The display driver, LVGL port init (lv_port_disp_init / lv_port_indev_init),
 * and LVGL tick timer are handled by Elecrow's BSP. The DISPLAY INIT section
 * below shows where to call those functions. Follow the register map in the
 * Elecrow example for your specific board revision.
 *
 * ── DEPENDENCIES ────────────────────────────────────────────────────────
 *   Install via Library Manager:
 *     - PubSubClient  by Nick O'Leary  (MQTT client)
 *     - ArduinoJson   by Benoit Blanchon  (JSON parsing)
 *     - LVGL          (usually included in Elecrow board package)
 *
 * ── IMPORTANT LVGL SETTINGS ─────────────────────────────────────────────
 *   In lv_conf.h (inside the Elecrow board package or LVGL library folder):
 *     #define LV_FONT_MONTSERRAT_14   1
 *     #define LV_FONT_MONTSERRAT_28   1
 *     #define LV_FONT_MONTSERRAT_48   1
 *     #define LV_COLOR_DEPTH          16
 *     #define LV_HOR_RES_MAX         480
 *     #define LV_VER_RES_MAX         480
 *
 * See docs/system-design.md §6d for architecture overview.
 * See docs/mqtt-payload-spec.md for topic and field reference.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <lvgl.h>

#include "config.h"

// ─────────────────────────────────────────────────────────────
// DISPLAY INIT  — Elecrow BSP
// Replace the stub calls below with the init functions from
// Elecrow's LVGL example for your board revision.
// ─────────────────────────────────────────────────────────────
// Typical pattern (exact names vary by BSP version):
//
//   #include <CrowPanel_HMI.h>      // or your Elecrow display header
//   CrowPanel_HMI panel;
//
//   void hw_init() {
//       panel.begin();              // display + LVGL + tick timer
//       // touch is usually registered inside panel.begin()
//   }
//
// Until you have the exact BSP header, keep hw_init() as a stub
// and the rest of this file will compile for testing logic.

void hw_init() {
    // ── INSERT Elecrow BSP initialisation here ────────────────
    // Example (fill in your actual function names):
    //   lv_init();
    //   lv_port_disp_init();    // display driver registration
    //   lv_port_indev_init();   // touch driver registration
    //   // Start LVGL tick via ESP32 hardware timer (1 ms):
    //   // timerBegin / timerAttachInterrupt → lv_tick_inc(1)
}

// ─────────────────────────────────────────────────────────────
// COLOUR PALETTE  (matches web UI dark theme)
// ─────────────────────────────────────────────────────────────
#define C_BG        0x1a1a2e
#define C_PANEL     0x16213e
#define C_BORDER    0x0f3460
#define C_ACCENT    0xe94560
#define C_GREEN     0x27ae60
#define C_YELLOW    0xf39c12
#define C_RED       0xe74c3c
#define C_BLUE      0x2980b9
#define C_TEXT      0xeaeaea
#define C_MUTED     0x8892a4

// ─────────────────────────────────────────────────────────────
// LIVE STATE  (updated from MQTT callbacks)
// ─────────────────────────────────────────────────────────────

struct HvacStatus {
    char    mode[20]        = "off";
    bool    compressorOn    = false;
    float   indoorTempF     = 0;
    float   indoorHumPct    = 0;
    float   outdoorTempF    = 0;
    float   outdoorHumPct   = 0;
    // Relay outputs
    bool    highCool        = false;
    bool    lowCool         = false;
    bool    highHeat        = false;
    bool    revValve        = false;
    bool    fanOn           = false;
    bool    theaterDamper   = false;
    bool    downDamper      = false;
    bool    ventOpen        = false;
    bool    dehumOn         = false;
    // Last update epoch
    unsigned long updatedAt = 0;
} hvac;

struct HvacConfig {
    int  heatPumpMinTempF  = 40;
    int  freeCoolMaxTempF  = 60;
    int  highHumidityPct   = 80;
    int  dehumMaxMinutes   = 20;
    int  ventMinPerHour    = 10;
    bool theaterEnabled    = false;
    char modeOverride[8]   = "auto";
} cfg;

volatile bool status_dirty = false;   // set in MQTT callback, cleared in loop
volatile bool config_dirty = false;

// ─────────────────────────────────────────────────────────────
// SETPOINT TABLE  (drives Screen 2)
// ─────────────────────────────────────────────────────────────

struct Setpoint {
    const char *label;
    const char *unit;
    int        *value;
    int         minVal;
    int         maxVal;
    const char *mqttKey;
};

Setpoint setpoints[] = {
    { "Heat Pump Min Temp", "F",    &cfg.heatPumpMinTempF, 20,  60, "heat_pump_min_temp_f" },
    { "Free Cool Max Temp", "F",    &cfg.freeCoolMaxTempF, 40,  80, "free_cool_max_temp_f" },
    { "Humidity Vent Limit", "%",   &cfg.highHumidityPct,  50, 100, "high_humidity_pct"    },
    { "Dehum Timeout",      "min",  &cfg.dehumMaxMinutes,   5, 120, "dehum_max_minutes"    },
    { "Vent Schedule",      "m/hr", &cfg.ventMinPerHour,    0,  60, "vent_minutes_per_hour"},
};
constexpr int NUM_SETPOINTS = 5;

// ─────────────────────────────────────────────────────────────
// ENCODER STATE
// ─────────────────────────────────────────────────────────────

volatile int  g_enc_delta  = 0;    // net rotation steps since last read
volatile bool g_btn_event  = false;
static   uint8_t enc_last_a = HIGH;

unsigned long lastBtnMs = 0;
constexpr unsigned long BTN_DEBOUNCE_MS = 50;

void IRAM_ATTR enc_isr_a() {
    uint8_t a = digitalRead(ENC_A);
    if (a != enc_last_a) {
        enc_last_a = a;
        if (a == HIGH) {
            // Rising edge on A: direction from B
            g_enc_delta += (digitalRead(ENC_B) == LOW) ? 1 : -1;
        }
    }
}

void IRAM_ATTR btn_isr() {
    unsigned long now = millis();
    if ((now - lastBtnMs) > BTN_DEBOUNCE_MS) {
        if (digitalRead(ENC_BTN) == LOW) {
            g_btn_event = true;
            lastBtnMs = now;
        }
    }
}

// Read and clear encoder delta atomically
int read_encoder() {
    noInterrupts();
    int d = g_enc_delta;
    g_enc_delta = 0;
    interrupts();
    return d;
}

bool read_button() {
    noInterrupts();
    bool b = g_btn_event;
    g_btn_event = false;
    interrupts();
    return b;
}

// ─────────────────────────────────────────────────────────────
// SCREEN / NAVIGATION STATE
// ─────────────────────────────────────────────────────────────

int  currentScreen    = 0;
constexpr int NUM_SCREENS = 4;

// Setpoints screen sub-state
int  selectedSp       = 0;     // which setpoint row is highlighted (0-4)
bool editingSetpoint  = false; // true = encoder changes the value, not the screen
int  editTempValue    = 0;     // working copy while editing

// Zones screen sub-state
int  selectedZoneCtrl = 0;     // 0 = theater toggle, 1 = mode override

// ─────────────────────────────────────────────────────────────
// LVGL UI OBJECTS  (global so update functions can reach them)
// ─────────────────────────────────────────────────────────────

// Screens
lv_obj_t *scr[NUM_SCREENS];   // 0=Status 1=Outputs 2=Setpoints 3=Zones

// Status screen
lv_obj_t *lbl_mode_badge;
lv_obj_t *lbl_comp;
lv_obj_t *lbl_in_temp, *lbl_in_hum;
lv_obj_t *lbl_out_temp, *lbl_out_hum;
lv_obj_t *lbl_status_ts;

// Outputs screen
lv_obj_t *lbl_relay[9];        // one label per relay (showing name + state)

// Setpoints screen
lv_obj_t *lbl_sp_index;        // "2 / 5"
lv_obj_t *lbl_sp_name;         // parameter name
lv_obj_t *lbl_sp_value;        // current (or editing) value
lv_obj_t *lbl_sp_unit;         // unit string
lv_obj_t *lbl_sp_hint;         // "Rotate: select   Press: edit"  etc.

// Zones screen
lv_obj_t *lbl_theater_state;
lv_obj_t *lbl_override_state;
lv_obj_t *lbl_zone_cursor;     // ">" indicator

// ─────────────────────────────────────────────────────────────
// MQTT + WiFi
// ─────────────────────────────────────────────────────────────

WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);

void publish_setconfig(const char *key, int value) {
    char buf[128];
    snprintf(buf, sizeof(buf),
             "{\"cmd\":\"setConfig\",\"key\":\"%s\",\"value\":%d}", key, value);
    mqtt.publish(TOPIC_CMD, buf, false);
    Serial.printf("[MQTT] CMD → %s\n", buf);
}

void publish_setconfig_str(const char *key, const char *value) {
    char buf[128];
    snprintf(buf, sizeof(buf),
             "{\"cmd\":\"setConfig\",\"key\":\"%s\",\"value\":\"%s\"}", key, value);
    mqtt.publish(TOPIC_CMD, buf, false);
}

void publish_setconfig_bool(const char *key, bool value) {
    char buf[128];
    snprintf(buf, sizeof(buf),
             "{\"cmd\":\"setConfig\",\"key\":\"%s\",\"value\":%s}",
             key, value ? "true" : "false");
    mqtt.publish(TOPIC_CMD, buf, false);
}

// ─────────────────────────────────────────────────────────────
// MQTT CALLBACK — runs in main loop context (inside mqtt.loop())
// Parse incoming status and config payloads, set dirty flags.
// LVGL updates happen in the main loop after the callback returns.
// ─────────────────────────────────────────────────────────────

void on_mqtt_message(char *topic, byte *payload, unsigned int len) {
    // Make null-terminated copy (payload is NOT null-terminated by PubSubClient)
    char msg[len + 1];
    memcpy(msg, payload, len);
    msg[len] = '\0';

    DynamicJsonDocument doc(4096);
    if (deserializeJson(doc, msg)) {
        Serial.printf("[MQTT] JSON parse error on %s\n", topic);
        return;
    }

    if (strcmp(topic, TOPIC_STATUS) == 0) {
        strncpy(hvac.mode, doc["mode"] | "off", sizeof(hvac.mode) - 1);
        hvac.compressorOn  = doc["compressor_on"] | false;
        hvac.indoorTempF   = doc["indoor_temp_f"]      | 0.0f;
        hvac.indoorHumPct  = doc["indoor_humidity_pct"]| 0.0f;
        hvac.outdoorTempF  = doc["outdoor_temp_f"]     | 0.0f;
        hvac.outdoorHumPct = doc["outdoor_humidity_pct"]| 0.0f;
        hvac.highCool      = doc["high_cool"]       | false;
        hvac.lowCool       = doc["low_cool"]        | false;
        hvac.highHeat      = doc["high_heat"]       | false;
        hvac.revValve      = doc["reversing_valve"] | false;
        hvac.fanOn         = doc["fan_on"]          | false;
        hvac.theaterDamper = doc["theater_damper"]  | false;
        hvac.downDamper    = doc["downstairs_damper"]| false;
        hvac.ventOpen      = doc["vent_open"]       | false;
        hvac.dehumOn       = doc["dehumidifier_on"] | false;
        hvac.updatedAt     = millis();
        status_dirty = true;

    } else if (strcmp(topic, TOPIC_CONFIG) == 0) {
        cfg.heatPumpMinTempF = doc["heat_pump_min_temp_f"] | 40;
        cfg.freeCoolMaxTempF = doc["free_cool_max_temp_f"] | 60;
        cfg.highHumidityPct  = doc["high_humidity_pct"]    | 80;
        cfg.dehumMaxMinutes  = doc["dehum_max_minutes"]    | 20;
        cfg.ventMinPerHour   = doc["vent_minutes_per_hour"]| 10;
        cfg.theaterEnabled   = doc["theater_enabled"]      | false;
        strncpy(cfg.modeOverride, doc["mode_override"] | "auto",
                sizeof(cfg.modeOverride) - 1);
        config_dirty = true;
    }
}

// ─────────────────────────────────────────────────────────────
// WIFI + MQTT CONNECTION MANAGEMENT
// ─────────────────────────────────────────────────────────────

void wifi_connect() {
    if (WiFi.status() == WL_CONNECTED) return;
    Serial.printf("[WiFi] Connecting to %s", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    unsigned long t = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - t) < 15000) {
        delay(300);
        Serial.print('.');
    }
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n[WiFi] Connected — IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
        Serial.println("\n[WiFi] Connection failed — will retry");
    }
}

void mqtt_reconnect() {
    if (mqtt.connected()) return;
    Serial.printf("[MQTT] Connecting to %s:%d ...\n", MQTT_HOST, MQTT_PORT);
    bool ok;
    if (strlen(MQTT_USER) > 0) {
        ok = mqtt.connect("hvac-hmi", MQTT_USER, MQTT_PASS);
    } else {
        ok = mqtt.connect("hvac-hmi");
    }
    if (ok) {
        Serial.println("[MQTT] Connected");
        mqtt.subscribe(TOPIC_STATUS, 0);
        mqtt.subscribe(TOPIC_CONFIG, 1);
    } else {
        Serial.printf("[MQTT] Failed (state=%d)\n", mqtt.state());
    }
}

// ─────────────────────────────────────────────────────────────
// LVGL HELPER — quick style setters
// ─────────────────────────────────────────────────────────────

static inline void obj_bg(lv_obj_t *obj, uint32_t hex) {
    lv_obj_set_style_bg_color(obj, lv_color_hex(hex), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, LV_PART_MAIN);
}
static inline void obj_text_color(lv_obj_t *obj, uint32_t hex) {
    lv_obj_set_style_text_color(obj, lv_color_hex(hex), LV_PART_MAIN);
}
static inline void obj_border(lv_obj_t *obj, uint32_t hex, int w = 1) {
    lv_obj_set_style_border_color(obj, lv_color_hex(hex), LV_PART_MAIN);
    lv_obj_set_style_border_width(obj, w, LV_PART_MAIN);
}
static lv_obj_t *make_label(lv_obj_t *parent, const char *txt,
                              const lv_font_t *font, uint32_t color,
                              int x, int y, lv_text_align_t align = LV_TEXT_ALIGN_CENTER) {
    lv_obj_t *lbl = lv_label_create(parent);
    lv_label_set_text(lbl, txt);
    lv_obj_set_style_text_font(lbl, font, LV_PART_MAIN);
    obj_text_color(lbl, color);
    lv_obj_set_style_text_align(lbl, align, LV_PART_MAIN);
    lv_obj_set_pos(lbl, x, y);
    return lbl;
}

// ─────────────────────────────────────────────────────────────
// SCREEN BUILDERS
// All screens use a 480×480 canvas. Keep content within the
// inscribed circle (~220 px radius from centre = point 240,240).
// ─────────────────────────────────────────────────────────────

// ── Screen 0: Status ──────────────────────────────────────────
void build_status_screen() {
    lv_obj_t *s = lv_obj_create(NULL);
    obj_bg(s, C_BG);
    lv_obj_clear_flag(s, LV_OBJ_FLAG_SCROLLABLE);

    // Screen title
    make_label(s, "STATUS", &lv_font_montserrat_14, C_MUTED, 0, 32);
    lv_obj_set_width(lv_obj_get_child(s, -1), 480);

    // Mode badge (large, centre)
    lbl_mode_badge = lv_label_create(s);
    lv_obj_set_size(lbl_mode_badge, 300, 70);
    lv_obj_set_pos(lbl_mode_badge, 90, 185);
    obj_bg(lbl_mode_badge, C_BORDER);
    lv_obj_set_style_radius(lbl_mode_badge, 12, LV_PART_MAIN);
    lv_obj_set_style_text_font(lbl_mode_badge, &lv_font_montserrat_28, LV_PART_MAIN);
    obj_text_color(lbl_mode_badge, C_TEXT);
    lv_obj_set_style_text_align(lbl_mode_badge, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_set_style_pad_all(lbl_mode_badge, 10, LV_PART_MAIN);
    lv_label_set_text(lbl_mode_badge, "—");

    // Compressor state
    lbl_comp = make_label(s, "Compressor: —", &lv_font_montserrat_14, C_MUTED, 0, 265);
    lv_obj_set_width(lbl_comp, 480);
    lv_obj_set_style_text_align(lbl_comp, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

    // Indoor
    make_label(s, "INDOOR", &lv_font_montserrat_14, C_MUTED, 60, 110);
    lbl_in_temp = make_label(s, "—", &lv_font_montserrat_28, C_TEXT, 60, 130);
    lbl_in_hum  = make_label(s, "—", &lv_font_montserrat_14, C_MUTED, 60, 165);

    // Outdoor
    make_label(s, "OUTDOOR", &lv_font_montserrat_14, C_MUTED, 310, 110);
    lbl_out_temp = make_label(s, "—", &lv_font_montserrat_28, C_TEXT, 295, 130);
    lbl_out_hum  = make_label(s, "—", &lv_font_montserrat_14, C_MUTED, 295, 165);

    // Timestamp (bottom)
    lbl_status_ts = make_label(s, "No data", &lv_font_montserrat_14, C_MUTED, 0, 395);
    lv_obj_set_width(lbl_status_ts, 480);
    lv_obj_set_style_text_align(lbl_status_ts, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

    scr[0] = s;
}

// ── Screen 1: Relay Outputs ───────────────────────────────────
void build_outputs_screen() {
    static const char *relay_names[] = {
        "High Cool", "Low Cool", "Aux Heat", "Rev Valve",
        "Fan", "Theater Dmpr", "Dwn Dmpr", "Vent", "Dehum"
    };

    lv_obj_t *s = lv_obj_create(NULL);
    obj_bg(s, C_BG);
    lv_obj_clear_flag(s, LV_OBJ_FLAG_SCROLLABLE);

    make_label(s, "RELAY OUTPUTS", &lv_font_montserrat_14, C_MUTED, 0, 32);
    lv_obj_set_width(lv_obj_get_child(s, -1), 480);

    // 3×3 grid of relay chips, centred on 480×480 round display
    const int cols = 3, rows = 3;
    const int cell_w = 130, cell_h = 52, gap_x = 10, gap_y = 10;
    const int grid_w = cols * cell_w + (cols - 1) * gap_x;
    const int grid_h = rows * cell_h + (rows - 1) * gap_y;
    const int ox = (480 - grid_w) / 2;
    const int oy = (480 - grid_h) / 2 + 10;

    for (int i = 0; i < 9; i++) {
        int col = i % cols, row = i / cols;
        int x   = ox + col * (cell_w + gap_x);
        int y   = oy + row * (cell_h + gap_y);

        lv_obj_t *cell = lv_obj_create(s);
        lv_obj_set_size(cell, cell_w, cell_h);
        lv_obj_set_pos(cell, x, y);
        obj_bg(cell, C_PANEL);
        obj_border(cell, 0x444444);
        lv_obj_set_style_radius(cell, 8, LV_PART_MAIN);
        lv_obj_clear_flag(cell, LV_OBJ_FLAG_SCROLLABLE);

        lv_obj_t *name = lv_label_create(cell);
        lv_label_set_text(name, relay_names[i]);
        lv_obj_set_style_text_font(name, &lv_font_montserrat_14, LV_PART_MAIN);
        obj_text_color(name, C_MUTED);
        lv_obj_set_pos(name, 0, 4);
        lv_obj_set_width(name, cell_w);
        lv_obj_set_style_text_align(name, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

        lbl_relay[i] = lv_label_create(cell);
        lv_label_set_text(lbl_relay[i], "off");
        lv_obj_set_style_text_font(lbl_relay[i], &lv_font_montserrat_14, LV_PART_MAIN);
        obj_text_color(lbl_relay[i], C_MUTED);
        lv_obj_set_pos(lbl_relay[i], 0, 26);
        lv_obj_set_width(lbl_relay[i], cell_w);
        lv_obj_set_style_text_align(lbl_relay[i], LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    }

    scr[1] = s;
}

// ── Screen 2: Setpoints ───────────────────────────────────────
void build_setpoints_screen() {
    lv_obj_t *s = lv_obj_create(NULL);
    obj_bg(s, C_BG);
    lv_obj_clear_flag(s, LV_OBJ_FLAG_SCROLLABLE);

    make_label(s, "SETPOINTS", &lv_font_montserrat_14, C_MUTED, 0, 32);
    lv_obj_set_width(lv_obj_get_child(s, -1), 480);

    // Index indicator  "1 / 5"
    lbl_sp_index = make_label(s, "1 / 5", &lv_font_montserrat_14, C_MUTED, 0, 65);
    lv_obj_set_width(lbl_sp_index, 480);
    lv_obj_set_style_text_align(lbl_sp_index, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

    // Parameter name
    lbl_sp_name = make_label(s, setpoints[0].label, &lv_font_montserrat_14, C_TEXT, 0, 90);
    lv_obj_set_width(lbl_sp_name, 480);
    lv_obj_set_style_text_align(lbl_sp_name, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

    // Large value display
    lbl_sp_value = make_label(s, "--", &lv_font_montserrat_48, C_TEXT, 0, 180);
    lv_obj_set_width(lbl_sp_value, 340);
    lv_obj_set_pos(lbl_sp_value, 70, 165);
    lv_obj_set_style_text_align(lbl_sp_value, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    // Unit  "°F" / "%" / "min"
    lbl_sp_unit = make_label(s, setpoints[0].unit, &lv_font_montserrat_28, C_MUTED, 380, 185);

    // Hint text at bottom
    lbl_sp_hint = make_label(s,
        "Rotate: select setpoint\nPress: edit value",
        &lv_font_montserrat_14, C_MUTED, 0, 350);
    lv_obj_set_width(lbl_sp_hint, 480);
    lv_obj_set_style_text_align(lbl_sp_hint, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

    scr[2] = s;
}

// ── Screen 3: Zones ───────────────────────────────────────────
void build_zones_screen() {
    lv_obj_t *s = lv_obj_create(NULL);
    obj_bg(s, C_BG);
    lv_obj_clear_flag(s, LV_OBJ_FLAG_SCROLLABLE);

    make_label(s, "ZONE CONFIG", &lv_font_montserrat_14, C_MUTED, 0, 32);
    lv_obj_set_width(lv_obj_get_child(s, -1), 480);

    // ── Theater zone row ──
    make_label(s, "Theater Zone", &lv_font_montserrat_14, C_MUTED, 80, 140);

    lbl_theater_state = lv_label_create(s);
    lv_obj_set_size(lbl_theater_state, 160, 40);
    lv_obj_set_pos(lbl_theater_state, 260, 130);
    obj_bg(lbl_theater_state, C_PANEL);
    obj_border(lbl_theater_state, 0x444444);
    lv_obj_set_style_radius(lbl_theater_state, 8, LV_PART_MAIN);
    lv_obj_set_style_text_align(lbl_theater_state, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_set_style_text_font(lbl_theater_state, &lv_font_montserrat_14, LV_PART_MAIN);
    obj_text_color(lbl_theater_state, C_MUTED);
    lv_obj_set_style_pad_all(lbl_theater_state, 8, LV_PART_MAIN);
    lv_label_set_text(lbl_theater_state, "DISABLED");

    // ── Mode override row ──
    make_label(s, "System Override", &lv_font_montserrat_14, C_MUTED, 80, 240);

    lbl_override_state = lv_label_create(s);
    lv_obj_set_size(lbl_override_state, 160, 40);
    lv_obj_set_pos(lbl_override_state, 260, 230);
    obj_bg(lbl_override_state, C_PANEL);
    obj_border(lbl_override_state, 0x444444);
    lv_obj_set_style_radius(lbl_override_state, 8, LV_PART_MAIN);
    lv_obj_set_style_text_align(lbl_override_state, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_set_style_text_font(lbl_override_state, &lv_font_montserrat_14, LV_PART_MAIN);
    obj_text_color(lbl_override_state, C_MUTED);
    lv_obj_set_style_pad_all(lbl_override_state, 8, LV_PART_MAIN);
    lv_label_set_text(lbl_override_state, "AUTO");

    // Cursor indicator
    lbl_zone_cursor = make_label(s, ">", &lv_font_montserrat_28, C_ACCENT, 50, 130);

    // Hint
    make_label(s, "Rotate: select   Press: toggle",
               &lv_font_montserrat_14, C_MUTED, 0, 390);
    lv_obj_set_width(lv_obj_get_child(s, -1), 480);
    lv_obj_set_style_text_align(lv_obj_get_child(s, -1), LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

    scr[3] = s;
}

// ─────────────────────────────────────────────────────────────
// UI UPDATE FUNCTIONS  (called from main loop, LVGL-safe)
// ─────────────────────────────────────────────────────────────

// Mode string → accent colour
uint32_t mode_color(const char *mode) {
    if (strncmp(mode, "heat",      4) == 0) return 0xff6b6b;
    if (strncmp(mode, "high_cool", 9) == 0) return 0x64b5f6;
    if (strncmp(mode, "low_cool",  8) == 0) return 0x80cbc4;
    if (strncmp(mode, "dehum",     5) == 0) return 0xce93d8;
    if (strncmp(mode, "fan",       3) == 0) return 0xa5d6a7;
    return C_MUTED;
}

void update_status_ui() {
    // Mode badge
    char upper[32];
    strncpy(upper, hvac.mode, sizeof(upper) - 1);
    for (char *p = upper; *p; p++) if (*p == '_') *p = ' '; else *p = toupper(*p);
    lv_label_set_text(lbl_mode_badge, upper);
    obj_text_color(lbl_mode_badge, mode_color(hvac.mode));

    // Compressor
    char comp_buf[32];
    snprintf(comp_buf, sizeof(comp_buf), "Compressor: %s",
             hvac.compressorOn ? "ON" : "off");
    lv_label_set_text(lbl_comp, comp_buf);
    obj_text_color(lbl_comp, hvac.compressorOn ? C_GREEN : C_MUTED);

    // Sensors
    char buf[20];
    snprintf(buf, sizeof(buf), "%.1f F", hvac.indoorTempF);
    lv_label_set_text(lbl_in_temp, buf);
    snprintf(buf, sizeof(buf), "%.0f%%", hvac.indoorHumPct);
    lv_label_set_text(lbl_in_hum, buf);
    snprintf(buf, sizeof(buf), "%.1f F", hvac.outdoorTempF);
    lv_label_set_text(lbl_out_temp, buf);
    snprintf(buf, sizeof(buf), "%.0f%%", hvac.outdoorHumPct);
    lv_label_set_text(lbl_out_hum, buf);

    // Timestamp
    unsigned long age = (millis() - hvac.updatedAt) / 1000;
    snprintf(buf, sizeof(buf), "Updated %lu s ago", age);
    lv_label_set_text(lbl_status_ts, buf);
}

void update_outputs_ui() {
    bool states[9] = {
        hvac.highCool, hvac.lowCool, hvac.highHeat, hvac.revValve, hvac.fanOn,
        hvac.theaterDamper, hvac.downDamper, hvac.ventOpen, hvac.dehumOn
    };
    for (int i = 0; i < 9; i++) {
        lv_label_set_text(lbl_relay[i], states[i] ? "ON" : "off");
        obj_text_color(lbl_relay[i], states[i] ? C_GREEN : C_MUTED);
        // Also tint the parent cell
        lv_obj_t *cell = lv_obj_get_parent(lbl_relay[i]);
        obj_bg(cell, states[i] ? 0x163d1e : C_PANEL);
        obj_border(cell, states[i] ? C_GREEN : 0x444444);
    }
}

void update_setpoints_ui() {
    char idx_buf[8];
    snprintf(idx_buf, sizeof(idx_buf), "%d / %d", selectedSp + 1, NUM_SETPOINTS);
    lv_label_set_text(lbl_sp_index, idx_buf);

    const Setpoint &sp = setpoints[selectedSp];
    lv_label_set_text(lbl_sp_name, sp.label);
    lv_label_set_text(lbl_sp_unit, sp.unit);

    char val_buf[12];
    int  disp_val = editingSetpoint ? editTempValue : *sp.value;
    snprintf(val_buf, sizeof(val_buf), "%d", disp_val);
    lv_label_set_text(lbl_sp_value, val_buf);
    obj_text_color(lbl_sp_value, editingSetpoint ? C_ACCENT : C_TEXT);

    if (editingSetpoint) {
        lv_label_set_text(lbl_sp_hint, "Rotate: change value\nPress: save & send");
        obj_text_color(lbl_sp_hint, C_YELLOW);
    } else {
        lv_label_set_text(lbl_sp_hint, "Rotate: select setpoint\nPress: edit value");
        obj_text_color(lbl_sp_hint, C_MUTED);
    }
}

void update_zones_ui() {
    // Theater state
    lv_label_set_text(lbl_theater_state, cfg.theaterEnabled ? "ENABLED" : "DISABLED");
    obj_text_color(lbl_theater_state, cfg.theaterEnabled ? C_GREEN : C_MUTED);
    obj_border(lbl_theater_state, cfg.theaterEnabled ? C_GREEN : 0x444444);
    obj_bg(lbl_theater_state, cfg.theaterEnabled ? 0x163d1e : C_PANEL);

    // Override state
    bool isOff = (strcmp(cfg.modeOverride, "off") == 0);
    lv_label_set_text(lbl_override_state, isOff ? "FORCED OFF" : "AUTO");
    obj_text_color(lbl_override_state, isOff ? C_RED : C_GREEN);
    obj_border(lbl_override_state, isOff ? C_RED : C_GREEN);
    obj_bg(lbl_override_state, isOff ? 0x3d1616 : 0x163d1e);

    // Move cursor indicator
    int cursor_y = (selectedZoneCtrl == 0) ? 130 : 230;
    lv_obj_set_pos(lbl_zone_cursor, 50, cursor_y);
}

// ─────────────────────────────────────────────────────────────
// SCREEN SWITCH
// ─────────────────────────────────────────────────────────────

void switch_screen(int idx) {
    currentScreen     = (idx + NUM_SCREENS) % NUM_SCREENS;
    editingSetpoint   = false;
    selectedZoneCtrl  = 0;
    lv_scr_load_anim(scr[currentScreen], LV_SCR_LOAD_ANIM_FADE_IN, 250, 0, false);
    // Immediately refresh the new screen with current data
    switch (currentScreen) {
        case 0: update_status_ui();    break;
        case 1: update_outputs_ui();   break;
        case 2: update_setpoints_ui(); break;
        case 3: update_zones_ui();     break;
    }
}

// ─────────────────────────────────────────────────────────────
// ENCODER EVENT PROCESSING  (called from main loop)
// ─────────────────────────────────────────────────────────────

void handle_encoder(int delta, bool btn) {
    if (delta == 0 && !btn) return;

    switch (currentScreen) {

      // ── Status: no knob interaction ──────────────────────────
      case 0:
        if (delta != 0) switch_screen(currentScreen + (delta > 0 ? 1 : -1));
        break;

      // ── Outputs: no knob interaction ─────────────────────────
      case 1:
        if (delta != 0) switch_screen(currentScreen + (delta > 0 ? 1 : -1));
        break;

      // ── Setpoints ─────────────────────────────────────────────
      case 2:
        if (!editingSetpoint) {
            if (delta != 0) {
                // Rotate without editing — cycle screens if at boundary,
                // otherwise cycle through setpoints
                if (delta > 0 && selectedSp == NUM_SETPOINTS - 1) {
                    switch_screen(currentScreen + 1);
                } else if (delta < 0 && selectedSp == 0) {
                    switch_screen(currentScreen - 1);
                } else {
                    selectedSp = constrain(selectedSp + (delta > 0 ? 1 : -1),
                                           0, NUM_SETPOINTS - 1);
                    update_setpoints_ui();
                }
            }
            if (btn) {
                editTempValue = *setpoints[selectedSp].value;
                editingSetpoint = true;
                update_setpoints_ui();
            }
        } else {
            // In edit mode: rotate adjusts value
            if (delta != 0) {
                editTempValue = constrain(
                    editTempValue + delta,
                    setpoints[selectedSp].minVal,
                    setpoints[selectedSp].maxVal);
                update_setpoints_ui();
            }
            if (btn) {
                // Save: publish to MQTT and update local state
                publish_setconfig(setpoints[selectedSp].mqttKey, editTempValue);
                *setpoints[selectedSp].value = editTempValue;
                editingSetpoint = false;
                update_setpoints_ui();
            }
        }
        break;

      // ── Zones ─────────────────────────────────────────────────
      case 3:
        if (delta != 0) {
            if (delta > 0 && selectedZoneCtrl == 1) {
                switch_screen(currentScreen + 1);
            } else if (delta < 0 && selectedZoneCtrl == 0) {
                switch_screen(currentScreen - 1);
            } else {
                selectedZoneCtrl = constrain(selectedZoneCtrl + (delta > 0 ? 1 : -1), 0, 1);
                update_zones_ui();
            }
        }
        if (btn) {
            if (selectedZoneCtrl == 0) {
                // Toggle theater zone
                cfg.theaterEnabled = !cfg.theaterEnabled;
                publish_setconfig_bool("theater_enabled", cfg.theaterEnabled);
            } else {
                // Toggle mode override
                bool isOff = (strcmp(cfg.modeOverride, "off") == 0);
                const char *newMode = isOff ? "auto" : "off";
                strncpy(cfg.modeOverride, newMode, sizeof(cfg.modeOverride) - 1);
                publish_setconfig_str("mode_override", newMode);
            }
            update_zones_ui();
        }
        break;
    }
}

// ─────────────────────────────────────────────────────────────
// SETUP
// ─────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    Serial.println("[HVAC HMI] Starting...");

    // ── Hardware: display + touch + LVGL (Elecrow BSP) ─────────
    hw_init();
    // After hw_init(), lv_init() has been called and the display
    // driver + tick timer are registered.

    // ── Encoder pins ────────────────────────────────────────────
    pinMode(ENC_A,   INPUT_PULLUP);
    pinMode(ENC_B,   INPUT_PULLUP);
    pinMode(ENC_BTN, INPUT_PULLUP);
    enc_last_a = digitalRead(ENC_A);
    attachInterrupt(digitalPinToInterrupt(ENC_A),   enc_isr_a, CHANGE);
    attachInterrupt(digitalPinToInterrupt(ENC_BTN), btn_isr,   FALLING);

    // ── Build LVGL screens ──────────────────────────────────────
    build_status_screen();
    build_outputs_screen();
    build_setpoints_screen();
    build_zones_screen();

    // Load the status screen first
    lv_scr_load(scr[0]);

    // ── WiFi ────────────────────────────────────────────────────
    WiFi.mode(WIFI_STA);
    wifi_connect();

    // ── MQTT ────────────────────────────────────────────────────
    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setCallback(on_mqtt_message);
    mqtt.setBufferSize(4096);   // status payload can be ~1 KB
    mqtt_reconnect();

    Serial.println("[HVAC HMI] Ready");
}

// ─────────────────────────────────────────────────────────────
// LOOP  (~10 ms cycle)
// ─────────────────────────────────────────────────────────────

static unsigned long last_conn_check = 0;

void loop() {
    // ── LVGL task handler ────────────────────────────────────────
    lv_timer_handler();

    // ── MQTT polling ─────────────────────────────────────────────
    if (mqtt.connected()) {
        mqtt.loop();
    }

    // ── Apply UI updates from MQTT callbacks ────────────────────
    if (status_dirty) {
        status_dirty = false;
        if (currentScreen == 0) update_status_ui();
        if (currentScreen == 1) update_outputs_ui();
    }
    if (config_dirty) {
        config_dirty = false;
        if (currentScreen == 2) update_setpoints_ui();
        if (currentScreen == 3) update_zones_ui();
    }

    // ── Encoder events ───────────────────────────────────────────
    int delta = read_encoder();
    bool btn  = read_button();
    handle_encoder(delta, btn);

    // ── Periodic status refresh on status screen ─────────────────
    // Even without new MQTT data, refresh the "updated X s ago" timestamp
    static unsigned long last_ts_refresh = 0;
    if (currentScreen == 0 && (millis() - last_ts_refresh) > 5000) {
        last_ts_refresh = millis();
        if (hvac.updatedAt > 0) update_status_ui();
    }

    // ── Connection watchdog (every 5 s) ──────────────────────────
    if ((millis() - last_conn_check) > 5000) {
        last_conn_check = millis();
        if (WiFi.status() != WL_CONNECTED) wifi_connect();
        if (!mqtt.connected())             mqtt_reconnect();
    }

    delay(5);   // ~200 Hz max — plenty for LVGL + encoder responsiveness
}
