/*
  Vital Sign Monitor - sensor node (ESP32)

  Reads heart rate from a MAX30102 and, if present, skin temperature from a
  GY-906 MLX90614, then publishes one JSON reading every PUBLISH_INTERVAL_MS
  to the gateway's MQTT broker on topic "vsm/sensor/<NODE_ID>".

  Wiring (both sensors share the I2C bus):
    3V3 -> VIN/VCC   GND -> GND   GPIO22 -> SCL   GPIO21 -> SDA

  Libraries (Arduino Library Manager):
    - SparkFun MAX3010x Pulse and Proximity Sensor Library
    - Adafruit MLX90614 Library
    - PubSubClient (by Nick O'Leary)

  Copy secrets.example.h to secrets.h and fill in Wi-Fi/MQTT settings.
  secrets.h is git-ignored; never commit it.

  Message format (one JSON object per publish; null = no valid value):
    {"node_id":"node-1","seq":12,"hr":72,"spo2":null,"body_temp":36.4,
     "finger":true,"uptime_ms":123456}
*/
#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <math.h>
#include "MAX30105.h"
#include "heartRate.h"
#include <Adafruit_MLX90614.h>

#include "secrets.h"

#ifndef NODE_ID
#define NODE_ID "node-1"
#endif

#define SDA_PIN 21
#define SCL_PIN 22

const unsigned long PUBLISH_INTERVAL_MS = 2000;
const unsigned long TEMP_INTERVAL_MS = 500;
const unsigned long MLX_RETRY_MS = 10000;
const unsigned long MQTT_RETRY_MS = 5000;
const unsigned long BEAT_TIMEOUT_MS = 3000;  // no beat for this long -> hr = null
const long FINGER_IR_THRESHOLD = 50000;

const char *TOPIC = "vsm/sensor/" NODE_ID;

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
MAX30105 pulseSensor;
Adafruit_MLX90614 mlx;

// heart rate state
const byte RATE_SIZE = 4;
byte rates[RATE_SIZE];
byte rateCount = 0;
byte rateSpot = 0;
unsigned long lastBeat = 0;
int bpmAvg = 0;
bool finger = false;

// temperature state
bool mlxOk = false;
unsigned long lastMlxTry = 0;
unsigned long lastTemp = 0;
float skinTempC = NAN;

// network state
unsigned long lastMqttTry = 0;
unsigned long lastPublish = 0;
uint32_t seq = 0;

void resetHeartRate() {
  rateCount = 0;
  rateSpot = 0;
  bpmAvg = 0;
  lastBeat = 0;
}

void tryStartMlx() {
  lastMlxTry = millis();
  mlxOk = mlx.begin(0x5A, &Wire);
  Serial.println(mlxOk ? "MLX90614 OK" : "MLX90614 not found; body_temp will be null");
}

void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Connecting to Wi-Fi \"%s\" (non-blocking)\n", WIFI_SSID);
}

void maintainMqtt() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) return;
  unsigned long now = millis();
  if (now - lastMqttTry < MQTT_RETRY_MS) return;
  lastMqttTry = now;

  Serial.printf("Wi-Fi OK (%s). Connecting to MQTT %s:%d ...\n",
                WiFi.localIP().toString().c_str(), MQTT_HOST, MQTT_PORT);
  String clientId = String("vsm-") + NODE_ID;
  bool ok = mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASSWORD);
  if (ok) {
    Serial.println("MQTT connected");
  } else {
    Serial.printf("MQTT connect failed, state=%d (retry in %lus)\n",
                  mqtt.state(), MQTT_RETRY_MS / 1000);
  }
}

void sampleHeartRate() {
  long ir = pulseSensor.getIR();
  bool nowFinger = ir > FINGER_IR_THRESHOLD;
  if (nowFinger != finger) {
    finger = nowFinger;
    resetHeartRate();
  }
  if (!finger) return;

  if (checkForBeat(ir)) {
    unsigned long now = millis();
    if (lastBeat != 0) {
      float bpm = 60.0f / ((now - lastBeat) / 1000.0f);
      if (bpm > 20 && bpm < 255) {
        rates[rateSpot] = (byte)bpm;
        rateSpot = (rateSpot + 1) % RATE_SIZE;
        if (rateCount < RATE_SIZE) rateCount++;
        int sum = 0;
        for (byte i = 0; i < rateCount; i++) sum += rates[i];
        bpmAvg = sum / rateCount;
      }
    }
    lastBeat = now;
  }
}

void sampleTemperature() {
  unsigned long now = millis();
  if (!mlxOk) {
    if (now - lastMlxTry >= MLX_RETRY_MS) tryStartMlx();
    return;
  }
  if (now - lastTemp < TEMP_INTERVAL_MS) return;
  lastTemp = now;
  skinTempC = mlx.readObjectTempC();
}

// Writes a number or the literal null into buf.
void jsonNumber(char *buf, size_t len, float value, int digits) {
  if (isnan(value)) snprintf(buf, len, "null");
  else snprintf(buf, len, "%.*f", digits, value);
}

void publishReading() {
  unsigned long now = millis();
  if (now - lastPublish < PUBLISH_INTERVAL_MS) return;
  lastPublish = now;

  bool hrValid = finger && rateCount == RATE_SIZE && lastBeat != 0 &&
                 now - lastBeat < BEAT_TIMEOUT_MS;
  char hr[12], temp[12];
  jsonNumber(hr, sizeof hr, hrValid ? (float)bpmAvg : NAN, 0);
  jsonNumber(temp, sizeof temp, mlxOk ? skinTempC : NAN, 2);

  char payload[200];
  snprintf(payload, sizeof payload,
           "{\"node_id\":\"%s\",\"seq\":%lu,\"hr\":%s,\"spo2\":null,"
           "\"body_temp\":%s,\"finger\":%s,\"uptime_ms\":%lu}",
           NODE_ID, (unsigned long)seq, hr, temp, finger ? "true" : "false", now);
  seq++;

  bool sent = mqtt.connected() && mqtt.publish(TOPIC, payload);
  Serial.printf("%s %s\n", sent ? "[sent]" : "[not sent]", payload);
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("\n--- VSM sensor node %s ---\n", NODE_ID);

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);  // MLX90614 (SMBus) max 100 kHz

  if (!pulseSensor.begin(Wire, I2C_SPEED_STANDARD)) {
    Serial.println("MAX30102 not found. Check wiring/power.");
    while (true) delay(1000);
  }
  pulseSensor.setup(0x1F, 4, 2, 400, 411, 4096);
  pulseSensor.setPulseAmplitudeRed(0x0A);
  pulseSensor.setPulseAmplitudeGreen(0);

  tryStartMlx();

  setupWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setSocketTimeout(3);  // keep reconnect attempts short so sampling resumes quickly
}

void loop() {
  sampleHeartRate();  // keep this fast: beat detection needs frequent samples
  sampleTemperature();
  maintainMqtt();
  mqtt.loop();
  publishReading();
}
