// ─────────────────────────────────────────────────────────────
// config.h  —  CrowPanel HVAC HMI — user configuration
// Fill in your WiFi credentials and MQTT broker address before
// flashing. Do NOT commit this file to a public repository if
// you add real passwords.
// ─────────────────────────────────────────────────────────────

#pragma once

// WiFi
#define WIFI_SSID   "your-network-ssid"
#define WIFI_PASS   "your-wifi-password"

// MQTT broker (usually the IP of your Home Assistant / Mosquitto server)
#define MQTT_HOST   "192.168.1.x"
#define MQTT_PORT   1883
#define MQTT_USER   ""    // leave empty if broker has no auth
#define MQTT_PASS   ""

// MQTT topics — must match bridge_daemon.py
#define TOPIC_STATUS   "home/hvac/status"
#define TOPIC_CONFIG   "home/hvac/config"
#define TOPIC_CMD      "home/hvac/cmd"

// CrowPanel 2.1" encoder pin assignments
// GPIO42 = CLK (A), GPIO4 = DT (B)
// Verify ENC_BTN for your specific board revision — common values: 0, 17, 21
#define ENC_A    42
#define ENC_B     4
#define ENC_BTN  17

// Display backlight pin (PWM)
#define LCD_BL    6
