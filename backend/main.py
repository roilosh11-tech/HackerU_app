"""
מוח הקורס (Course Brain) — backend API.

FastAPI + SQLite. Serves the static front-end (the .dc.html app) AND a JSON API
for auth, per-user data, gamification and an admin dashboard.

Deploy target: Railway. See README.md at the repo root.

Auth model: username + password. New signups land in status='pending' and must be
approved by an admin before they can log in. A first admin is seeded from env vars
ADMIN_USERNAME / ADMIN_PASSWORD on startup.

Dependencies are intentionally minimal (see requirements.txt): fastapi, uvicorn,
PyJWT. Password hashing uses the stdlib (pbkdf2_hmac) and storage uses stdlib
sqlite3 — no compiled deps, so Railway builds are fast and reliable.
"""

import os
import json
import time
import sqlite3
import hashlib
import secrets
import datetime as dt
from typing import Optional

import jwt
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# On Railway, attach a Volume mounted at /data so the SQLite file survives
# restarts and re-deploys. Locally it falls back to ./course.db.
DB_PATH = os.environ.get("DB_PATH", "/data/course.db")
if not os.path.isdir(os.path.dirname(DB_PATH) or "."):
    DB_PATH = os.path.join(os.path.dirname(__file__), "course.db")

# The web root = the folder that holds Course Brain.dc.html, support.js, etc.
# By default that is the parent of this backend/ folder.
STATIC_DIR = os.environ.get(
    "STATIC_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production-" + secrets.token_hex(8))
JWT_ALG = "HS256"
TOKEN_TTL_DAYS = 60

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # if empty, admin isn't auto-seeded

