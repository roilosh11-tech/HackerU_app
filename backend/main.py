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
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional

import jwt
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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

# Signups are OPEN by default: students may create an account, and every new account
# lands status='pending' until the admin approves it. Kill switch: SIGNUP_CLOSED=1.
# (The legacy ALLOW_SIGNUP=0 var is still honoured, but only if SIGNUP_CLOSED is unset
# and it is explicitly set — so a stale deploy env can be cleared by removing it.)
ALLOW_SIGNUP = not (
    os.environ.get("SIGNUP_CLOSED", "0") == "1"
    or os.environ.get("ALLOW_SIGNUP", "1") == "0"
)

# Google accounts that are ALWAYS admins (comma-separated emails). Signing in with one
# of these Google accounts promotes it to role='admin' + status='approved' automatically,
# so the teacher can reach the Admin screen through Google like everyone else.
ADMIN_EMAILS = set(
    e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()
)


def is_admin_email(email: str) -> bool:
    return bool(email) and email.strip().lower() in ADMIN_EMAILS


# Google-only mode (DEFAULT ON): students may sign in ONLY via their Google account.
# Username/password login and password registration are refused for non-admins (the
# admin can still use a password so the Admin screen stays reachable). The one-time
# Google "claim" flow is unaffected — students still link an older account once to
# migrate their progress. Set GOOGLE_ONLY=0 to re-open password accounts.
GOOGLE_ONLY = os.environ.get("GOOGLE_ONLY", "1") != "0"

# Anthropic (for the "שאל את הקורס" chat). Set ANTHROPIC_API_KEY in the deploy env to
# enable the chat in production. If empty, /api/chat returns 503 and the app shows a
# graceful "chat unavailable" message.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# Per-user usage caps for the LLM chat, so one student can't run up the Anthropic
# bill. One unit = one successful chat request. 0 disables that particular limit.
# Admins are exempt. Counts reset daily / at the start of each calendar month (UTC).
CHAT_DAILY_LIMIT = int(os.environ.get("CHAT_DAILY_LIMIT", "25"))
CHAT_MONTHLY_LIMIT = int(os.environ.get("CHAT_MONTHLY_LIMIT", "250"))

# Google Sign-In. Set GOOGLE_CLIENT_ID (the OAuth 2.0 Web client ID from Google
# Cloud Console) to enable "התחברות עם Google". If empty, the Google button is
# hidden and only username/password auth is available. GOOGLE_CLIENT_SECRET is
# required for the authorization-code flow (server-side code→token exchange).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


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
            email         TEXT DEFAULT '',           -- Google email (once linked); may be empty
            google_sub    TEXT DEFAULT '',           -- Google account id ('sub' claim); '' = not linked
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

        -- One row per user per UTC day; count = successful chat requests that day.
        -- Daily usage = today's row; monthly usage = SUM over the 'YYYY-MM-' prefix.
        CREATE TABLE IF NOT EXISTS chat_usage (
            user_id INTEGER NOT NULL,
            day     TEXT NOT NULL,          -- 'YYYY-MM-DD' (UTC)
            count   INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        );
        """
    )
    # --- migrations for databases created before Google Sign-In existed -------
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "email" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
    if "google_sub" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT DEFAULT ''")
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


def verify_google_credential(credential: str) -> dict:
    """Verify a Google Identity Services ID token and return its claims.

    Uses Google's tokeninfo endpoint, which validates the signature and expiry
    server-side (no extra crypto deps needed). Raises HTTPException on any problem.
    Returns a dict with at least sub / email / email_verified / name / picture.
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "google sign-in not configured")
    if not credential:
        raise HTTPException(400, "missing credential")
    try:
        url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(credential)
        with urllib.request.urlopen(url, timeout=10) as resp:
            claims = json.loads(resp.read().decode("utf-8"))
    except Exception:
        raise HTTPException(401, "could not verify google token")
    if claims.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(401, "google token audience mismatch")
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "google token issuer mismatch")
    if not claims.get("sub"):
        raise HTTPException(401, "google token missing sub")
    return claims


