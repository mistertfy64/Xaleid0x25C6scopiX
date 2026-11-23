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
import uuid

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
                next_attempt_at REAL NOT NULL DEFAULT 0,
                session_id TEXT
            )
        """)
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(queued_sessions)").fetchall()
        }
        if "session_id" not in columns:
            connection.execute("ALTER TABLE queued_sessions ADD COLUMN session_id TEXT")


def enqueue(readings):
    session_id = uuid.uuid4().hex
    with closing(queue_connection()) as connection, connection:
        connection.execute(
            "INSERT INTO queued_sessions(measurements, session_id) VALUES(?, ?)",
            (json.dumps(readings), session_id),
        )


def oldest_queued():
    with closing(queue_connection()) as connection:
        return connection.execute(
            "SELECT id, measurements, attempts, next_attempt_at, session_id "
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


MQTT_DEFAULT_PORT = 1883
MQTT_DEFAULT_TOPIC = "vsm/sensor/+"


def fake_reading():
    return {"hr": random.randint(60, 90), "spo2": random.randint(95, 100),
            "body_temp": round(random.uniform(36.2, 37.2), 1)}


def mqtt_config(broker_url=None, broker_port=None, username=None, password=None, topic=None):
    url = (broker_url or os.environ.get("MQTT_BROKER_URL", "")).strip()
    if not url:
        raise ValueError("missing mqtt broker url")
    raw_port = broker_port if broker_port is not None else os.environ.get("MQTT_BROKER_PORT", "")
    if raw_port == "" or raw_port is None:
        port = MQTT_DEFAULT_PORT
    else:
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid mqtt broker port: {raw_port}") from error
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid mqtt broker port: {port}")
    user = username if username is not None else os.environ.get("MQTT_USER", "")
    passwd = password if password is not None else os.environ.get("MQTT_PASSWORD", "")
    sub_topic = (topic or os.environ.get("MQTT_TOPIC", MQTT_DEFAULT_TOPIC)).strip() or MQTT_DEFAULT_TOPIC
    return url, port, user, passwd, sub_topic



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


def upload(readings, session_id=None):
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
    body = {
        "userID": login["userID"],
        "token": login["token"],
        "measurements": readings,
    }
    if session_id:
        body["session_id"] = session_id
    response = requests.post(
        f"{url}/measurements",
        timeout=10,
        verify=True,
        json=body,
    )
    response.raise_for_status()


def flush_queue(wait_for_retry=True):
    while queued := oldest_queued():
        row_id, payload, attempts, next_attempt_at, session_id = queued
        delay = next_attempt_at - time.time()
        if delay > 0:
            if not wait_for_retry:
                return
            time.sleep(delay)
        try:
            upload(json.loads(payload), session_id=session_id)
        except requests.exceptions.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                raise RuntimeError(
                    f"permanent upload failure (HTTP {status}); "
                    f"session {row_id} remains queued"
                ) from error
            delay = defer_queued(row_id, attempts)
            print(f"upload failed; retrying session {row_id} "
                  f"in {delay}s: {error}")
            if not wait_for_retry:
                return
            time.sleep(delay)
        else:
            remove_queued(row_id)
            print(f"uploaded queued session {row_id}")


def run_sessions(read, n, loop=False):
    while True:
        enqueue(collect_session(read, n))
        flush_queue(wait_for_retry=not loop)
        if not loop:
            return

class MqttSessionCollector:
    def __init__(self, n, loop=False, topic=MQTT_DEFAULT_TOPIC):
        if n < 1:
            raise ValueError("n must be at least 1")
        self.n = n
        self.loop = loop
        self.topic = topic
        self.buffer = []

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            print(f"Connected to MQTT broker; subscribing to {self.topic}")
            client.subscribe(self.topic)
        else:
            print(f"Failed to connect to MQTT broker, return code {reason_code}")

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        try:
            reading = json.loads(payload)
        except json.JSONDecodeError:
            print(f"Ignoring invalid JSON on {msg.topic}: {payload}")
            return
        if not isinstance(reading, dict):
            print(f"Ignoring non-object JSON on {msg.topic}: {payload}")
            return

        reading["machine_temp"] = read_machine_temp()
        reading["t"] = time.time()
        self.buffer.append(reading)
        print("reading:", reading)

        if len(self.buffer) >= self.n:
            batch = list(self.buffer)
            self.buffer.clear()
            enqueue(batch)
            flush_queue(wait_for_retry=not self.loop)
            if not self.loop:
                client.disconnect()


def run_mqtt(broker_url, broker_port, n, loop=False, username="", password="", topic=MQTT_DEFAULT_TOPIC):
    import paho.mqtt.client as mqtt

    collector = MqttSessionCollector(n=n, loop=loop, topic=topic)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = collector.on_connect
    client.on_message = collector.on_message
    if username:
        client.username_pw_set(username, password)
    client.connect(broker_url, broker_port, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--loop", action="store_true",
                    help="collect sessions continuously")
    ap.add_argument("--port", help="serial device path (uses MQTT if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--n", type=int, default=10, help="readings per session")
    ap.add_argument("--mqtt-broker-url", type=str, default=None,
                    help="url of mqtt broker, supersedes environment variable if specified")
    ap.add_argument("--mqtt-broker-port", type=int, default=None,
                    help="port of mqtt broker, supersedes environment variable if specified")
    ap.add_argument("--mqtt-user", type=str, default=None,
                    help="mqtt username, supersedes environment variable if specified")
    ap.add_argument("--mqtt-password", type=str, default=None,
                    help="mqtt password, supersedes environment variable if specified")
    ap.add_argument("--mqtt-topic", type=str, default=None,
                    help="mqtt topic filter to subscribe to (default: vsm/sensor/+)")
    args = ap.parse_args()

    init_queue()
    flush_queue(wait_for_retry=False)
    if args.fake:
        run_sessions(fake_reading, args.n, loop=args.loop)
    elif args.port:
        import serial

        with serial.Serial(args.port, args.baud, timeout=2) as ser:
            run_sessions(lambda: read_serial(ser), args.n, loop=args.loop)
    else:
        broker_url, broker_port, mqtt_user, mqtt_password, mqtt_topic = mqtt_config(
            broker_url=args.mqtt_broker_url,
            broker_port=args.mqtt_broker_port,
            username=args.mqtt_user,
            password=args.mqtt_password,
            topic=args.mqtt_topic,
        )
        run_mqtt(
            broker_url,
            broker_port,
            args.n,
            loop=args.loop,
            username=mqtt_user,
            password=mqtt_password,
            topic=mqtt_topic,
        )

