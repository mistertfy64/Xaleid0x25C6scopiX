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
