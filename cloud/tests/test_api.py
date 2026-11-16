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