# Whether brand-new registrations are allowed at all (signup_open = "requires approval").
ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "1") != "0"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            display_name  TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            avatar        TEXT DEFAULT '',
            role          TEXT DEFAULT 'student',   -- 'student' | 'admin'
            status        TEXT DEFAULT 'pending',   -- 'pending' | 'approved' | 'rejected'
            points        INTEGER DEFAULT 0,
            completion    INTEGER DEFAULT 0,         -- % course completion
            streak        INTEGER DEFAULT 0,
            achievements  TEXT DEFAULT '[]',         -- JSON array of achievement ids
            data_json     TEXT DEFAULT '{}',         -- full per-user overlay blob
            created_at    REAL,
            updated_at    REAL,
            last_active   REAL
        );

        -- Shared project gallery (class-wide feed). One row per post.
        -- author_name / author_avatar are denormalised so a post keeps its
        -- byline even if the user later changes their profile.
        CREATE TABLE IF NOT EXISTS posts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id     INTEGER NOT NULL,
            author_name   TEXT NOT NULL,
            author_avatar TEXT DEFAULT '',
            title         TEXT NOT NULL,
            body          TEXT NOT NULL,
            link          TEXT DEFAULT '',
            kudos_json    TEXT DEFAULT '[]',          -- JSON array of user ids who gave 👏
            created_at    REAL
        );

        CREATE TABLE IF NOT EXISTS post_comments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id       INTEGER NOT NULL,
            author_id     INTEGER NOT NULL,
            author_name   TEXT NOT NULL,
            author_avatar TEXT DEFAULT '',
            text          TEXT NOT NULL,
            created_at    REAL
        );
        CREATE INDEX IF NOT EXISTS idx_comments_post ON post_comments(post_id);
        """
    )
    conn.commit()
    conn.close()
    seed_admin()


def seed_admin():
    if not ADMIN_PASSWORD:
        return
    # Usernames are always stored + matched in lowercase (login lowercases input),
    # so normalise here too — otherwise a capitalised ADMIN_USERNAME would create an
    # admin the login screen can never reach.
    admin_uname = ADMIN_USERNAME.strip().lower()
    conn = db()
    row = conn.execute("SELECT id FROM users WHERE username=?", (admin_uname,)).fetchone()
    now = time.time()
    if row is None:
        conn.execute(
            """INSERT INTO users (username, display_name, password_hash, role, status,
               avatar, created_at, updated_at, last_active)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (admin_uname, "מנהל/ת", hash_pw(ADMIN_PASSWORD), "admin", "approved",
             "sun", now, now, now),
        )
    else:
        # promote/repair an existing account with this username: make it an approved
        # admin and sync its password to the env var.
        conn.execute(
            "UPDATE users SET password_hash=?, role='admin', status='approved' WHERE username=?",
            (hash_pw(ADMIN_PASSWORD), admin_uname),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Password + token helpers
# ---------------------------------------------------------------------------
def hash_pw(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return "pbkdf2$120000$" + salt.hex() + "$" + dk.hex()


def verify_pw(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def make_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": dt.datetime.utcnow() + dt.timedelta(days=TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def user_public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "displayName": row["display_name"],
        "avatar": row["avatar"],
        "role": row["role"],
        "status": row["status"],
        "points": row["points"],
        "completion": row["completion"],
        "streak": row["streak"],
        "achievements": json.loads(row["achievements"] or "[]"),
    }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="מוח הקורס API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()


def current_user(request: Request) -> sqlite3.Row:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        raise HTTPException(401, "invalid token")
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (payload["sub"],)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(401, "user not found")
    if row["status"] != "approved":
        raise HTTPException(403, "account not approved")
    return row


def require_admin(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "admin only")
    return user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class RegisterIn(BaseModel):
    username: str
    displayName: str
    password: str
    avatar: Optional[str] = ""


class LoginIn(BaseModel):
    username: str
    password: str


class ProfileIn(BaseModel):
    displayName: Optional[str] = None
    avatar: Optional[str] = None


class DataIn(BaseModel):
    data: dict
    points: Optional[int] = None
    completion: Optional[int] = None
    streak: Optional[int] = None
    achievements: Optional[list] = None


class PostIn(BaseModel):
    title: str
    body: str
    link: Optional[str] = ""


class CommentIn(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/register")
def register(body: RegisterIn):
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "signups are closed")
    uname = body.username.strip().lower()
    if len(uname) < 3 or len(body.password) < 4 or not body.displayName.strip():
        raise HTTPException(400, "invalid fields")
    conn = db()
    if conn.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone():
        conn.close()
        raise HTTPException(409, "username taken")
    now = time.time()
    conn.execute(
        """INSERT INTO users (username, display_name, password_hash, avatar, role, status,
           created_at, updated_at, last_active)
           VALUES (?,?,?,?, 'student', 'pending', ?,?,?)""",
        (uname, body.displayName.strip(), hash_pw(body.password), body.avatar or "", now, now, now),
    )
    conn.commit()
    conn.close()
    # No token — must be approved first.
    return {"ok": True, "status": "pending",
            "message": "החשבון נוצר וממתין לאישור המנהל/ת."}


@app.post("/api/login")
def login(body: LoginIn):
    uname = body.username.strip().lower()
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone()
    if row is None or not verify_pw(body.password, row["password_hash"]):
        conn.close()
        raise HTTPException(401, "שם משתמש או סיסמה שגויים")
    if row["status"] == "pending":
        conn.close()
        raise HTTPException(403, "החשבון עדיין ממתין לאישור המנהל/ת")
    if row["status"] == "rejected":
        conn.close()
        raise HTTPException(403, "החשבון נדחה. פנה/י למנהל/ת")
    conn.execute("UPDATE users SET last_active=? WHERE id=?", (time.time(), row["id"]))
    conn.commit()
    conn.close()
    return {"token": make_token(row["id"]), "user": user_public(row)}


@app.get("/api/me")
def me(request: Request):
    user = current_user(request)
    out = user_public(user)
    out["data"] = json.loads(user["data_json"] or "{}")
    return out


@app.put("/api/me")
def update_profile(body: ProfileIn, request: Request):
    user = current_user(request)
    conn = db()
    dn = body.displayName.strip() if body.displayName else user["display_name"]
    av = body.avatar if body.avatar is not None else user["avatar"]
    conn.execute(
        "UPDATE users SET display_name=?, avatar=?, updated_at=? WHERE id=?",
        (dn, av, time.time(), user["id"]),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.close()
    return user_public(row)


@app.get("/api/data")
def get_data(request: Request):
    user = current_user(request)
    return {"data": json.loads(user["data_json"] or "{}"),
            "points": user["points"], "completion": user["completion"],
            "streak": user["streak"],
            "achievements": json.loads(user["achievements"] or "[]")}


@app.put("/api/data")
def put_data(body: DataIn, request: Request):
    user = current_user(request)
    conn = db()
    points = body.points if body.points is not None else user["points"]
    completion = body.completion if body.completion is not None else user["completion"]
    streak = body.streak if body.streak is not None else user["streak"]
    ach = json.dumps(body.achievements) if body.achievements is not None else user["achievements"]
    conn.execute(
        """UPDATE users SET data_json=?, points=?, completion=?, streak=?, achievements=?,
           updated_at=?, last_active=? WHERE id=?""",
        (json.dumps(body.data, ensure_ascii=False), points, completion, streak, ach,
         time.time(), time.time(), user["id"]),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Project gallery (shared class-wide feed)
# ---------------------------------------------------------------------------
def _ms(t):
    return int((t or 0) * 1000)


def post_public(conn, row, viewer_id=None) -> dict:
    kudos = json.loads(row["kudos_json"] or "[]")
    crows = conn.execute(
        "SELECT * FROM post_comments WHERE post_id=? ORDER BY created_at ASC", (row["id"],)
    ).fetchall()
    comments = [{
        "id": str(c["id"]),
        "authorId": c["author_id"],
        "authorName": c["author_name"],
        "authorAvatar": c["author_avatar"],
        "text": c["text"],
        "ts": _ms(c["created_at"]),
    } for c in crows]
    return {
        "id": str(row["id"]),
        "authorId": row["author_id"],
        "authorName": row["author_name"],
        "authorAvatar": row["author_avatar"],
        "title": row["title"],
        "body": row["body"],
        "link": row["link"] or "",
        "kudos": kudos,
        "ts": _ms(row["created_at"]),
        "comments": comments,
    }


@app.get("/api/gallery")
def gallery_list(request: Request):
    user = current_user(request)
    conn = db()
    rows = conn.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    out = [post_public(conn, r, user["id"]) for r in rows]
    conn.close()
    return {"posts": out}


@app.post("/api/gallery")
def gallery_create(body: PostIn, request: Request):
    user = current_user(request)
    title = (body.title or "").strip()
    text = (body.body or "").strip()
    if not title or not text:
        raise HTTPException(400, "title and body required")
    conn = db()
    now = time.time()
    cur = conn.execute(
        """INSERT INTO posts (author_id, author_name, author_avatar, title, body, link,
           kudos_json, created_at) VALUES (?,?,?,?,?,?, '[]', ?)""",
        (user["id"], user["display_name"], user["avatar"] or "",
         title[:200], text[:4000], (body.link or "").strip()[:600], now),
    )
    pid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    out = post_public(conn, row, user["id"])
    conn.close()
    return out


@app.delete("/api/gallery/{pid}")
def gallery_delete(pid: int, request: Request):
    user = current_user(request)
    conn = db()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, "not found")
    if row["author_id"] != user["id"] and user["role"] != "admin":
        conn.close()
        raise HTTPException(403, "not allowed")
    conn.execute("DELETE FROM post_comments WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM posts WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/gallery/{pid}/kudos")
def gallery_kudos(pid: int, request: Request):
    user = current_user(request)
    conn = db()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, "not found")
    kudos = json.loads(row["kudos_json"] or "[]")
    if user["id"] in kudos:
        kudos = [k for k in kudos if k != user["id"]]
    else:
        kudos.append(user["id"])
    conn.execute("UPDATE posts SET kudos_json=? WHERE id=?", (json.dumps(kudos), pid))
    conn.commit()
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    out = post_public(conn, row, user["id"])
    conn.close()
    return out


@app.post("/api/gallery/{pid}/comments")
def gallery_comment(pid: int, body: CommentIn, request: Request):
    user = current_user(request)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "empty comment")
    conn = db()
    row = conn.execute("SELECT id FROM posts WHERE id=?", (pid,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, "not found")
    conn.execute(
        """INSERT INTO post_comments (post_id, author_id, author_name, author_avatar, text, created_at)
           VALUES (?,?,?,?,?,?)""",
        (pid, user["id"], user["display_name"], user["avatar"] or "", text[:1000], time.time()),
    )
    conn.commit()
    prow = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    out = post_public(conn, prow, user["id"])
    conn.close()
    return out


@app.delete("/api/gallery/{pid}/comments/{cid}")
def gallery_comment_delete(pid: int, cid: int, request: Request):
    user = current_user(request)
    conn = db()
    crow = conn.execute("SELECT * FROM post_comments WHERE id=? AND post_id=?", (cid, pid)).fetchone()
    if crow is None:
        conn.close()
        raise HTTPException(404, "not found")
    if crow["author_id"] != user["id"] and user["role"] != "admin":
        conn.close()
        raise HTTPException(403, "not allowed")
    conn.execute("DELETE FROM post_comments WHERE id=?", (cid,))
    conn.commit()
    prow = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    out = post_public(conn, prow, user["id"])
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------
@app.get("/api/admin/users")
def admin_users(request: Request):
    require_admin(request)
    conn = db()
    rows = conn.execute("SELECT * FROM users ORDER BY status='pending' DESC, created_at DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        u = user_public(r)
        u["createdAt"] = r["created_at"]
        u["lastActive"] = r["last_active"]
        u["notes"] = json.loads(r["data_json"] or "{}").get("notes", {})
        out.append(u)
    return {"users": out}


@app.post("/api/admin/users/{uid}/approve")
def admin_approve(uid: int, request: Request):
    require_admin(request)
    conn = db()
    conn.execute("UPDATE users SET status='approved' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/admin/users/{uid}/reject")
def admin_reject(uid: int, request: Request):
    require_admin(request)
    conn = db()
    conn.execute("UPDATE users SET status='rejected' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/admin/users/{uid}")
def admin_delete(uid: int, request: Request):
    admin = require_admin(request)
    if admin["id"] == uid:
        raise HTTPException(400, "cannot delete yourself")
    conn = db()
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "signupOpen": ALLOW_SIGNUP}


# ---------------------------------------------------------------------------
# Static front-end
# ---------------------------------------------------------------------------
# Entry point is the login page. Everything else (the .dc.html app, support.js,
# course-data.js, api.js, assets/) is served as-is from STATIC_DIR.
@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "Login.dc.html"))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
