# Sensor node (ESP32)

`node/node.ino` reads heart rate from a MAX30102 and skin temperature from a
GY-906 MLX90614 (optional: if it is missing, `body_temp` is `null` and the node
keeps retrying every 10 s). It publishes one JSON reading every 2 s over Wi-Fi
to the gateway's MQTT broker.

## Wiring

Both sensors share the I2C bus:

| ESP32  | MAX30102 | GY-906 |
| ------ | -------- | ------ |
| 3V3    | VIN      | VCC    |
| GND    | GND      | GND    |
| GPIO22 | SCL      | SCL    |
| GPIO21 | SDA      | SDA    |

I2C addresses: MAX30102 `0x57`, MLX90614 `0x5A`. The bus runs at 100 kHz
because the MLX90614 does not support faster speeds.

## Build

1. In the Arduino Library Manager install **SparkFun MAX3010x Pulse and
   Proximity Sensor Library**, **Adafruit MLX90614 Library** and
   **PubSubClient**.
2. Copy `node/secrets.example.h` to `node/secrets.h` and fill in the Wi-Fi
   hotspot, the gateway's IP address, the MQTT user and a unique `NODE_ID`.
   `secrets.h` is git-ignored; never commit it.
3. Open `node/node.ino`, select **ESP32 Dev Module**, upload, and open the
   Serial Monitor at 115200 baud. Each publish prints `[sent]` or
   `[not sent]` followed by the JSON.

## Message format

Topic: `vsm/sensor/<node_id>`

```json
{"node_id":"node-1","seq":12,"hr":72,"spo2":97,"body_temp":36.4,"finger":true,"uptime_ms":123456}
```

| Field       | Meaning                                                            |
| ----------- | ------------------------------------------------------------------ |
| `node_id`   | Unique per physical node; the gateway keeps it in every reading     |
| `seq`       | Counter since boot; gaps show lost messages (packet delivery rate) |
| `hr`        | Average of the last 4 beats in bpm, `null` without a stable signal |
| `spo2`      | Blood oxygen % (Maxim red/IR algorithm, 4 s window), `null` if unstable |
| `body_temp` | MLX90614 object temperature in °C (skin, not core temperature)     |
| `finger`    | Whether a finger is on the MAX30102                                |
| `uptime_ms` | Milliseconds since boot                                            |

`hr` needs four detected beats with a finger held still, so it stays `null`
for the first few seconds after placing a finger.
