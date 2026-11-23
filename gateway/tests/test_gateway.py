import json
from types import SimpleNamespace

import pytest

from gateway import gateway as gateway_module


def test_default_mode_runs_one_session_and_waits_for_upload(monkeypatch):
    events = []
    monkeypatch.setattr(
        gateway_module, "collect_session", lambda *_: events.append("collect") or []
    )
    monkeypatch.setattr(gateway_module, "enqueue", lambda *_: events.append("enqueue"))
    monkeypatch.setattr(
        gateway_module,
        "flush_queue",
        lambda wait_for_retry: events.append(("flush", wait_for_retry)),
    )

    gateway_module.run_sessions(lambda: {}, 10)

    assert events == ["collect", "enqueue", ("flush", True)]


def test_loop_mode_keeps_collecting_and_flushes_without_blocking(monkeypatch):
    events = []

    class StopLoop(Exception):
        pass

    def collect(*_):
        events.append("collect")
        if events.count("collect") == 3:
            raise StopLoop
        return []

    monkeypatch.setattr(gateway_module, "collect_session", collect)
    monkeypatch.setattr(gateway_module, "enqueue", lambda *_: events.append("enqueue"))
    monkeypatch.setattr(
        gateway_module,
        "flush_queue",
        lambda wait_for_retry: events.append(("flush", wait_for_retry)),
    )

    with pytest.raises(StopLoop):
        gateway_module.run_sessions(lambda: {}, 10, loop=True)

    assert events == [
        "collect", "enqueue", ("flush", False),
        "collect", "enqueue", ("flush", False),
        "collect",
    ]


def test_mqtt_config_reads_env_and_allows_cli_overrides(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER_URL", "192.168.1.50")
    monkeypatch.setenv("MQTT_BROKER_PORT", "1884")
    monkeypatch.setenv("MQTT_USER", "gateway")
    monkeypatch.setenv("MQTT_PASSWORD", "secret")
    monkeypatch.delenv("MQTT_TOPIC", raising=False)

    assert gateway_module.mqtt_config() == (
        "192.168.1.50", 1884, "gateway", "secret", "vsm/sensor/+"
    )
    assert gateway_module.mqtt_config(
        broker_url="127.0.0.1", broker_port=8883, topic="vsm/sensor/node-1"
    ) == ("127.0.0.1", 8883, "gateway", "secret", "vsm/sensor/node-1")


def test_mqtt_config_defaults_port_and_validates(monkeypatch):
    monkeypatch.delenv("MQTT_BROKER_URL", raising=False)
    monkeypatch.delenv("MQTT_BROKER_PORT", raising=False)
    monkeypatch.delenv("MQTT_USER", raising=False)
    monkeypatch.delenv("MQTT_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="missing mqtt broker url"):
        gateway_module.mqtt_config()

    assert gateway_module.mqtt_config(broker_url="127.0.0.1") == (
        "127.0.0.1", 1883, "", "", "vsm/sensor/+"
    )

    with pytest.raises(ValueError, match="invalid mqtt broker port"):
        gateway_module.mqtt_config(broker_url="127.0.0.1", broker_port=70000)


def test_mqtt_collector_batches_enriches_and_disconnects_in_single_session(monkeypatch):
    enqueued = []
    flushes = []
    subscribed = []
    disconnected = []

    monkeypatch.setattr(gateway_module, "read_machine_temp", lambda: 31.5)
    monkeypatch.setattr(gateway_module.time, "time", lambda: 1700000000.0)
    monkeypatch.setattr(gateway_module, "enqueue", lambda batch: enqueued.append(batch))
    monkeypatch.setattr(
        gateway_module, "flush_queue", lambda wait_for_retry: flushes.append(wait_for_retry)
    )

    fake_client = SimpleNamespace(
        subscribe=lambda topic: subscribed.append(topic),
        disconnect=lambda: disconnected.append(True),
    )
    collector = gateway_module.MqttSessionCollector(n=2, loop=False)
    collector.on_connect(fake_client, None, None, 0, None)
    assert subscribed == ["vsm/sensor/+"]

    def send(payload, topic="vsm/sensor/node-1"):
        msg = SimpleNamespace(topic=topic, payload=payload.encode("utf-8"))
        collector.on_message(fake_client, None, msg)

    # Invalid payloads are ignored without affecting the buffer
    send("not-json")
    send("[1, 2, 3]")
    assert enqueued == []

    send(json.dumps({"node_id": "node-1", "seq": 1, "hr": 72, "spo2": 98, "body_temp": 36.5}))
    assert enqueued == []
    assert disconnected == []

    send(json.dumps({"node_id": "node-1", "seq": 2, "hr": None, "spo2": None, "body_temp": 36.6}))
    assert len(enqueued) == 1
    assert enqueued[0] == [
        {
            "node_id": "node-1", "seq": 1, "hr": 72, "spo2": 98,
            "body_temp": 36.5, "machine_temp": 31.5, "t": 1700000000.0,
        },
        {
            "node_id": "node-1", "seq": 2, "hr": None, "spo2": None,
            "body_temp": 36.6, "machine_temp": 31.5, "t": 1700000000.0,
        },
    ]
    assert flushes == [True]
    assert disconnected == [True]


def test_mqtt_collector_loop_mode_flushes_non_blocking_without_disconnect(monkeypatch):
    enqueued = []
    flushes = []
    disconnected = []

    monkeypatch.setattr(gateway_module, "read_machine_temp", lambda: None)
    monkeypatch.setattr(gateway_module.time, "time", lambda: 1700000001.0)
    monkeypatch.setattr(gateway_module, "enqueue", lambda batch: enqueued.append(batch))
    monkeypatch.setattr(
        gateway_module, "flush_queue", lambda wait_for_retry: flushes.append(wait_for_retry)
    )

    fake_client = SimpleNamespace(disconnect=lambda: disconnected.append(True))
    collector = gateway_module.MqttSessionCollector(n=2, loop=True)

    for seq in range(4):
        msg = SimpleNamespace(
            topic="vsm/sensor/node-1",
            payload=json.dumps({"node_id": "node-1", "seq": seq, "hr": 70}).encode("utf-8"),
        )
        collector.on_message(fake_client, None, msg)

    assert len(enqueued) == 2
    assert flushes == [False, False]
    assert disconnected == []


def test_queued_session_preserves_session_id_across_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_module, "SPOOL_DIR", tmp_path)
    monkeypatch.setattr(gateway_module, "QUEUE_DB", tmp_path / "queue.sqlite3")
    gateway_module.init_queue()

    gateway_module.enqueue([{"hr": 75, "spo2": 99}])
    row_id, payload, attempts, next_attempt_at, session_id = gateway_module.oldest_queued()
    assert row_id == 1
    assert json.loads(payload) == [{"hr": 75, "spo2": 99}]
    assert attempts == 0
    assert session_id

    seen_session_ids = []

    def flaky_upload(readings, session_id=None):
        seen_session_ids.append(session_id)
        if len(seen_session_ids) == 1:
            raise gateway_module.requests.exceptions.ConnectionError("offline")

    monkeypatch.setattr(gateway_module, "upload", flaky_upload)
    gateway_module.flush_queue(wait_for_retry=False)
    deferred = gateway_module.oldest_queued()
    assert deferred is not None

    # Allow immediate retry on next flush
    monkeypatch.setattr(gateway_module.time, "time", lambda: deferred[3] + 10)
    gateway_module.flush_queue(wait_for_retry=False)
    assert gateway_module.oldest_queued() is None
    assert len(seen_session_ids) == 2
    assert seen_session_ids[0] == seen_session_ids[1] == session_id
