import importlib.util
from contextlib import closing
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("VSM_CORS_ORIGINS", "https://dashboard.example.test")
    backend_path = Path(__file__).resolve().parents[1] / "main.py"
    module_name = f"cloud_main_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, backend_path)
    backend = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = backend
    try:
        spec.loader.exec_module(backend)
        with TestClient(backend.app) as client:
            yield backend, client
    finally:
        sys.modules.pop(module_name, None)


def create_user(backend):
    backend.add_user("alice", "correct-horse")


def login(client, kind="gateway"):
    return client.post(
        "/login",
        json={"username": "alice", "password": "correct-horse", "client": kind},
    )


def test_login_success_and_failure(api):
    backend, client = api
    create_user(backend)

    success = login(client)
    assert success.status_code == 200
    assert success.json()["success"] is True
    assert success.json()["userID"] == 1
    assert success.json()["token"]

    failure = client.post(
        "/login",
        json={"username": "alice", "password": "wrong", "client": "gateway"},
    )
    assert failure.status_code == 200
    assert failure.json() == {"success": False}


def test_gateway_token_is_consumed_after_upload(api):
    backend, client = api
    create_user(backend)
    credentials = login(client).json()
    payload = {
        "userID": credentials["userID"],
        "token": credentials["token"],
        "measurements": [{"hr": 72, "spo2": 98}],
    }

    assert client.post("/measurements", json=payload).status_code == 200
    assert client.post("/measurements", json=payload).status_code == 401


def test_wrong_user_id_is_rejected_without_consuming_token(api):
    backend, client = api
    create_user(backend)
    credentials = login(client).json()
    payload = {
        "userID": credentials["userID"] + 1,
        "token": credentials["token"],
        "measurements": [],
    }

    assert client.post("/measurements", json=payload).status_code == 401
    payload["userID"] = credentials["userID"]
    assert client.post("/measurements", json=payload).status_code == 200


def test_viewer_token_can_be_reused(api):
    backend, client = api
    create_user(backend)
    credentials = login(client, kind="viewer").json()
    headers = {"Authorization": f"Bearer {credentials['token']}"}

    first = client.get(
        "/measurements", params={"userID": credentials["userID"]}, headers=headers
    )
    second = client.get(
        "/measurements", params={"userID": credentials["userID"]}, headers=headers
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["measurements"] == second.json()["measurements"] == []


def test_expired_token_is_rejected(api):
    backend, client = api
    create_user(backend)
    credentials = login(client, kind="viewer").json()
    expired_at = time.time() - backend.TOKEN_TTL - 1
    with closing(sqlite3.connect(backend.DB)) as connection, connection:
        connection.execute(
            "UPDATE tokens SET created_at=? WHERE token=?",
            (expired_at, credentials["token"]),
        )

    response = client.get(
        "/measurements",
        params={"userID": credentials["userID"]},
        headers={"Authorization": f"Bearer {credentials['token']}"},
    )

    assert response.status_code == 401


def test_duplicate_session_id_is_deduplicated(api):
    backend, client = api
    create_user(backend)

    first_creds = login(client).json()
    first = client.post(
        "/measurements",
        json={
            "userID": first_creds["userID"],
            "token": first_creds["token"],
            "session_id": "sess-abc-123",
            "measurements": [{"node_id": "node-1", "hr": 72, "spo2": 98}],
        },
    )
    assert first.status_code == 200
    assert first.json() == {"success": True}

    second_creds = login(client).json()
    second = client.post(
        "/measurements",
        json={
            "userID": second_creds["userID"],
            "token": second_creds["token"],
            "session_id": "sess-abc-123",
            "measurements": [{"node_id": "node-1", "hr": 72, "spo2": 98}],
        },
    )
    assert second.status_code == 200
    assert second.json() == {"success": True, "duplicate": True}

    viewer = login(client, kind="viewer").json()
    fetched = client.get(
        "/measurements",
        params={"userID": viewer["userID"]},
        headers={"Authorization": f"Bearer {viewer['token']}"},
    )
    assert fetched.status_code == 200
    assert len(fetched.json()["measurements"]) == 1


def test_analyze_requires_viewer_token(api):
    backend, client = api
    create_user(backend)
    response = client.post("/analyze", json={"userID": 1})
    assert response.status_code == 401


def test_analyze_rule_based_fallback_and_node_filter(api, monkeypatch):
    backend, client = api
    create_user(backend)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    gw = login(client, kind="gateway").json()
    client.post(
        "/measurements",
        json={
            "userID": gw["userID"],
            "token": gw["token"],
            "measurements": [
                {"node_id": "node-1", "hr": 72, "spo2": 98, "body_temp": 36.6, "t": 100},
                {"node_id": "node-2", "hr": 105, "spo2": 93, "body_temp": 37.9, "t": 110},
                {"node_id": "node-2", "hr": 112, "spo2": 92, "body_temp": 38.2, "t": 120},
            ],
        },
    )

    viewer = login(client, kind="viewer").json()
    headers = {"Authorization": f"Bearer {viewer['token']}"}

    node1 = client.post(
        "/analyze",
        json={"userID": viewer["userID"], "node_id": "node-1"},
        headers=headers,
    )
    assert node1.status_code == 200
    data1 = node1.json()
    assert data1["success"] is True
    assert data1["source"] == "rule-based"
    assert data1["risk_level"] == "low"
    assert data1["sample_count"] == 1

    node2 = client.post(
        "/analyze",
        json={"userID": viewer["userID"], "node_id": "node-2"},
        headers=headers,
    )
    assert node2.status_code == 200
    data2 = node2.json()
    assert data2["source"] == "rule-based"
    assert data2["risk_level"] == "high"
    assert data2["risk_score"] >= 5
    assert data2["sample_count"] == 2


def test_analyze_uses_gemini_when_configured(api, monkeypatch):
    backend, client = api
    create_user(backend)

    gw = login(client, kind="gateway").json()
    client.post(
        "/measurements",
        json={
            "userID": gw["userID"],
            "token": gw["token"],
            "measurements": [{"node_id": "node-1", "hr": 74, "spo2": 98, "body_temp": 36.7, "t": 100}],
        },
    )

    monkeypatch.setattr(
        backend,
        "summarize_with_gemini",
        lambda analysis, node_id=None: "สรุปจาก Gemini: สัญญาณชีพปกติทั้งหมด",
    )
    viewer = login(client, kind="viewer").json()
    response = client.post(
        "/analyze",
        json={"userID": viewer["userID"], "node_id": "node-1"},
        headers={"Authorization": f"Bearer {viewer['token']}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "gemini"
    assert body["summary"] == "สรุปจาก Gemini: สัญญาณชีพปกติทั้งหมด"