def verify_google_access_token(access_token: str) -> dict:
    """Verify a Google OAuth access token (from the token-client popup) and return
    its claims. tokeninfo validates the token and tells us which client it was
    minted for (aud/azp) plus the user's sub/email."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "google sign-in not configured")
    if not access_token:
        raise HTTPException(400, "missing access token")
    try:
        url = "https://oauth2.googleapis.com/tokeninfo?access_token=" + urllib.parse.quote(access_token)
        with urllib.request.urlopen(url, timeout=10) as resp:
            claims = json.loads(resp.read().decode("utf-8"))
    except Exception:
        raise HTTPException(401, "could not verify google token")
    if claims.get("aud") != GOOGLE_CLIENT_ID and claims.get("azp") != GOOGLE_CLIENT_ID:
        raise HTTPException(401, "google token audience mismatch")
    if not claims.get("sub"):
        raise HTTPException(401, "google token missing sub")
    return claims


def resolve_google_identity(credential=None, access_token=None, code=None, redirect_uri=None, link_token=None) -> dict:
    """Accept a GIS ID token, an OAuth access token, an authorization code (exchanged
    server-side), or a short-lived link ticket, and return normalized claims."""
    if link_token:
        return read_glink(link_token)
    if code:
        return exchange_google_code(code, redirect_uri or "")
    if credential:
        return verify_google_credential(credential)
    if access_token:
        return verify_google_access_token(access_token)
    raise HTTPException(400, "missing google credential")


def exchange_google_code(code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for tokens (server-side, using the client
    secret), then return the verified identity claims."""
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        raise HTTPException(503, "google sign-in not configured")
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    try:
        req_obj = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req_obj, timeout=10) as resp:
            tok = json.loads(resp.read().decode("utf-8"))
    except Exception:
        raise HTTPException(401, "could not exchange google code")
    if tok.get("id_token"):
        return verify_google_credential(tok["id_token"])
    if tok.get("access_token"):
        return verify_google_access_token(tok["access_token"])
    raise HTTPException(401, "google token missing")


def make_glink(claims: dict) -> str:
    """Short-lived signed ticket proving a verified Google identity, handed to the
    client so the follow-up claim/create call needn't re-exchange the (single-use) code."""
    payload = {
        "gsub": claims["sub"],
        "gemail": (claims.get("email") or "").strip().lower(),
        "gname": claims.get("name") or "",
        "typ": "glink",
        "exp": dt.datetime.utcnow() + dt.timedelta(minutes=15),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def read_glink(token: str) -> dict:
    try:
        p = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        raise HTTPException(401, "link session expired")
    if p.get("typ") != "glink":
        raise HTTPException(401, "bad link token")
    return {"sub": p["gsub"], "email": p.get("gemail", ""),
            "email_verified": "true", "name": p.get("gname", "")}


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


# Force revalidation of the app shell (HTML/JS/manifest) so a new deploy is picked
# up immediately instead of a stale browser-cached copy. Hashed assets could be
# cached long-term, but here we keep it simple and just no-cache the shell.
@app.middleware("http")
async def no_cache_app_shell(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".dc.html", ".html", ".js", ".webmanifest")):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


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


class GoogleAuthIn(BaseModel):
    credential: Optional[str] = None
    accessToken: Optional[str] = None
    code: Optional[str] = None
    redirectUri: Optional[str] = None


class GoogleClaimIn(BaseModel):
    credential: Optional[str] = None
    accessToken: Optional[str] = None
    linkToken: Optional[str] = None
    username: str
    password: str


class GoogleCreateIn(BaseModel):
    credential: Optional[str] = None
    accessToken: Optional[str] = None
    linkToken: Optional[str] = None
    displayName: str
    avatar: Optional[str] = ""


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


class ChatIn(BaseModel):
    messages: list
    system: Optional[str] = ""
    max_tokens: Optional[int] = 2000
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/register")
def register(body: RegisterIn):
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "signups are closed")
    if GOOGLE_ONLY:
        raise HTTPException(403, "ההרשמה לקורס היא דרך חשבון Google בלבד")
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
    if GOOGLE_ONLY and row["role"] != "admin":
        conn.close()
        raise HTTPException(403, "התחברות לתלמידים היא דרך חשבון Google בלבד")
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


def _google_status_gate(row):
    if row["status"] == "pending":
        raise HTTPException(403, "החשבון עדיין ממתין לאישור המנהל/ת")
    if row["status"] == "rejected":
        raise HTTPException(403, "החשבון נדחה. פנה/י למנהל/ת")


