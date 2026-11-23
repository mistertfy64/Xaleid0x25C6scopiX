"""Vital Sign Monitor - cloud backend (FastAPI + SQLite).

Run:  uvicorn main:app --host 127.0.0.1 --port 8000
Seed a user:  python main.py add-user <username> <password>
"""
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv(Path(__file__).with_name(".env"))

DB = os.environ.get("VSM_DB", str(Path(__file__).with_name("vsm.db")))
TOKEN_TTL = 15 * 60  # seconds
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW = 60
login_attempts: dict[str, deque[float]] = {}
login_attempts_lock = threading.Lock()
last_rate_limit_cleanup = 0.0


def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    return connection


def cleanup_expired_tokens(connection):
    connection.execute(
        "DELETE FROM tokens WHERE created_at <= ?",
        (time.time() - TOKEN_TTL,),
    )


def init():
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            salt TEXT NOT NULL, pw_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tokens(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_tokens_created_at
            ON tokens(created_at);
        CREATE TABLE IF NOT EXISTS measurements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            data TEXT NOT NULL,
            session_id TEXT);
        """)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(measurements)").fetchall()
        }
        if "session_id" not in columns:
            connection.execute("ALTER TABLE measurements ADD COLUMN session_id TEXT")
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_measurements_user_session
                ON measurements(user_id, session_id)
                WHERE session_id IS NOT NULL
        """)
        cleanup_expired_tokens(connection)


def hash_pw(password, salt):
    return hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1
    ).hex()


def add_user(username, password):
    salt = secrets.token_hex(16)
    with db() as connection:
        connection.execute(
            "INSERT INTO users(username,salt,pw_hash) VALUES(?,?,?)",
            (username, salt, hash_pw(password, salt)),
        )


app = FastAPI(title="Vital Sign Monitor Cloud")
cors_origins = [
    origin.strip()
    for origin in os.environ.get("VSM_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
init()


class LoginReq(BaseModel):
    username: str
    password: str
    client: str = "gateway"


class MeasureReq(BaseModel):
    userID: int
    token: str
    measurements: Any
    session_id: Optional[str] = None


def enforce_login_rate_limit(request: Request):
    global last_rate_limit_cleanup
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with login_attempts_lock:
        if now - last_rate_limit_cleanup >= LOGIN_RATE_WINDOW:
            for ip, timestamps in list(login_attempts.items()):
                while timestamps and now - timestamps[0] >= LOGIN_RATE_WINDOW:
                    timestamps.popleft()
                if not timestamps:
                    del login_attempts[ip]
            last_rate_limit_cleanup = now

        timestamps = login_attempts.setdefault(client_ip, deque())
        while timestamps and now - timestamps[0] >= LOGIN_RATE_WINDOW:
            timestamps.popleft()
        if len(timestamps) >= LOGIN_RATE_LIMIT:
            retry_after = max(1, int(LOGIN_RATE_WINDOW - (now - timestamps[0])))
            raise HTTPException(
                429,
                "too many login attempts",
                headers={"Retry-After": str(retry_after)},
            )
        timestamps.append(now)


@app.post("/login")
def login(r: LoginReq, request: Request):
    enforce_login_rate_limit(request)
    if r.client not in ("gateway", "viewer"):
        raise HTTPException(400, "client must be 'gateway' or 'viewer'")
    with db() as connection:
        cleanup_expired_tokens(connection)
        user = connection.execute(
            "SELECT * FROM users WHERE username=?", (r.username,)
        ).fetchone()
        if not user or not hmac.compare_digest(
            user["pw_hash"], hash_pw(r.password, user["salt"])
        ):
            return {"success": False}
        token = secrets.token_urlsafe(32)
        connection.execute(
            "INSERT INTO tokens VALUES(?,?,?,?)",
            (token, user["id"], r.client, time.time()),
        )
    return {"success": True, "token": token, "userID": user["id"]}


def check_token(connection, token, user_id, kind):
    stored_token = connection.execute(
        "SELECT * FROM tokens WHERE token=?", (token,)
    ).fetchone()
    valid = (
        stored_token
        and stored_token["user_id"] == user_id
        and stored_token["kind"] == kind
        and time.time() - stored_token["created_at"] < TOKEN_TTL
    )
    return stored_token if valid else None


@app.post("/measurements")
def post_measurements(r: MeasureReq):
    session_id = r.session_id.strip() if r.session_id and r.session_id.strip() else None
    with db() as connection:
        cleanup_expired_tokens(connection)
        connection.commit()
        if not check_token(connection, r.token, r.userID, "gateway"):
            raise HTTPException(401, "invalid token")
        connection.execute("DELETE FROM tokens WHERE token=?", (r.token,))
        cursor = connection.execute(
            "INSERT OR IGNORE INTO measurements(user_id,created_at,data,session_id) "
            "VALUES(?,?,?,?)",
            (r.userID, time.time(), json.dumps(r.measurements), session_id),
        )
        duplicate = cursor.rowcount == 0
    if duplicate:
        return {"success": True, "duplicate": True}
    return {"success": True}


@app.get("/measurements")
def get_measurements(
    userID: int,
    authorization: Optional[str] = Header(None),
    limit: int = 100,
):
    token = (authorization or "").removeprefix("Bearer ").strip()
    with db() as connection:
        cleanup_expired_tokens(connection)
        connection.commit()
        if not check_token(connection, token, userID, "viewer"):
            raise HTTPException(401, "invalid token")
        rows = connection.execute(
            "SELECT id,created_at,data FROM measurements WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (userID, limit),
        ).fetchall()
    return {
        "success": True,
        "measurements": [
            {
                "id": row["id"],
                "timestamp": row["created_at"],
                "data": json.loads(row["data"]),
            }
            for row in rows
        ],
    }


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "add-user":
    add_user(sys.argv[2], sys.argv[3])
    print("user added")
