// Copy this file to secrets.h (same folder) and fill in real values.
// secrets.h is git-ignored. Never commit real passwords.
#pragma once

#define WIFI_SSID "your-hotspot-name"
#define WIFI_PASSWORD "your-hotspot-password"

// IP address or hostname of the Raspberry Pi running Mosquitto.
#define MQTT_HOST "192.168.1.50"
#define MQTT_PORT 1883
#define MQTT_USER "node"
#define MQTT_PASSWORD "replace-with-mqtt-password"

// Must be unique for every physical node (node-1, node-2, ...).
#define NODE_ID "node-1"
