/*
  Vital Sign Monitor - sensor node (ESP32)

  Reads heart rate and SpO2 from a MAX30102 and, if present, skin temperature
  from a GY-906 MLX90614, then publishes one JSON reading every
  PUBLISH_INTERVAL_MS to the gateway's MQTT broker on topic
  "vsm/sensor/<NODE_ID>".

  Heart rate: beat detection on the IR signal (SparkFun checkForBeat).
  SpO2: Maxim's red/IR ratio algorithm (SparkFun spo2_algorithm) over a
  4 s window, recalculated every second. Demo-grade only, not a medical device.

  Wiring (both sensors share the I2C bus):
    3V3 -> VIN/VCC   GND -> GND   GPIO22 -> SCL   GPIO21 -> SDA

  Libraries (Arduino Library Manager):
    - SparkFun MAX3010x Pulse and Proximity Sensor Library
    - Adafruit MLX90614 Library
    - PubSubClient (by Nick O'Leary)

  Copy secrets.example.h to secrets.h and fill in Wi-Fi/MQTT settings.
  secrets.h is git-ignored; never commit it.

  Message format (one JSON object per publish; null = no valid value):
    {"node_id":"node-1","seq":12,"hr":72,"spo2":97,"body_temp":36.4,
     "finger":true,"uptime_ms":123456}
*/
#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <math.h>
#include "MAX30105.h"
#include "heartRate.h"
#include "spo2_algorithm.h"
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

// The sensor delivers 100 samples/s (400 Hz with 4-sample averaging).
// The SpO2 algorithm expects FreqS (25) samples/s, so average every 4 samples.
const byte SPO2_DECIMATION = 4;
const int SPO2_STEP = FreqS;                 // recalculate once per second
const unsigned long SPO2_HOLD_MS = 5000;     // no valid result for this long -> spo2 = null
const int32_t SPO2_MIN = 70, SPO2_MAX = 100; // discard physiologically implausible results

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

// SpO2 state: sliding window of BUFFER_SIZE (100) decimated samples = 4 s
uint32_t irBuffer[BUFFER_SIZE];
uint32_t redBuffer[BUFFER_SIZE];
int spo2Filled = 0;          // samples currently in the window
int spo2SinceCalc = 0;       // new samples since the last calculation
uint32_t irAccum = 0, redAccum = 0;
byte accumCount = 0;
int32_t spo2Value = -1;
unsigned long lastSpo2 = 0;  // millis() of the last valid SpO2 result

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

void resetSpo2() {
  spo2Filled = 0;
  spo2SinceCalc = 0;
  irAccum = redAccum = 0;
  accumCount = 0;
  spo2Value = -1;
  lastSpo2 = 0;
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

// Adds one 100 Hz red/IR sample to the SpO2 window (decimated to 25 Hz) and
// runs the Maxim algorithm once per second when the 4 s window is full.
void addSpo2Sample(uint32_t ir, uint32_t red) {
  irAccum += ir;
  redAccum += red;
  if (++accumCount < SPO2_DECIMATION) return;
  uint32_t irAvg = irAccum / SPO2_DECIMATION;
  uint32_t redAvg = redAccum / SPO2_DECIMATION;
  irAccum = redAccum = 0;
  accumCount = 0;

  if (spo2Filled == BUFFER_SIZE) {  // slide the window by one sample
    memmove(irBuffer, irBuffer + 1, (BUFFER_SIZE - 1) * sizeof(uint32_t));
    memmove(redBuffer, redBuffer + 1, (BUFFER_SIZE - 1) * sizeof(uint32_t));
    spo2Filled--;
  }
  irBuffer[spo2Filled] = irAvg;
  redBuffer[spo2Filled] = redAvg;
  spo2Filled++;
  spo2SinceCalc++;

  if (spo2Filled < BUFFER_SIZE || spo2SinceCalc < SPO2_STEP) return;
  spo2SinceCalc = 0;

  int32_t spo2, algoHr;
  int8_t spo2Valid, algoHrValid;
  maxim_heart_rate_and_oxygen_saturation(irBuffer, BUFFER_SIZE, redBuffer,
                                         &spo2, &spo2Valid, &algoHr, &algoHrValid);
  if (spo2Valid && spo2 >= SPO2_MIN && spo2 <= SPO2_MAX) {
    spo2Value = spo2;
    lastSpo2 = millis();
  }
}

// Reads every new sample from the MAX30102 FIFO: IR drives beat detection,
// red + IR feed the SpO2 window.
void samplePulseOximeter() {
  pulseSensor.check();
  while (pulseSensor.available()) {
    uint32_t ir = pulseSensor.getFIFOIR();
    uint32_t red = pulseSensor.getFIFORed();
    pulseSensor.nextSample();
    processSample(ir, red);
  }
}

void processSample(uint32_t ir, uint32_t red) {
  bool nowFinger = ir > FINGER_IR_THRESHOLD;
  if (nowFinger != finger) {
    finger = nowFinger;
    resetHeartRate();
    resetSpo2();
  }
  if (!finger) return;

  addSpo2Sample(ir, red);

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
  bool spo2Ok = finger && lastSpo2 != 0 && now - lastSpo2 < SPO2_HOLD_MS;
  char hr[12], spo2[12], temp[12];
  jsonNumber(hr, sizeof hr, hrValid ? (float)bpmAvg : NAN, 0);
  jsonNumber(spo2, sizeof spo2, spo2Ok ? (float)spo2Value : NAN, 0);
  jsonNumber(temp, sizeof temp, mlxOk ? skinTempC : NAN, 2);

  char payload[200];
  snprintf(payload, sizeof payload,
           "{\"node_id\":\"%s\",\"seq\":%lu,\"hr\":%s,\"spo2\":%s,"
           "\"body_temp\":%s,\"finger\":%s,\"uptime_ms\":%lu}",
           NODE_ID, (unsigned long)seq, hr, spo2, temp, finger ? "true" : "false", now);
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
  // brightness 0x1F, 4-sample average, red+IR, 400 Hz, 411 us, 4096 nA range
  // -> 100 samples/s out of the FIFO. Red LED stays at the same amplitude as
  // IR because SpO2 is computed from the red/IR ratio.
  pulseSensor.setup(0x1F, 4, 2, 400, 411, 4096);
  pulseSensor.setPulseAmplitudeGreen(0);

  tryStartMlx();

  setupWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setSocketTimeout(3);  // keep reconnect attempts short so sampling resumes quickly
}

void loop() {
  samplePulseOximeter();  // keep this fast: beat detection needs every sample
  sampleTemperature();
  maintainMqtt();
  mqtt.loop();
  publishReading();
}