@app.post("/api/auth/google")
def google_auth(body: GoogleAuthIn):
    """Sign in with a Google ID token.

    Links by google_sub (returning user) or by verified email (auto-link to an
    existing password account). If no account matches, returns status='unlinked'
    so the front-end can offer the one-time claim step or a new-account form.
    """
    claims = resolve_google_identity(body.credential, body.accessToken, body.code, body.redirectUri)
    sub = claims["sub"]
    email = (claims.get("email") or "").strip().lower()
    email_verified = str(claims.get("email_verified")).lower() == "true"
    name = claims.get("name") or ""
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
    if row is None and email and email_verified:
        cand = conn.execute(
            "SELECT * FROM users WHERE email=? AND (google_sub='' OR google_sub IS NULL)",
            (email,),
        ).fetchone()
        if cand is not None:
            conn.execute("UPDATE users SET google_sub=? WHERE id=?", (sub, cand["id"]))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id=?", (cand["id"],)).fetchone()
    if row is None:
        conn.close()
        return {"status": "unlinked", "email": email, "name": name, "linkToken": make_glink(claims)}
    if is_admin_email(email):
        conn.execute("UPDATE users SET role='admin', status='approved' WHERE id=?", (row["id"],))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    try:
        _google_status_gate(row)
    except HTTPException:
        conn.close()
        raise
    conn.execute("UPDATE users SET last_active=? WHERE id=?", (time.time(), row["id"]))
    conn.commit()
    conn.close()
    return {"token": make_token(row["id"]), "user": user_public(row)}


@app.post("/api/auth/google/claim")
def google_claim(body: GoogleClaimIn):
    """One-time link: prove ownership of an existing account with its old
    username+password, then bind this Google account to it (progress preserved)."""
    claims = resolve_google_identity(body.credential, body.accessToken, None, None, body.linkToken)
    sub = claims["sub"]
    email = (claims.get("email") or "").strip().lower()
    uname = body.username.strip().lower()
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone()
    if row is None or not verify_pw(body.password, row["password_hash"]):
        conn.close()
        raise HTTPException(401, "שם משתמש או סיסמה שגויים")
    other = conn.execute(
        "SELECT id FROM users WHERE google_sub=? AND id<>?", (sub, row["id"])
    ).fetchone()
    if other is not None:
        conn.close()
        raise HTTPException(409, "חשבון Google זה כבר מקושר למשתמש אחר")
    conn.execute(
        "UPDATE users SET google_sub=?, email=? WHERE id=?",
        (sub, email or row["email"], row["id"]),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    try:
        _google_status_gate(row)
    except HTTPException:
        conn.close()
        raise
    conn.execute("UPDATE users SET last_active=? WHERE id=?", (time.time(), row["id"]))
    conn.commit()
    conn.close()
    return {"token": make_token(row["id"]), "user": user_public(row)}


@app.post("/api/auth/google/create")
def google_create(body: GoogleCreateIn):
    """Brand-new student signing up via Google (no prior account). Lands pending,
    exactly like a password registration — admin approves before first entry."""
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "signups are closed")
    claims = resolve_google_identity(body.credential, body.accessToken, None, None, body.linkToken)
    sub = claims["sub"]
    email = (claims.get("email") or "").strip().lower()
    dn = (body.displayName or claims.get("name") or "").strip()
    if not dn:
        raise HTTPException(400, "invalid fields")
    conn = db()
    if conn.execute("SELECT id FROM users WHERE google_sub=?", (sub,)).fetchone():
        conn.close()
        raise HTTPException(409, "חשבון Google זה כבר רשום")
    base = (email.split("@")[0] if email else "user").lower()
    base = "".join(ch for ch in base if ch.isalnum() or ch in "._-") or "user"
    if len(base) < 3:
        base = base + "123"
    uname = base
    i = 1
    while conn.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone():
        i += 1
        uname = base + str(i)
    now = time.time()
    conn.execute(
        """INSERT INTO users (username, display_name, password_hash, avatar, email, google_sub,
           role, status, created_at, updated_at, last_active)
           VALUES (?,?,?,?,?,?, ?, ?, ?,?,?)""",
        (uname, dn, "google-only$" + secrets.token_hex(8), body.avatar or "", email, sub,
         "admin" if is_admin_email(email) else "student",
         "approved" if is_admin_email(email) else "pending", now, now, now),
    )
    conn.commit()
    if is_admin_email(email):
        row = conn.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
        conn.close()
        return {"token": make_token(row["id"]), "user": user_public(row)}
    conn.close()
    return {"ok": True, "status": "pending", "message": "החשבון נוצר וממתין לאישור המנהל/ת."}


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
_RECALL_OK = {"good", "easy"}
_RECALL_FAIL = {"again", "hard"}


