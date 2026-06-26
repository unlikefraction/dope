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
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DOPE_DB_PATH", ROOT.parent / "dope.db"))
SECRET_KEY = os.environ.get("DOPE_SECRET_KEY", "dev-only-change-me")
COOKIE_SECURE = os.environ.get("DOPE_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
COOKIE_NAME = "dope_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
IST_OFFSET = timedelta(hours=5, minutes=30)
DOPE_DAY_RESET = datetime_time(hour=9)

app = FastAPI(title="Dope")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def dope_day_for(value: str | datetime) -> date:
    parsed = parse_iso_datetime(value) if isinstance(value, str) else value.astimezone(timezone.utc)
    local = parsed + IST_OFFSET
    day = local.date()
    if local.time() < DOPE_DAY_RESET:
        day -= timedelta(days=1)
    return day


def current_dope_day() -> date:
    return dope_day_for(datetime.now(timezone.utc))


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
              color TEXT NOT NULL DEFAULT '#1a1a1a',
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
              unassigned_at TEXT,
              unassign_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS commit_links (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              url TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dope_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              version_number INTEGER NOT NULL,
              title TEXT NOT NULL,
              description_html TEXT NOT NULL,
              edited_by INTEGER NOT NULL REFERENCES users(id),
              edited_at TEXT NOT NULL,
              UNIQUE(dope_id, version_number)
            );

            CREATE TABLE IF NOT EXISTS dope_dependencies (
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              depends_on_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              PRIMARY KEY (dope_id, depends_on_id),
              CHECK (dope_id != depends_on_id)
            );

            CREATE TABLE IF NOT EXISTS api_keys (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              key_hash TEXT NOT NULL UNIQUE,
              prefix TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_used_at TEXT,
              revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS categories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE COLLATE NOCASE,
              color TEXT NOT NULL,
              position INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO dope_versions (dope_id, version_number, title, description_html, edited_by, edited_at)
            SELECT d.id, 1, d.title, d.description_html, d.created_by, d.created_at
            FROM dopes d
            WHERE NOT EXISTS (
              SELECT 1 FROM dope_versions v WHERE v.dope_id = d.id
            )
            """
        )
        assignment_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(assignment_history)").fetchall()
        }
        if "unassign_reason" not in assignment_columns:
            conn.execute("ALTER TABLE assignment_history ADD COLUMN unassign_reason TEXT")
        user_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "color" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN color TEXT NOT NULL DEFAULT '#1a1a1a'")
        dope_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(dopes)").fetchall()
        }
        if "category_id" not in dope_columns:
            conn.execute("ALTER TABLE dopes ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        if not conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
            seed_categories = [
                ("Silicon Centered", "#2e6f8e"),
                ("Silicon Supporting", "#5a9a6b"),
                ("Client Side", "#c56f2d"),
                ("Team", "#7a4d8e"),
            ]
            seeded_at = now_iso()
            conn.executemany(
                "INSERT INTO categories (name, color, position, created_at) VALUES (?, ?, ?, ?)",
                [(name, color, index, seeded_at) for index, (name, color) in enumerate(seed_categories)],
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


def hash_api_key(key: str) -> str:
    return hmac.new(SECRET_KEY.encode(), key.encode(), hashlib.sha256).hexdigest()


def current_user(dope_session: str | None = None, authorization: str | None = None) -> sqlite3.Row:
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization.split(" ", 1)[1].strip()
        if key:
            digest = hash_api_key(key)
            with db() as conn:
                api_key = conn.execute(
                    "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
                    (digest,),
                ).fetchone()
                if api_key:
                    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), api_key["id"]))
                    user = conn.execute("SELECT * FROM users WHERE id = ?", (api_key["user_id"],)).fetchone()
                    if user:
                        return user
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
    dependency_ids: list[int] = Field(default_factory=list, max_length=50)
    category_id: int | None = Field(default=None)


class DopeEditIn(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    description_html: str = Field(min_length=1, max_length=250_000)
    dependency_ids: list[int] = Field(default_factory=list, max_length=50)
    category_id: int | None = Field(default=None)


class UnassignIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class CompleteIn(BaseModel):
    completion_text: str | None = Field(default=None, max_length=30_000)
    commit_links: list[str] = Field(default_factory=list, max_length=50)
    completion_description: str = Field(default="", max_length=20_000)


class ProfileIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class ApiKeyIn(BaseModel):
    name: str = Field(default="API key", min_length=1, max_length=80)


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class DopeCategoryIn(BaseModel):
    category_id: int | None = Field(default=None)


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


def extract_http_links(value: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://[^\s<>)\"']+", value):
        url = match.group(0).rstrip(".,;:]}")
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def status_for(row: sqlite3.Row) -> str:
    if row["archived_at"]:
        return "archived"
    if row["completed_at"]:
        return "completed"
    return "active"


def user_public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "color": row["color"]}


def normalize_color(value: str | None, fallback: str = "#1a1a1a") -> str:
    if value and re.match(r"^#[0-9a-fA-F]{6}$", value):
        return value.lower()
    return fallback


def category_public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "color": row["color"]}


def validate_category_id(conn: sqlite3.Connection, category_id: int | None) -> int | None:
    if category_id is None:
        return None
    row = conn.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Category not found")
    return category_id


def add_dope_version(
    conn: sqlite3.Connection,
    dope_id: int,
    title: str,
    description_html: str,
    edited_by: int,
    edited_at: str | None = None,
) -> None:
    current = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) FROM dope_versions WHERE dope_id = ?",
        (dope_id,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO dope_versions (dope_id, version_number, title, description_html, edited_by, edited_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (dope_id, int(current) + 1, title, description_html, edited_by, edited_at or now_iso()),
    )


def clean_dependency_ids(raw_ids: list[int], dope_id: int) -> list[int]:
    seen: set[int] = set()
    ids: list[int] = []
    for raw_id in raw_ids:
        dep_id = int(raw_id)
        if dep_id == dope_id or dep_id in seen:
            continue
        seen.add(dep_id)
        ids.append(dep_id)
    return ids


def assert_dependencies_allowed(conn: sqlite3.Connection, dope_id: int, dependency_ids: list[int]) -> list[int]:
    ids = clean_dependency_ids(dependency_ids, dope_id)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM dopes WHERE archived_at IS NULL AND id IN ({placeholders})",
        ids,
    ).fetchall()
    found = {row["id"] for row in rows}
    missing = [dep_id for dep_id in ids if dep_id not in found]
    if missing:
        raise HTTPException(status_code=400, detail="Dependency dope not found")

    graph: dict[int, list[int]] = {}
    edges = conn.execute(
        "SELECT dope_id, depends_on_id FROM dope_dependencies WHERE dope_id != ?",
        (dope_id,),
    ).fetchall()
    for edge in edges:
        graph.setdefault(edge["dope_id"], []).append(edge["depends_on_id"])
    graph[dope_id] = ids

    def reaches_source(start: int) -> bool:
        stack = [start]
        visited: set[int] = set()
        while stack:
            current = stack.pop()
            if current == dope_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            stack.extend(graph.get(current, []))
        return False

    if any(reaches_source(dep_id) for dep_id in ids):
        raise HTTPException(status_code=400, detail="Circular dependencies are not allowed")
    return ids


def set_dope_dependencies(conn: sqlite3.Connection, dope_id: int, dependency_ids: list[int]) -> None:
    ids = assert_dependencies_allowed(conn, dope_id, dependency_ids)
    conn.execute("DELETE FROM dope_dependencies WHERE dope_id = ?", (dope_id,))
    conn.executemany(
        "INSERT INTO dope_dependencies (dope_id, depends_on_id, created_at) VALUES (?, ?, ?)",
        [(dope_id, dep_id, now_iso()) for dep_id in ids],
    )


def active_dependent_count(conn: sqlite3.Connection, dope_id: int) -> int:
    return len(active_dependent_rows(conn, dope_id))


def active_dependent_rows(conn: sqlite3.Connection, dope_id: int) -> list[dict[str, Any]]:
    edges = conn.execute(
        """
        SELECT dd.dope_id, dd.depends_on_id
        FROM dope_dependencies dd
        JOIN dopes child ON child.id = dd.dope_id
        WHERE child.archived_at IS NULL
          AND child.completed_at IS NULL
        """
    ).fetchall()
    reverse_graph: dict[int, list[int]] = {}
    for edge in edges:
        reverse_graph.setdefault(edge["depends_on_id"], []).append(edge["dope_id"])

    dependents: set[int] = set()
    depths: dict[int, int] = {}
    stack = [(child_id, 1) for child_id in reverse_graph.get(dope_id, [])]
    while stack:
        current, depth = stack.pop()
        if current in dependents:
            continue
        dependents.add(current)
        depths[current] = depth
        stack.extend((child_id, depth + 1) for child_id in reverse_graph.get(current, []))
    if not dependents:
        return []

    placeholders = ",".join("?" for _ in dependents)
    rows = conn.execute(
        f"""
        SELECT id, title, time_minutes, assigned_to, completed_at, archived_at
        FROM dopes
        WHERE id IN ({placeholders})
        """,
        tuple(dependents),
    ).fetchall()
    return sorted(
        [
            {
                "id": row["id"],
                "title": row["title"],
                "time_minutes": row["time_minutes"],
                "status": "archived" if row["archived_at"] else "completed" if row["completed_at"] else "active",
                "depth": depths.get(row["id"], 1),
            }
            for row in rows
        ],
        key=lambda item: (item["depth"], item["title"].lower(), item["id"]),
    )


def incomplete_dependency_rows(conn: sqlite3.Connection, dope_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.* FROM dope_dependencies dd
        JOIN dopes d ON d.id = dd.depends_on_id
        WHERE dd.dope_id = ? AND d.completed_at IS NULL
        ORDER BY d.id DESC
        """,
        (dope_id,),
    ).fetchall()


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
    versions = conn.execute(
        """
        SELECT v.id, v.version_number, v.title, v.description_html, v.edited_by, v.edited_at,
               u.username AS editor_username, u.display_name AS editor_display_name
        FROM dope_versions v
        JOIN users u ON u.id = v.edited_by
        WHERE v.dope_id = ?
        ORDER BY v.version_number DESC
        """,
        (row["id"],),
    ).fetchall()
    dependencies = conn.execute(
        """
        SELECT d.id, d.title, d.time_minutes, d.completed_at, d.archived_at
        FROM dope_dependencies dd
        JOIN dopes d ON d.id = dd.depends_on_id
        WHERE dd.dope_id = ?
        ORDER BY d.completed_at IS NULL DESC, d.id DESC
        """,
        (row["id"],),
    ).fetchall()
    dependents = active_dependent_rows(conn, row["id"])
    dependent_count = len(dependents)
    blocked_dependencies = [dep for dep in dependencies if dep["completed_at"] is None]
    category = category_public(
        conn.execute(
            "SELECT id, name, color FROM categories WHERE id = ?", (row["category_id"],)
        ).fetchone()
    ) if row["category_id"] else None
    return {
        "id": row["id"],
        "title": row["title"],
        "category": category,
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
        "dependencies": [
            {
                "id": dep["id"],
                "title": dep["title"],
                "time_minutes": dep["time_minutes"],
                "status": "archived" if dep["archived_at"] else "completed" if dep["completed_at"] else "active",
                "completed_at": dep["completed_at"],
            }
            for dep in dependencies
        ],
        "blocked_dependencies": [
            {
                "id": dep["id"],
                "title": dep["title"],
                "time_minutes": dep["time_minutes"],
                "status": "archived" if dep["archived_at"] else "active",
            }
            for dep in blocked_dependencies
        ],
        "dependent_count": dependent_count,
        "dependents": dependents,
        "versions": [
            {
                "id": v["id"],
                "version_number": v["version_number"],
                "title": v["title"],
                "description_html": v["description_html"],
                "edited_at": v["edited_at"],
                "edited_by": {
                    "id": v["edited_by"],
                    "username": v["editor_username"],
                    "display_name": v["editor_display_name"],
                    "color": "#1a1a1a",
                },
            }
            for v in versions
        ],
    }


@app.head("/")
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
def me(
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return user_public(current_user(user_cookie, authorization))  # type: ignore[return-value]


@app.patch("/api/me")
def update_me(
    data: ProfileIn,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user = current_user(user_cookie, authorization)
    display_name = data.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Display name is required")
    color = normalize_color(data.color)
    with db() as conn:
        conn.execute(
            "UPDATE users SET display_name = ?, color = ? WHERE id = ?",
            (display_name, color, user["id"]),
        )
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return user_public(updated)  # type: ignore[return-value]


@app.get("/api/me/keys")
def list_api_keys(user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = current_user(user_cookie)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, prefix, created_at, last_used_at, revoked_at
            FROM api_keys
            WHERE user_id = ? AND revoked_at IS NULL
            ORDER BY created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/me/keys")
def create_api_key(data: ApiKeyIn, user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = current_user(user_cookie)
    key = f"dope_{secrets.token_urlsafe(32)}"
    prefix = key[:14]
    created_at = now_iso()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO api_keys (user_id, name, key_hash, prefix, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user["id"], data.name.strip() or "API key", hash_api_key(key), prefix, created_at),
        )
    return {"id": int(cur.lastrowid), "name": data.name.strip() or "API key", "prefix": prefix, "created_at": created_at, "key": key}


@app.delete("/api/me/keys/{key_id}")
def revoke_api_key(key_id: int, user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = current_user(user_cookie)
    with db() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="API key not found")
        conn.execute("UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (now_iso(), key_id))
    return {"ok": True}


@app.get("/api/categories")
def list_categories(
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    current_user(user_cookie, authorization)
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, color FROM categories ORDER BY position, id"
        ).fetchall()
    return [category_public(row) for row in rows]  # type: ignore[misc]


@app.post("/api/categories")
def create_category(
    data: CategoryIn,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    current_user(user_cookie, authorization)
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    color = normalize_color(data.color)
    with db() as conn:
        position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM categories").fetchone()[0]
        try:
            cur = conn.execute(
                "INSERT INTO categories (name, color, position, created_at) VALUES (?, ?, ?, ?)",
                (name, color, position, now_iso()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Category name already exists") from None
        row = conn.execute("SELECT id, name, color FROM categories WHERE id = ?", (cur.lastrowid,)).fetchone()
    return category_public(row)  # type: ignore[return-value]


@app.patch("/api/categories/{category_id}")
def update_category(
    category_id: int,
    data: CategoryIn,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    current_user(user_cookie, authorization)
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    color = normalize_color(data.color)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Category not found")
        try:
            conn.execute(
                "UPDATE categories SET name = ?, color = ? WHERE id = ?",
                (name, color, category_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Category name already exists") from None
        row = conn.execute("SELECT id, name, color FROM categories WHERE id = ?", (category_id,)).fetchone()
    return category_public(row)  # type: ignore[return-value]


@app.delete("/api/categories/{category_id}")
def delete_category(
    category_id: int,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    current_user(user_cookie, authorization)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Category not found")
        conn.execute("UPDATE dopes SET category_id = NULL WHERE category_id = ?", (category_id,))
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    return {"ok": True}


@app.get("/api/dopes")
def list_dopes(
    status: str = "active",
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    current_user(user_cookie, authorization)
    where = {
        "active": "archived_at IS NULL AND completed_at IS NULL",
        "completed": "archived_at IS NULL AND completed_at IS NOT NULL",
        "archived": "archived_at IS NOT NULL",
        "all": "1 = 1",
    }.get(status)
    if not where:
        raise HTTPException(status_code=400, detail="Bad status")
    if status == "active":
        order = """
        CASE
          WHEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes child ON child.id = dd.dope_id
            WHERE dd.depends_on_id = dopes.id
              AND child.archived_at IS NULL
              AND child.completed_at IS NULL
          ) > 0 THEN 0
          WHEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes parent ON parent.id = dd.depends_on_id
            WHERE dd.dope_id = dopes.id
              AND parent.archived_at IS NULL
          ) = 0 THEN 1
          ELSE 2
        END,
        CASE
          WHEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes child ON child.id = dd.dope_id
            WHERE dd.depends_on_id = dopes.id
              AND child.archived_at IS NULL
              AND child.completed_at IS NULL
          ) > 0 THEN -(
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes child ON child.id = dd.dope_id
            WHERE dd.depends_on_id = dopes.id
              AND child.archived_at IS NULL
              AND child.completed_at IS NULL
          )
          WHEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes parent ON parent.id = dd.depends_on_id
            WHERE dd.dope_id = dopes.id
              AND parent.archived_at IS NULL
          ) > 0 THEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes parent ON parent.id = dd.depends_on_id
            WHERE dd.dope_id = dopes.id
              AND parent.archived_at IS NULL
          )
          ELSE 0
        END,
        title COLLATE NOCASE ASC,
        id DESC
        """
    else:
        order = "completed_at DESC, id DESC" if status == "completed" else "id DESC"
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM dopes WHERE {where} ORDER BY {order}").fetchall()
        payloads = [dope_payload(row, conn) for row in rows]
    if status == "active":
        def active_sort_key(item: dict[str, Any]) -> tuple[int, int, str, int]:
            dependency_count = len([dep for dep in item["dependencies"] if dep["status"] != "archived"])
            if item["dependent_count"]:
                return (0, -int(item["dependent_count"]), item["title"].lower(), -int(item["id"]))
            if dependency_count == 0:
                return (1, 0, item["title"].lower(), -int(item["id"]))
            return (2, dependency_count, item["title"].lower(), -int(item["id"]))

        payloads.sort(key=active_sort_key)
    return payloads


@app.get("/api/stats/progress")
def progress_stats(
    days: int = 7,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    current_user(user_cookie, authorization)
    if days not in {7, 14, 30}:
        raise HTTPException(status_code=400, detail="Progress range must be 7, 14, or 30 days")

    today = current_dope_day()
    first_day = today - timedelta(days=days - 1)
    start_utc = datetime.combine(first_day, DOPE_DAY_RESET, tzinfo=timezone.utc) - IST_OFFSET
    buckets: dict[date, dict[int, dict[str, Any]]] = {first_day + timedelta(days=i): {} for i in range(days)}

    with db() as conn:
        rows = conn.execute(
            """
            SELECT d.time_minutes, d.completed_at, u.id AS user_id, u.display_name, u.color
            FROM dopes d
            JOIN users u ON u.id = d.completed_by
            WHERE d.archived_at IS NULL
              AND d.completed_at IS NOT NULL
              AND d.completed_at >= ?
            ORDER BY d.completed_at ASC
            """,
            (start_utc.isoformat(timespec="seconds"),),
        ).fetchall()

    for row in rows:
        day = dope_day_for(row["completed_at"])
        if day not in buckets:
            continue
        stack = buckets[day].setdefault(
            row["user_id"],
            {
                "user_id": row["user_id"],
                "display_name": row["display_name"],
                "color": normalize_color(row["color"]),
                "minutes": 0,
                "count": 0,
            },
        )
        stack["minutes"] += int(row["time_minutes"])
        stack["count"] += 1

    payload = []
    for day in sorted(buckets):
        stacks = sorted(buckets[day].values(), key=lambda item: (-item["minutes"], item["display_name"].lower()))
        total_minutes = sum(item["minutes"] for item in stacks)
        payload.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%b %-d") if os.name != "nt" else day.strftime("%b %#d"),
                "total_minutes": total_minutes,
                "stacks": stacks,
            }
        )
    return payload


@app.post("/api/dopes")
def create_dope(
    data: DopeIn,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user = current_user(user_cookie, authorization)
    minutes = parse_time_to_minutes(data.time_text)
    with db() as conn:
        category_id = validate_category_id(conn, data.category_id)
        cur = conn.execute(
            """
            INSERT INTO dopes (title, description_html, time_minutes, created_by, created_at, category_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (data.title.strip(), data.description_html, minutes, user["id"], now_iso(), category_id),
        )
        dope_id = int(cur.lastrowid)
        set_dope_dependencies(conn, dope_id, data.dependency_ids)
        add_dope_version(conn, dope_id, data.title.strip(), data.description_html, user["id"], now_iso())
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dope_payload(row, conn)


@app.put("/api/dopes/{dope_id}")
def edit_dope(
    dope_id: int,
    data: DopeEditIn,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user = current_user(user_cookie, authorization)
    title = data.title.strip()
    edited_at = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"]:
            raise HTTPException(status_code=404, detail="Editable dope not found")
        category_id = validate_category_id(conn, data.category_id)
        if row["title"] != title or row["description_html"] != data.description_html:
            conn.execute(
                "UPDATE dopes SET title = ?, description_html = ? WHERE id = ?",
                (title, data.description_html, dope_id),
            )
            add_dope_version(conn, dope_id, title, data.description_html, user["id"], edited_at)
        if row["category_id"] != category_id:
            conn.execute("UPDATE dopes SET category_id = ? WHERE id = ?", (category_id, dope_id))
        set_dope_dependencies(conn, dope_id, data.dependency_ids)
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)


@app.post("/api/dopes/{dope_id}/assign")
def assign_dope(
    dope_id: int,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user = current_user(user_cookie, authorization)
    assigned_at = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["completed_at"]:
            raise HTTPException(status_code=404, detail="Active dope not found")
        if incomplete_dependency_rows(conn, dope_id):
            raise HTTPException(status_code=400, detail="Dependencies Undoped")
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
def unassign_dope(
    dope_id: int,
    data: UnassignIn,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    current_user(user_cookie, authorization)
    unassigned_at = now_iso()
    reason = data.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Reason is required")
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or not row["assigned_to"] or row["archived_at"] or row["completed_at"]:
            raise HTTPException(status_code=400, detail="Dope is not assigned")
        conn.execute("UPDATE dopes SET assigned_to = NULL, assigned_at = NULL WHERE id = ?", (dope_id,))
        conn.execute(
            """
            UPDATE assignment_history SET unassigned_at = ?, unassign_reason = ?
            WHERE id = (
              SELECT id FROM assignment_history
              WHERE dope_id = ? AND unassigned_at IS NULL
              ORDER BY id DESC LIMIT 1
            )
            """,
            (unassigned_at, reason, dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)


@app.post("/api/dopes/{dope_id}/complete")
def complete_dope(
    dope_id: int,
    data: CompleteIn,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user = current_user(user_cookie, authorization)
    completion_text = data.completion_text.strip() if data.completion_text is not None else data.completion_description.strip()
    raw_links = extract_http_links(completion_text) if data.completion_text is not None else data.commit_links
    clean_links = []
    for link in raw_links:
        url = link.strip()
        if not re.match(r"^https?://", url):
            raise HTTPException(status_code=400, detail="Commit links must start with http:// or https://")
        clean_links.append(url)
    if not clean_links:
        raise HTTPException(status_code=400, detail="Add at least one commit link")
    completed_at = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["completed_at"]:
            raise HTTPException(status_code=404, detail="Active dope not found")
        if incomplete_dependency_rows(conn, dope_id):
            raise HTTPException(status_code=400, detail="Dependencies Undoped")
        conn.execute(
            """
            UPDATE dopes
            SET completed_by = ?, completed_at = ?, completion_description = ?, assigned_to = NULL, assigned_at = NULL
            WHERE id = ?
            """,
            (user["id"], completed_at, completion_text, dope_id),
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


@app.patch("/api/dopes/{dope_id}/category")
def set_dope_category(
    dope_id: int,
    data: DopeCategoryIn,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    current_user(user_cookie, authorization)
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dope not found")
        category_id = validate_category_id(conn, data.category_id)
        conn.execute("UPDATE dopes SET category_id = ? WHERE id = ?", (category_id, dope_id))
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)


@app.post("/api/dopes/{dope_id}/archive")
def archive_dope(
    dope_id: int,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user = current_user(user_cookie, authorization)
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
def restore_dope(
    dope_id: int,
    user_cookie: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    current_user(user_cookie, authorization)
    with db() as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dope not found")
        conn.execute("UPDATE dopes SET archived_by = NULL, archived_at = NULL WHERE id = ?", (dope_id,))
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn)
