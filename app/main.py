from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DOPE_DB_PATH", ROOT.parent / "dope.db"))
SECRET_KEY = os.environ.get("DOPE_SECRET_KEY", "dev-only-change-me")
COOKIE_SECURE = os.environ.get("DOPE_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
COOKIE_NAME = "dope_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30

app = FastAPI(title="Dope")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL,
              display_name TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dopes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description_html TEXT NOT NULL,
              time_minutes INTEGER NOT NULL,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              assigned_to INTEGER REFERENCES users(id),
              assigned_at TEXT,
              completed_by INTEGER REFERENCES users(id),
              completed_at TEXT,
              completion_description TEXT,
              archived_by INTEGER REFERENCES users(id),
              archived_at TEXT
            );

            CREATE TABLE IF NOT EXISTS assignment_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              display_name TEXT NOT NULL,
              assigned_at TEXT NOT NULL,
              unassigned_at TEXT
            );

            CREATE TABLE IF NOT EXISTS commit_links (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              url TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )


@app.on_event("startup")
def startup() -> None:
    init_db()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, digest_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return hmac.compare_digest(actual, expected)


def sign(payload: dict[str, Any]) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def unsign(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    expected = hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:
        return None
    if payload.get("exp", 0) < int(time.time()):
        return None
    return payload


def set_session(response: Response, user_id: int) -> None:
    token = sign({"sub": user_id, "exp": int(time.time()) + SESSION_MAX_AGE})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )


def current_user(dope_session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> sqlite3.Row:
    payload = unsign(dope_session)
    if not payload:
        raise HTTPException(status_code=401, detail="Not signed in")
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (payload["sub"],)).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


class AuthIn(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=4, max_length=200)
    display_name: str | None = Field(default=None, max_length=120)


class DopeIn(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    description_html: str = Field(min_length=1, max_length=250_000)
    time_text: str = Field(min_length=1, max_length=40)


class CompleteIn(BaseModel):
    commit_links: list[str] = Field(min_length=1, max_length=20)
    completion_description: str = Field(default="", max_length=20_000)


def parse_time_to_minutes(value: str) -> int:
    text = value.strip().lower()
    token_re = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)?")
    matches = list(token_re.finditer(text))
    consumed = "".join(match.group(0) for match in matches)
    if not matches or re.sub(r"\s+", "", consumed) != re.sub(r"\s+", "", text):
        raise HTTPException(status_code=400, detail="Use time like 30min, 2hr, 0.5hr, or 2hr 30min")
    minutes = 0.0
    for match in matches:
        amount = float(match.group(1))
        unit = match.group(2) or "hr"
        minutes += amount * 60 if unit.startswith("h") else amount
    if minutes <= 0 or minutes > 60 * 24 * 365:
        raise HTTPException(status_code=400, detail="Time must be greater than zero")
    return max(1, round(minutes))


def status_for(row: sqlite3.Row) -> str:
    if row["archived_at"]:
        return "archived"
    if row["completed_at"]:
        return "completed"
    return "active"


def user_public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"]}


