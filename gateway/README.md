The gateway stores each completed session in `spool/gateway_queue.sqlite3`
before uploading it. Failed network requests, timeouts, HTTP 5xx responses, and
HTTP 429 responses are retried with exponential backoff (up to 5 minutes).
The queue is retained across restarts; set `VSM_SPOOL_DIR` to choose another
location with persistent storage.

Each upload attempt logs in again because gateway tokens are one-time use.
Each queued session carries a generated `session_id` idempotency key so retries
after a lost response do not create duplicate records on the cloud backend.

Install gateway dependencies with `python -m pip install -r requirements.txt`.
From the repository root, install test dependencies with
`python -m pip install -r gateway/requirements-dev.txt` and run
`python -m pytest gateway/tests`.
Copy `.env.example` to `.env` and set `VSM_URL`, `VSM_USER`, `VSM_PASS`, and
`MQTT_BROKER_URL` (plus `MQTT_BROKER_PORT`, `MQTT_USER`, `MQTT_PASSWORD`, and
`MQTT_TOPIC` if needed; defaults are port `1883` and topic `vsm/sensor/+`).
The URL must use HTTPS; certificate verification is always enabled. For a LAN
CA, set `REQUESTS_CA_BUNDLE` to its trusted certificate file.

By default (when neither `--fake` nor `--port` is supplied), the gateway
subscribes to the MQTT broker and batches `--n` readings per uploaded session
(matching the ESP32 sensor node topic `vsm/sensor/<node_id>`). Pass `--port` to
read from serial instead, or `--fake` to generate test readings.

The LM35 connects to ADS1115 A0; power the ADS1115 at 3.3 V and share ground
with the Pi. Keep the LM35 output within the ADC input range. The gateway
averages five voltage samples and converts at 10 mV/°C. If the ADS1115 or its
CircuitPython libraries are unavailable, `machine_temp` is `None`.

## systemd on Raspberry Pi OS

From the repository root, install the files under `/opt/vsm`, create a service
user, and give it access to the serial and I2C devices. Enable I2C in
`raspi-config` first:

```sh
sudo useradd --system --create-home --home-dir /opt/vsm --shell /usr/sbin/nologin vsm
sudo usermod -aG dialout,i2c vsm
sudo install -d -o root -g root /opt/vsm/gateway
sudo cp gateway/gateway.py gateway/requirements.txt /opt/vsm/gateway/
sudo python3 -m venv /opt/vsm/venv
sudo /opt/vsm/venv/bin/pip install -r /opt/vsm/gateway/requirements.txt
sudo install -d -m 0750 -o root -g vsm /etc/vsm
sudo install -m 0640 -o root -g vsm gateway/.env.example /etc/vsm/gateway.env
```

Edit `/etc/vsm/gateway.env`: set the HTTPS URL, Cloud credentials, MQTT broker
settings (`MQTT_BROKER_URL`, `MQTT_BROKER_PORT`, and optional `MQTT_USER`/
`MQTT_PASSWORD`), and `VSM_SPOOL_DIR=/var/lib/vsm`. Then install and enable the
unit:

```sh
sudo install -m 0644 gateway/vsm-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vsm-gateway.service
sudo journalctl -u vsm-gateway.service -f
```

The current gateway collects one session and exits after upload; systemd starts
it with `--loop` to collect repeated sessions and restarts it on failure.
Manual runs remain one session unless `--loop` is supplied, for example
`python gateway.py --fake --loop --n 10`. While offline, continuous mode keeps
collecting sessions into SQLite and attempts to flush the backlog between
sessions.
