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
import urllib.error
import urllib.request

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


class AnalyzeReq(BaseModel):
    userID: int
    node_id: Optional[str] = None
    limit: int = 100


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


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric else None


def _score_hr(hr: Optional[float]) -> int:
    if hr is None:
        return 0
    if hr <= 40 or hr >= 131:
        return 3
    if hr >= 111:
        return 2
    if hr <= 50 or hr >= 91:
        return 1
    return 0


def _score_spo2(spo2: Optional[float]) -> int:
    if spo2 is None:
        return 0
    if spo2 <= 91:
        return 3
    if spo2 <= 93:
        return 2
    if spo2 <= 95:
        return 1
    return 0


def _score_body_temp(temp: Optional[float]) -> int:
    if temp is None:
        return 0
    if temp <= 35.0 or temp >= 39.1:
        return 3
    if temp >= 38.1:
        return 2
    if temp <= 36.0 or temp >= 37.6:
        return 1
    return 0


def _metric_stats(values: list[float], stable_delta: float, digits: int = 1) -> Optional[dict[str, Any]]:
    if not values:
        return None
    avg = sum(values) / len(values)
    trend = "stable"
    if len(values) >= 4:
        mid = len(values) // 2
        early_avg = sum(values[:mid]) / mid
        late_avg = sum(values[mid:]) / (len(values) - mid)
        diff = late_avg - early_avg
        if diff >= stable_delta:
            trend = "rising"
        elif diff <= -stable_delta:
            trend = "falling"
    return {
        "min": round(min(values), digits),
        "avg": round(avg, digits),
        "max": round(max(values), digits),
        "latest": round(values[-1], digits),
        "trend": trend,
    }


def analyze_vital_signs(readings: list[dict[str, Any]]) -> dict[str, Any]:
    disclaimer = "การวิเคราะห์นี้เป็นข้อมูลเบื้องต้นเพื่อช่วยเฝ้าระวังเท่านั้น ไม่ใช่การวินิจฉัยทางการแพทย์"
    if not readings:
        return {
            "sample_count": 0,
            "risk_score": 0,
            "risk_level": "unknown",
            "risk_label": "ยังไม่มีข้อมูล",
            "stats": {},
            "findings": ["ยังไม่มีข้อมูลการวัดเพียงพอสำหรับวิเคราะห์"],
            "summary": "ยังไม่มีข้อมูลสัญญาณชีพสำหรับวิเคราะห์ กรุณารอข้อมูลจากโหนดเซ็นเซอร์",
            "disclaimer": disclaimer,
        }

    ordered = sorted(readings, key=lambda item: _to_float(item.get("t")) or 0.0)
    hr_vals = [v for item in ordered if (v := _to_float(item.get("hr"))) is not None]
    spo2_vals = [v for item in ordered if (v := _to_float(item.get("spo2"))) is not None]
    temp_vals = [v for item in ordered if (v := _to_float(item.get("body_temp"))) is not None]
    mach_vals = [v for item in ordered if (v := _to_float(item.get("machine_temp"))) is not None]

    latest_hr = hr_vals[-1] if hr_vals else None
    latest_spo2 = spo2_vals[-1] if spo2_vals else None
    latest_temp = temp_vals[-1] if temp_vals else None
    latest_mach = mach_vals[-1] if mach_vals else None

    hr_score = _score_hr(latest_hr)
    spo2_score = _score_spo2(latest_spo2)
    temp_score = _score_body_temp(latest_temp)
    risk_score = hr_score + spo2_score + temp_score
    max_single = max(hr_score, spo2_score, temp_score)

    if risk_score >= 5 or max_single >= 3:
        risk_level, risk_label = "high", "ความเสี่ยงสูง"
    elif risk_score >= 2:
        risk_level, risk_label = "moderate", "ควรเฝ้าระวัง"
    else:
        risk_level, risk_label = "low", "ความเสี่ยงต่ำ"

    stats = {
        "hr": _metric_stats(hr_vals, stable_delta=5.0, digits=0),
        "spo2": _metric_stats(spo2_vals, stable_delta=1.5, digits=0),
        "body_temp": _metric_stats(temp_vals, stable_delta=0.3, digits=1),
        "machine_temp": _metric_stats(mach_vals, stable_delta=2.0, digits=1),
    }

    findings = []
    if latest_hr is not None:
        if latest_hr > 100:
            findings.append(f"ชีพจรล่าสุดสูงกว่าเกณฑ์ปกติ ({latest_hr:.0f} bpm)")
        elif latest_hr < 60:
            findings.append(f"ชีพจรล่าสุดต่ำกว่าเกณฑ์ปกติ ({latest_hr:.0f} bpm)")
        if stats["hr"] and stats["hr"]["trend"] == "rising":
            findings.append("แนวโน้มชีพจรมีทิศทางเพิ่มขึ้นต่อเนื่อง")
        elif stats["hr"] and stats["hr"]["trend"] == "falling":
            findings.append("แนวโน้มชีพจรมีทิศทางลดลง")
    if latest_spo2 is not None:
        if latest_spo2 < 95:
            findings.append(f"ค่า SpO₂ ล่าสุดต่ำกว่าเกณฑ์ปกติ ({latest_spo2:.0f}%) ควรตรวจสอบการวางนิ้วและวัดซ้ำ")
        if stats["spo2"] and stats["spo2"]["trend"] == "falling":
            findings.append("แนวโน้มค่า SpO₂ ลดลง ควรเฝ้าระวังอย่างใกล้ชิด")
    if latest_temp is not None:
        if latest_temp > 37.5:
            findings.append(f"อุณหภูมิร่างกายล่าสุดสูงกว่าปกติ ({latest_temp:.1f} °C)")
        elif latest_temp < 36.0:
            findings.append(f"อุณหภูมิร่างกายล่าสุดค่อนข้างต่ำ ({latest_temp:.1f} °C)")
    if latest_mach is not None and latest_mach > 45.0:
        findings.append(f"อุณหภูมิเครื่องค่อนข้างสูง ({latest_mach:.1f} °C) ควรตรวจสอบการระบายความร้อนของอุปกรณ์")

    if not findings:
        findings.append("สัญญาณชีพหลักทั้งหมดอยู่ในเกณฑ์ปกติและมีแนวโน้มคงที่")

    if risk_level == "high":
        summary = (
            f"คะแนนเฝ้าระวังเบื้องต้น {risk_score} ({risk_label}): "
            + " · ".join(findings[:2])
            + " แนะนำให้ตรวจสอบการวางเซ็นเซอร์ วัดซ้ำทันที และปรึกษาบุคลากรทางการแพทย์หากค่าผิดปกติต่อเนื่อง"
        )
    elif risk_level == "moderate":
        summary = (
            f"คะแนนเฝ้าระวังเบื้องต้น {risk_score} ({risk_label}): "
            + " · ".join(findings[:2])
            + " แนะนำให้นั่งพัก 2–3 นาทีแล้ววัดซ้ำเพื่อติดตามแนวโน้ม"
        )
    else:
        summary = (
            f"คะแนนเฝ้าระวังเบื้องต้น {risk_score} ({risk_label}): "
            "ภาพรวมสัญญาณชีพอยู่ในเกณฑ์ปกติ สามารถติดตามต่อเนื่องตามรอบปกติได้"
        )

    return {
        "sample_count": len(ordered),
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_label": risk_label,
        "stats": stats,
        "findings": findings,
        "summary": summary,
        "disclaimer": disclaimer,
    }