def dope_payload(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    users = {
        u["id"]: u
        for u in conn.execute(
            """
            SELECT DISTINCT users.* FROM users
            WHERE id IN (?, ?, ?, ?)
            """,
            (row["created_by"], row["assigned_to"], row["completed_by"], row["archived_by"]),
        ).fetchall()
    }
    history = conn.execute(
        "SELECT * FROM assignment_history WHERE dope_id = ? ORDER BY assigned_at DESC, id DESC",
        (row["id"],),
    ).fetchall()
    links = conn.execute("SELECT url FROM commit_links WHERE dope_id = ? ORDER BY id", (row["id"],)).fetchall()
    return {
        "id": row["id"],
        "title": row["title"],
        "description_html": row["description_html"],
        "time_minutes": row["time_minutes"],
        "created_at": row["created_at"],
        "assigned_at": row["assigned_at"],
        "completed_at": row["completed_at"],
        "completion_description": row["completion_description"] or "",
        "archived_at": row["archived_at"],
        "status": status_for(row),
        "created_by": user_public(users.get(row["created_by"])),
        "assigned_to": user_public(users.get(row["assigned_to"])),
        "completed_by": user_public(users.get(row["completed_by"])),
        "archived_by": user_public(users.get(row["archived_by"])),
        "assignment_history": [dict(h) for h in history],
        "commit_links": [l["url"] for l in links],
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "templates" / "index.html")


@app.post("/api/auth/signup")
def signup(data: AuthIn) -> dict[str, Any]:
    username = data.username.strip()
    display_name = (data.display_name or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Display name is required")
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                (username, hash_password(data.password), display_name, now_iso()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Username already exists") from None
    return {"ok": True, "username": username}


@app.post("/api/auth/login")
def login(data: AuthIn, response: Response) -> dict[str, Any]:
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (data.username.strip(),)).fetchone()
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    set_session(response, user["id"])
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/me")
def me(user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    return user_public(current_user(user_cookie))  # type: ignore[return-value]


@app.get("/api/dopes")
def list_dopes(status: str = "active", user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    current_user(user_cookie)
    where = {
        "active": "archived_at IS NULL AND completed_at IS NULL",
        "completed": "archived_at IS NULL AND completed_at IS NOT NULL",
        "archived": "archived_at IS NOT NULL",
        "all": "1 = 1",
    }.get(status)
    if not where:
        raise HTTPException(status_code=400, detail="Bad status")
    order = "completed_at DESC, id DESC" if status == "completed" else "id DESC"
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM dopes WHERE {where} ORDER BY {order}").fetchall()
        return [dope_payload(row, conn) for row in rows]


@app.post("/api/dopes")
def create_dope(data: DopeIn, user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = current_user(user_cookie)
    minutes = parse_time_to_minutes(data.time_text)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO dopes (title, description_html, time_minutes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (data.title.strip(), data.description_html, minutes, user["id"], now_iso()),
        )
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dope_payload(row, conn)


@app.post("/api/dopes/{dope_id}/assign")
def assign_dope(dope_id: int, user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = current_user(user_cookie)
    assigned_at = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["completed_at"]:
            raise HTTPException(status_code=404, detail="Active dope not found")
        conn.execute(
            "UPDATE dopes SET assigned_to = ?, assigned_at = ? WHERE id = ?",
            (user["id"], assigned_at, dope_id),
        )
        conn.execute(
            "INSERT INTO assignment_history (dope_id, user_id, display_name, assigned_at) VALUES (?, ?, ?, ?)",
            (dope_id, user["id"], user["display_name"], assigned_at),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)


@app.post("/api/dopes/{dope_id}/unassign")
def unassign_dope(dope_id: int, user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    current_user(user_cookie)
    unassigned_at = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or not row["assigned_to"] or row["archived_at"] or row["completed_at"]:
            raise HTTPException(status_code=400, detail="Dope is not assigned")
        conn.execute("UPDATE dopes SET assigned_to = NULL, assigned_at = NULL WHERE id = ?", (dope_id,))
        conn.execute(
            """
            UPDATE assignment_history SET unassigned_at = ?
            WHERE id = (
              SELECT id FROM assignment_history
              WHERE dope_id = ? AND unassigned_at IS NULL
              ORDER BY id DESC LIMIT 1
            )
            """,
            (unassigned_at, dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)


@app.post("/api/dopes/{dope_id}/complete")
def complete_dope(dope_id: int, data: CompleteIn, user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = current_user(user_cookie)
    clean_links = []
    for link in data.commit_links:
        url = link.strip()
        if not re.match(r"^https?://", url):
            raise HTTPException(status_code=400, detail="Commit links must start with http:// or https://")
        clean_links.append(url)
    completed_at = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["completed_at"]:
            raise HTTPException(status_code=404, detail="Active dope not found")
        conn.execute(
            """
            UPDATE dopes
            SET completed_by = ?, completed_at = ?, completion_description = ?, assigned_to = NULL, assigned_at = NULL
            WHERE id = ?
            """,
            (user["id"], completed_at, data.completion_description.strip(), dope_id),
        )
        conn.executemany(
            "INSERT INTO commit_links (dope_id, url, created_at) VALUES (?, ?, ?)",
            [(dope_id, link, completed_at) for link in clean_links],
        )
        conn.execute(
            """
            UPDATE assignment_history SET unassigned_at = COALESCE(unassigned_at, ?)
            WHERE dope_id = ? AND unassigned_at IS NULL
            """,
            (completed_at, dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)


@app.post("/api/dopes/{dope_id}/archive")
def archive_dope(dope_id: int, user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = current_user(user_cookie)
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dope not found")
        conn.execute(
            "UPDATE dopes SET archived_by = ?, archived_at = ? WHERE id = ?",
            (user["id"], now_iso(), dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)


@app.post("/api/dopes/{dope_id}/restore")
def restore_dope(dope_id: int, user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    current_user(user_cookie)
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dope not found")
        conn.execute("UPDATE dopes SET archived_by = NULL, archived_at = NULL WHERE id = ?", (dope_id,))
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)