def learning_metrics(data: dict) -> dict:
    """Derive review / recall / weak-spot metrics from a user's saved data blob.

    Reads `retLog` (list of {t, r, lesson, tag} recall events; `t` is ms since epoch,
    `r` is one of again/hard/good/easy). No DB schema change — computed on read.
    """
    retlog = data.get("retLog")
    if not isinstance(retlog, list):
        retlog = []
    now_ms = time.time() * 1000
    cutoff = now_ms - 30 * 86400 * 1000  # last 30 days
    total = ok = recent_total = recent_ok = 0
    per_lesson = {}
    last_review = 0
    day_set = set()
    for e in retlog:
        if not isinstance(e, dict):
            continue
        r = e.get("r")
        t = e.get("t") or 0
        if t:
            day_set.add(int(t // 86400000))
        if r not in _RECALL_OK and r not in _RECALL_FAIL:
            continue
        good = r in _RECALL_OK
        total += 1
        if good:
            ok += 1
        if t > last_review:
            last_review = t
        if t >= cutoff:
            recent_total += 1
            if good:
                recent_ok += 1
        ls = e.get("lesson")
        if ls is not None:
            d = per_lesson.setdefault(str(ls), {"lesson": str(ls), "total": 0, "fails": 0})
            d["total"] += 1
            if not good:
                d["fails"] += 1
    weak = [d for d in per_lesson.values() if d["fails"] > 0]
    weak.sort(key=lambda d: (d["fails"], d["fails"] / max(1, d["total"])), reverse=True)
    # usage: distinct active days (review-history days ∪ logged open-days) + total opens
    for d in (data.get("activeDays") or []):
        try:
            day_set.add(int(d))
        except (TypeError, ValueError):
            pass
    day_set.discard(0)
    today_idx = int(now_ms // 86400000)
    active_days = len(day_set)
    active_days_30 = len([d for d in day_set if d >= today_idx - 30])
    return {
        "recallPct": round(ok / total * 100) if total else None,
        "recallRecentPct": round(recent_ok / recent_total * 100) if recent_total else None,
        "recallRecentN": recent_total,
        "reviewsLogged": total,
        "lastReview": (last_review / 1000) if last_review else None,  # → seconds, like lastActive
        "weakLessons": weak[:3],
        "opens": int(data.get("opens") or 0),
        "activeDays": active_days,
        "activeDays30": active_days_30,
    }


@app.get("/api/admin/users")
def admin_users(request: Request):
    require_admin(request)
    conn = db()
    rows = conn.execute("SELECT * FROM users ORDER BY status='pending' DESC, created_at DESC").fetchall()
    out = []
    for r in rows:
        u = user_public(r)
        u["createdAt"] = r["created_at"]
        u["lastActive"] = r["last_active"]
        data = json.loads(r["data_json"] or "{}")
        u["notes"] = data.get("notes", {})
        u.update(learning_metrics(data))
        ct, cm, _ = chat_usage_counts(conn, r["id"])
        u["chatToday"] = ct
        u["chatMonth"] = cm
        u["reviewsLifetime"] = data.get("reviewsTotal") or u["reviewsLogged"]
        u["lessonStatus"] = {k: v for k, v in (data.get("statusOverride") or {}).items() if v}
        out.append(u)
    conn.close()
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


@app.get("/api/auth/google/start")
def google_start(request: Request):
    """Server-side redirect into Google's OAuth (authorization-code flow). The
    Google button is just a link here — the server builds the URL so nothing can
    go wrong client-side. Google returns to /Login.dc.html?code=… afterwards."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "google sign-in not configured")
    host = request.headers.get("host", "")
    redirect_uri = "https://" + host + "/Login.dc.html"
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "prompt": "select_account",
        "access_type": "online",
        "include_granted_scopes": "true",
    })
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + params, status_code=302)


@app.get("/api/health")
def health():
    return {"ok": True, "signupOpen": ALLOW_SIGNUP, "chat": bool(ANTHROPIC_API_KEY),
            "googleClientId": GOOGLE_CLIENT_ID or None, "googleOnly": GOOGLE_ONLY,
            "chatDailyLimit": CHAT_DAILY_LIMIT, "chatMonthlyLimit": CHAT_MONTHLY_LIMIT}


# ---------------------------------------------------------------------------
# LLM chat proxy (שאל את הקורס)
# ---------------------------------------------------------------------------
def chat_usage_counts(conn, uid):
    """Return (used_today, used_this_month, today_str) for a user's chat quota."""
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    drow = conn.execute(
        "SELECT count FROM chat_usage WHERE user_id=? AND day=?", (uid, today)
    ).fetchone()
    mrow = conn.execute(
        "SELECT COALESCE(SUM(count),0) AS c FROM chat_usage WHERE user_id=? AND day LIKE ?",
        (uid, today[:7] + "-%"),
    ).fetchone()
    return (drow["count"] if drow else 0), (mrow["c"] if mrow else 0), today


def chat_usage_bump(conn, uid, day):
    conn.execute(
        "INSERT INTO chat_usage (user_id, day, count) VALUES (?,?,1) "
        "ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1",
        (uid, day),
    )
    conn.commit()

# The front-end sends {system, messages, max_tokens}. We forward to Anthropic with
# the server-side key and return {text}. Auth-gated so only approved students can use it.
@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    user = current_user(request)  # must be logged in + approved
    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "chat not configured")

    # Per-user usage caps (admins exempt) so one student can't run up the bill.
    conn = db()
    used_today, used_month, today = chat_usage_counts(conn, user["id"])
    conn.close()
    if user["role"] != "admin":
        if CHAT_DAILY_LIMIT and used_today >= CHAT_DAILY_LIMIT:
            raise HTTPException(
                429,
                f"\u05d4\u05d2\u05e2\u05ea \u05dc\u05de\u05db\u05e1\u05ea \u05d4\u05e9\u05d0\u05dc\u05d5\u05ea \u05d4\u05d9\u05d5\u05de\u05d9\u05ea ({CHAT_DAILY_LIMIT} \u05e9\u05d0\u05dc\u05d5\u05ea). \u05e0\u05e1\u05d4/\u05d9 \u05e9\u05d5\u05d1 \u05de\u05d7\u05e8.",
            )
        if CHAT_MONTHLY_LIMIT and used_month >= CHAT_MONTHLY_LIMIT:
            raise HTTPException(
                429,
                f"\u05d4\u05d2\u05e2\u05ea \u05dc\u05de\u05db\u05e1\u05ea \u05d4\u05e9\u05d0\u05dc\u05d5\u05ea \u05d4\u05d7\u05d5\u05d3\u05e9\u05d9\u05ea ({CHAT_MONTHLY_LIMIT} \u05e9\u05d0\u05dc\u05d5\u05ea). \u05d4\u05de\u05db\u05e1\u05d4 \u05de\u05ea\u05d0\u05e4\u05e1\u05ea \u05d1\u05ea\u05d7\u05d9\u05dc\u05ea \u05d4\u05d7\u05d5\u05d3\u05e9.",
            )

    # Whitelist message shape: [{role: 'user'|'assistant', content: str}, ...]
    msgs = []
    for m in (body.messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            msgs.append({"role": role, "content": content})
    if not msgs:
        raise HTTPException(400, "no messages")

    payload = {
        "model": body.model or ANTHROPIC_MODEL,
        "max_tokens": max(64, min(int(body.max_tokens or 2000), 4096)),
        "messages": msgs,
    }
    if body.system:
        payload["system"] = body.system

    req_obj = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req_obj, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise HTTPException(502, "llm error: " + str(e.code) + " " + detail)
    except Exception as e:
        raise HTTPException(502, "llm unreachable")

    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )
    # Count this successful request against the user's quota.
    conn = db()
    chat_usage_bump(conn, user["id"], today)
    conn.close()
    return {
        "text": text,
        "usage": {
            "dailyUsed": used_today + 1, "dailyLimit": CHAT_DAILY_LIMIT,
            "monthlyUsed": used_month + 1, "monthlyLimit": CHAT_MONTHLY_LIMIT,
        },
    }


# ---------------------------------------------------------------------------
# Static front-end
# ---------------------------------------------------------------------------
# Entry point is the login page. Everything else (the .dc.html app, support.js,
# course-data.js, api.js, assets/) is served as-is from STATIC_DIR.
@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "Login.dc.html"))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