def summarize_with_gemini(analysis: dict[str, Any], node_id: Optional[str] = None) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    prompt = (
        "คุณคือผู้ช่วยสรุปข้อมูลสัญญาณชีพสำหรับระบบติดตามสุขภาพ (ไม่ใช่แพทย์ ห้ามวินิจฉัยโรค)\n"
        "กรุณาสรุปภาพรวมและคำแนะนำการเฝ้าระวังเบื้องต้นเป็นภาษาไทยกระชับ 2-3 ประโยค จากข้อมูลสถิติต่อไปนี้:\n"
        f"{json.dumps({'node_id': node_id or 'all', 'risk_score': analysis['risk_score'], 'risk_label': analysis['risk_label'], 'stats': analysis['stats'], 'findings': analysis['findings']}, ensure_ascii=False)}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 220},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts).strip()
        return text or None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError):
        return None


@app.post("/analyze")
def post_analyze(
    r: AnalyzeReq,
    authorization: Optional[str] = Header(None),
):
    token = (authorization or "").removeprefix("Bearer ").strip()
    limit = max(1, min(r.limit, 200))
    with db() as connection:
        cleanup_expired_tokens(connection)
        connection.commit()
        if not check_token(connection, token, r.userID, "viewer"):
            raise HTTPException(401, "invalid token")
        rows = connection.execute(
            "SELECT created_at, data FROM measurements WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (r.userID, limit),
        ).fetchall()

    readings: list[dict[str, Any]] = []
    for row in rows:
        raw_items = json.loads(row["data"])
        items = raw_items if isinstance(raw_items, list) else [raw_items]
        for item in items:
            if not isinstance(item, dict):
                continue
            if r.node_id and r.node_id != "all" and item.get("node_id") != r.node_id:
                continue
            enriched = dict(item)
            enriched.setdefault("t", row["created_at"])
            readings.append(enriched)

    analysis = analyze_vital_signs(readings)
    source = "rule-based"
    if readings:
        ai_text = summarize_with_gemini(analysis, node_id=r.node_id)
        if ai_text:
            analysis["summary"] = ai_text
            source = "gemini"

    return {"success": True, "source": source, **analysis}


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "add-user":
    add_user(sys.argv[2], sys.argv[3])
    print("user added")
