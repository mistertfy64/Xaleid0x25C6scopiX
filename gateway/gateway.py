"""Vital Sign Monitor gateway."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import random
import sqlite3
import time
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

SPOOL_DIR = Path(os.environ.get(
    "VSM_SPOOL_DIR", str(Path(__file__).resolve().parent / "spool")))
QUEUE_DB = SPOOL_DIR / "gateway_queue.sqlite3"
RETRY_BASE_SECONDS = 1
RETRY_MAX_SECONDS = 300
LM35_SAMPLES = 5
LM35_MILLIVOLTS_PER_C = 10
_lm35_channel = None
_lm35_unavailable = False


def queue_connection():
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(QUEUE_DB, timeout=30)
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def init_queue():
    with closing(queue_connection()) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS queued_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                measurements TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0
            )
        """)


def enqueue(readings):
    with closing(queue_connection()) as connection, connection:
        connection.execute(
            "INSERT INTO queued_sessions(measurements) VALUES(?)",
            (json.dumps(readings),),
        )


def oldest_queued():
    with closing(queue_connection()) as connection:
        return connection.execute(
            "SELECT id, measurements, attempts, next_attempt_at "
            "FROM queued_sessions ORDER BY id LIMIT 1"
        ).fetchone()


def remove_queued(session_id):
    with closing(queue_connection()) as connection, connection:
        connection.execute("DELETE FROM queued_sessions WHERE id=?", (session_id,))


def defer_queued(session_id, attempts):
    attempts += 1
    delay = min(RETRY_BASE_SECONDS * 2 ** min(attempts - 1, 20), RETRY_MAX_SECONDS)
    with closing(queue_connection()) as connection, connection:
        connection.execute(
            "UPDATE queued_sessions SET attempts=?, next_attempt_at=? WHERE id=?",
            (attempts, time.time() + delay, session_id),
        )
    return delay


def fake_reading():
    return {"hr": random.randint(60, 90), "spo2": random.randint(95, 100),
            "body_temp": round(random.uniform(36.2, 37.2), 1)}

def read_mqtt(url, port):
    if not url:
        raise ValueError("missing mqtt broker url")
    if not port:
        raise ValueError("missing mqtt broker port")



def read_serial(ser):
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue


def read_machine_temp():
    global _lm35_channel, _lm35_unavailable
    if _lm35_unavailable:
        return None
    try:
        if _lm35_channel is None:
            import board
            import adafruit_ads1x15.ads1115 as ADS
            from adafruit_ads1x15.analog_in import AnalogIn

            if not hasattr(board, "I2C"):
                raise RuntimeError("board does not provide I2C")
            adc = ADS.ADS1115(board.I2C())
            _lm35_channel = AnalogIn(adc, ADS.P0)
        voltage = sum(_lm35_channel.voltage for _ in range(LM35_SAMPLES)) / LM35_SAMPLES
    except (ImportError, OSError, RuntimeError, NotImplementedError) as error:
        _lm35_unavailable = True
        print(f"LM35 unavailable; using None: {error}")
        return None
    return round(voltage * 1000 / LM35_MILLIVOLTS_PER_C, 1)


def collect_session(read, n):
    readings = []
    for _ in range(n):
        reading = read()
        reading["machine_temp"] = read_machine_temp()
        reading["t"] = time.time()
        readings.append(reading)
        print("reading:", reading)
        time.sleep(1)
    return readings


def gateway_config():
    url = os.environ.get("VSM_URL")
    username = os.environ.get("VSM_USER")
    password = os.environ.get("VSM_PASS")
    missing = [
        name for name, value in (
            ("VSM_URL", url), ("VSM_USER", username), ("VSM_PASS", password)
        ) if not value
    ]
    if missing:
        raise RuntimeError(f"missing gateway settings: {', '.join(missing)}")
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        raise ValueError("VSM_URL must be an HTTPS URL")
    if parsed_url.username or parsed_url.password:
        raise ValueError("do not include credentials in VSM_URL")
    return url.rstrip("/"), username, password


def upload(readings):
    url, username, password = gateway_config()
    response = requests.post(
        f"{url}/login",
        json={"username": username, "password": password, "client": "gateway"},
        timeout=10,
        verify=True,
    )
    response.raise_for_status()
    login = response.json()
    if not login.get("success"):
        raise RuntimeError("login failed; queued session was retained")
    response = requests.post(
        f"{url}/measurements",
        timeout=10,
        verify=True,
        json={
            "userID": login["userID"],
            "token": login["token"],
            "measurements": readings,
        },
    )
    response.raise_for_status()


def flush_queue(wait_for_retry=True):
    while queued := oldest_queued():
        session_id, payload, attempts, next_attempt_at = queued
        delay = next_attempt_at - time.time()
        if delay > 0:
            if not wait_for_retry:
                return
            time.sleep(delay)
        try:
            upload(json.loads(payload))
        except requests.exceptions.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                raise RuntimeError(
                    f"permanent upload failure (HTTP {status}); "
                    f"session {session_id} remains queued"
                ) from error
            delay = defer_queued(session_id, attempts)
            print(f"upload failed; retrying session {session_id} "
                  f"in {delay}s: {error}")
            if not wait_for_retry:
                return
            time.sleep(delay)
        else:
            remove_queued(session_id)
            print(f"uploaded queued session {session_id}")


def run_sessions(read, n, loop=False):
    while True:
        enqueue(collect_session(read, n))
        flush_queue(wait_for_retry=not loop)
        if not loop:
            return

def on_mqtt_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Connected to MQTT Broker!")
        client.subscribe("vsm/sensor")
    else:
        print(f"Failed to connect, return code {reason_code}")

def on_mqtt_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8")
    print(f"Topic: {msg.topic} | Message: {payload}")

    readings = []
    readings.append(json.loads(payload))

    # enqueue(collect_session(read, n))
    enqueue(readings)
    flush_queue()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--loop", action="store_true",
                    help="collect sessions continuously")
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--n", type=int, default=10, help="readings per session")
    ap.add_argument("--mqtt-broker-url", type=str, default="",
                    help="url of mqtt broker, supersedes environemnt variable if specified")
    ap.add_argument("--mqtt-broker-port", type=int, default="1883",
                    help="port of mqtt broker, supersedes environment variable if specified")
    args = ap.parse_args()

    init_queue()
    flush_queue(wait_for_retry=False)
    if args.fake:
        read = fake_reading
    else:
        import serial
        import paho.mqtt.client as mqtt

        ser = serial.Serial(args.port, args.baud, timeout=2)
        # read = lambda: read_serial(ser)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        client.on_connect = on_mqtt_connect
        client.on_message = on_mqtt_message

        mqtt_broker_url = args.mqtt_broker_url or os.environ.get("MQTT_BROKER_URL", "")
        mqtt_broker_port = args.mqtt_broker_port or os.environ.get("MQTT_BROKER_PORT", "")

        client.connect(mqtt_broker_url, mqtt_broker_port, keepalive=60)
        client.loop_forever()


    # run_sessions(read, args.n, loop="vsm/sensor"args.loop)

