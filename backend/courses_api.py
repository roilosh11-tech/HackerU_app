"""
Courses, memberships, join codes, AI cost accounting and backups.
Mounted onto the main app via register(app, db, current_user, current_user_any, require_admin).

Tables:
  courses     — one row per course (tenant). budget_usd = monthly AI budget for the whole course.
  memberships — (user, course, role) ; role = 'teacher' | 'student'
  llm_usage   — one row per LLM call made through the server key (chat, pipeline)
"""

import os
import json
import time
import secrets
import shutil
import threading
import datetime as dt
from typing import Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

DEFAULT_COURSE_ID = os.environ.get("DEFAULT_COURSE_ID", "ai-arch")
DEFAULT_COURSE_NAME = os.environ.get("DEFAULT_COURSE_NAME", "AI Solutions Architect")
DEFAULT_TEACHER = os.environ.get("DEFAULT_TEACHER_NAME", "רילי")
COURSE_BUDGET_USD = float(os.environ.get("COURSE_BUDGET_USD", "40"))
BACKUP_DIR = os.environ.get("BACKUP_DIR", "")
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))

# USD per 1M tokens (input, output). Unknown models fall back to Sonnet pricing.
PRICING = {
    "claude-sonnet-4-5": (3.0, 15.0), "claude-sonnet-4": (3.0, 15.0), "claude-3-7-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0), "claude-3-5-haiku": (0.8, 4.0),
    "claude-opus-4-1": (15.0, 75.0), "claude-opus-4": (15.0, 75.0),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    teacher_name TEXT DEFAULT '',
    accent       TEXT DEFAULT '#98b1ff',
    join_code    TEXT UNIQUE,
    budget_usd   REAL DEFAULT 40,
    active       INTEGER DEFAULT 1,
    created_at   REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS memberships (
    user_id    INTEGER NOT NULL,
    course_id  TEXT NOT NULL,
    role       TEXT DEFAULT 'student',      -- 'teacher' | 'student'
    created_at REAL,
    PRIMARY KEY (user_id, course_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_course ON memberships(course_id);
CREATE TABLE IF NOT EXISTS llm_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    course_id  TEXT,
    kind       TEXT DEFAULT 'chat',         -- 'chat' | 'pipeline'
    model      TEXT,
    tokens_in  INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd   REAL DEFAULT 0,
    day        TEXT,                        -- 'YYYY-MM-DD' (UTC)
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_course_day ON llm_usage(course_id, day);
"""


def price(model: str, tin: int, tout: int) -> float:
    m = (model or "").lower()
    rate = None
    for k, v in PRICING.items():
        if m.startswith(k):
            rate = v
            break
    if rate is None:
        rate = PRICING["claude-sonnet-4-5"]
    return (tin * rate[0] + tout * rate[1]) / 1_000_000.0


def new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def month_prefix(month: Optional[str]) -> str:
    if month and len(month) == 7:
        return month
    return dt.datetime.utcnow().strftime("%Y-%m")


def course_public(row, include_code=False) -> dict:
    d = {"id": row["id"], "name": row["name"], "teacherName": row["teacher_name"], "accent": row["accent"],
         "budgetUsd": row["budget_usd"], "active": bool(row["active"]), "createdAt": row["created_at"]}
    if include_code:
        d["joinCode"] = row["join_code"]
    return d


def user_courses(conn, uid: int) -> list:
    rows = conn.execute(
        "SELECT c.*, m.role AS mrole FROM memberships m JOIN courses c ON c.id=m.course_id "
        "WHERE m.user_id=? AND c.active=1 ORDER BY m.created_at", (uid,)).fetchall()
    out = []
    for r in rows:
        d = course_public(r, include_code=(r["mrole"] == "teacher"))
        d["role"] = r["mrole"]
        out.append(d)
    return out


def ensure_default_course(conn):
    row = conn.execute("SELECT id FROM courses WHERE id=?", (DEFAULT_COURSE_ID,)).fetchone()
    now = time.time()
    if row is None:
        conn.execute("INSERT INTO courses (id, name, teacher_name, join_code, budget_usd, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (DEFAULT_COURSE_ID, DEFAULT_COURSE_NAME, DEFAULT_TEACHER, new_code(), COURSE_BUDGET_USD, now, now))
    # Backfill: every approved non-admin user without any membership joins the default course.
    conn.execute(
        "INSERT OR IGNORE INTO memberships (user_id, course_id, role, created_at) "
        "SELECT u.id, ?, 'student', ? FROM users u WHERE u.status='approved' AND u.role<>'admin' "
        "AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.user_id=u.id)", (DEFAULT_COURSE_ID, now))
    conn.commit()


def join_by_code(conn, uid: int, code: str) -> dict:
    """Attach a user to the course (and cohort) owning `code`. Cohort codes take priority;
    a course-level code lands the student in the course's active cohort. Raises 404."""
    code = (code or "").strip().upper()
    co = conn.execute("SELECT c.*, k.id AS cohort_id FROM cohorts k JOIN courses c ON c.id=k.course_id "
                      "WHERE k.join_code=? AND c.active=1", (code,)).fetchone()
    if co is not None:
        row, cohort_id = co, co["cohort_id"]
    else:
        row = conn.execute("SELECT * FROM courses WHERE join_code=? AND active=1", (code,)).fetchone()
        if row is None:
            raise HTTPException(404, "קוד הקורס לא נמצא")
        k = conn.execute("SELECT id FROM cohorts WHERE course_id=? ORDER BY status='active' DESC, created_at DESC LIMIT 1", (row["id"],)).fetchone()
        cohort_id = k["id"] if k else ""
    conn.execute("INSERT INTO memberships (user_id, course_id, role, cohort_id, created_at) VALUES (?,?,'student',?,?) "
                 "ON CONFLICT(user_id, course_id) DO UPDATE SET cohort_id=excluded.cohort_id",
                 (uid, row["id"], cohort_id, time.time()))
    conn.commit()
    return dict(course_public(row), cohortId=cohort_id)


def record_usage(conn, uid, course_id, model, usage: dict, kind="chat"):
    tin = int((usage or {}).get("input_tokens") or 0)
    tout = int((usage or {}).get("output_tokens") or 0)
    now = time.time()
    conn.execute("INSERT INTO llm_usage (user_id, course_id, kind, model, tokens_in, tokens_out, cost_usd, day, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (uid, course_id, kind, model, tin, tout, price(model, tin, tout),
                  dt.datetime.utcfromtimestamp(now).strftime("%Y-%m-%d"), now))
    conn.commit()


def month_spend(conn, course_id, month=None) -> float:
    r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) AS c FROM llm_usage WHERE course_id=? AND day LIKE ?",
                     (course_id, month_prefix(month) + "-%")).fetchone()
    return float(r["c"] or 0)


def check_budget(conn, course_id):
    """Raise 429 when the course has used its monthly AI budget."""
    row = conn.execute("SELECT budget_usd FROM courses WHERE id=?", (course_id,)).fetchone()
    if row is None or not row["budget_usd"]:
        return
    if month_spend(conn, course_id) >= row["budget_usd"]:
        raise HTTPException(429, "תקציב ה-AI החודשי של הקורס נוצל. המרצה יכול/ה להגדיל אותו במסך הניהול.")


def costs_report(conn, course_id, month=None) -> dict:
    mp = month_prefix(month)
    like = mp + "-%"
    tot = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) tout, "
                       "COUNT(*) n FROM llm_usage WHERE course_id=? AND day LIKE ?", (course_id, like)).fetchone()
    per_model = conn.execute("SELECT model, COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM llm_usage WHERE course_id=? AND day LIKE ? "
                             "GROUP BY model ORDER BY c DESC", (course_id, like)).fetchall()
    per_kind = conn.execute("SELECT kind, COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM llm_usage WHERE course_id=? AND day LIKE ? "
                            "GROUP BY kind", (course_id, like)).fetchall()
    per_user = conn.execute("SELECT l.user_id, u.display_name, u.avatar, COALESCE(SUM(l.cost_usd),0) c, COUNT(*) n "
                            "FROM llm_usage l LEFT JOIN users u ON u.id=l.user_id WHERE l.course_id=? AND l.day LIKE ? "
                            "GROUP BY l.user_id ORDER BY c DESC LIMIT 10", (course_id, like)).fetchall()
    daily = conn.execute("SELECT day, COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM llm_usage WHERE course_id=? AND day LIKE ? "
                         "GROUP BY day ORDER BY day", (course_id, like)).fetchall()
    course = conn.execute("SELECT budget_usd FROM courses WHERE id=?", (course_id,)).fetchone()
    budget = float(course["budget_usd"] or 0) if course else 0
    return {"courseId": course_id, "month": mp, "budgetUsd": budget,
            "totalUsd": round(float(tot["c"] or 0), 4), "calls": tot["n"],
            "tokensIn": tot["ti"], "tokensOut": tot["tout"],
            "pct": round(float(tot["c"] or 0) / budget * 100) if budget else None,
            "perModel": [{"model": r["model"], "usd": round(r["c"], 4), "calls": r["n"]} for r in per_model],
            "perKind": [{"kind": r["kind"], "usd": round(r["c"], 4), "calls": r["n"]} for r in per_kind],
            "perUser": [{"userId": r["user_id"], "displayName": r["display_name"] or "—", "avatar": r["avatar"] or "",
                         "usd": round(r["c"], 4), "calls": r["n"]} for r in per_user],
            "daily": [{"day": r["day"], "usd": round(r["c"], 4), "calls": r["n"]} for r in daily]}


# ---------------------------------------------------------------------------
# Backups — copy the SQLite file once a day into BACKUP_DIR (default: <db dir>/backups)
# ---------------------------------------------------------------------------
def backup_now(db_path: str) -> dict:
    bdir = BACKUP_DIR or os.path.join(os.path.dirname(db_path) or ".", "backups")
    os.makedirs(bdir, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(bdir, "course-%s.db" % stamp)
    import sqlite3
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    files = sorted(f for f in os.listdir(bdir) if f.startswith("course-") and f.endswith(".db"))
    for f in files[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(bdir, f))
        except OSError:
            pass
    files = files[-BACKUP_KEEP:]
    return {"dir": bdir, "latest": os.path.basename(dest), "count": len(files),
            "files": [{"name": f, "size": os.path.getsize(os.path.join(bdir, f))} for f in files]}


def list_backups(db_path: str) -> dict:
    bdir = BACKUP_DIR or os.path.join(os.path.dirname(db_path) or ".", "backups")
    if not os.path.isdir(bdir):
        return {"dir": bdir, "count": 0, "files": []}
    files = sorted(f for f in os.listdir(bdir) if f.startswith("course-") and f.endswith(".db"))
    return {"dir": bdir, "count": len(files),
            "files": [{"name": f, "size": os.path.getsize(os.path.join(bdir, f))} for f in files]}


def start_backup_thread(db_path: str):
    def loop():
        while True:
            try:
                backup_now(db_path)
            except Exception:
                import traceback; traceback.print_exc()
            time.sleep(86400)
    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------------------
class CourseIn(BaseModel):
    id: Optional[str] = None
    name: str
    teacherName: Optional[str] = ""
    accent: Optional[str] = "#98b1ff"
    budgetUsd: Optional[float] = None


class CourseUpdateIn(BaseModel):
    name: Optional[str] = None
    teacherName: Optional[str] = None
    accent: Optional[str] = None
    budgetUsd: Optional[float] = None
    active: Optional[bool] = None


class JoinIn(BaseModel):
    code: str


class MemberIn(BaseModel):
    userId: int
    role: Optional[str] = "student"


class ImportIn(BaseModel):
    lessons: list
    flashcards: Optional[list] = []
    distractors: Optional[dict] = {}
    insights: Optional[list] = []
    replace: Optional[bool] = False


def register(app, db, current_user, current_user_any, require_admin, db_path):
    conn = db(); conn.executescript(SCHEMA); conn.commit(); conn.close()

    def membership_role(conn, uid, course_id):
        r = conn.execute("SELECT role FROM memberships WHERE user_id=? AND course_id=?", (uid, course_id)).fetchone()
        return r["role"] if r else None

    def require_teacher(request: Request, course_id: str):
        """Platform admin, or a member with role='teacher' of this course."""
        user = current_user(request)
        if user["role"] == "admin":
            return user
        conn = db(); role = membership_role(conn, user["id"], course_id); conn.close()
        if role != "teacher":
            raise HTTPException(403, "teacher only")
        return user

    def require_member(request: Request, course_id: str):
        user = current_user(request)
        if user["role"] == "admin":
            return user
        conn = db(); role = membership_role(conn, user["id"], course_id); conn.close()
        if role is None:
            raise HTTPException(403, "not enrolled in this course")
        return user

    @app.get("/api/courses")
    def list_courses(request: Request):
        user = current_user(request)
        conn = db()
        if user["role"] == "admin":
            rows = conn.execute("SELECT * FROM courses ORDER BY created_at").fetchall()
            out = []
            for r in rows:
                d = course_public(r, include_code=True)
                d["role"] = "admin"
                d["students"] = conn.execute("SELECT COUNT(*) n FROM memberships WHERE course_id=? AND role='student'", (r["id"],)).fetchone()["n"]
                d["teachers"] = conn.execute("SELECT COUNT(*) n FROM memberships WHERE course_id=? AND role='teacher'", (r["id"],)).fetchone()["n"]
                d["published"] = conn.execute("SELECT COUNT(*) n FROM lessons WHERE course_id=? AND status='published'", (r["id"],)).fetchone()["n"]
                d["spentUsd"] = round(month_spend(conn, r["id"]), 4)
                out.append(d)
        else:
            out = user_courses(conn, user["id"])
            for d in out:
                if d["role"] == "teacher":
                    d["students"] = conn.execute("SELECT COUNT(*) n FROM memberships WHERE course_id=? AND role='student'", (d["id"],)).fetchone()["n"]
                    d["spentUsd"] = round(month_spend(conn, d["id"]), 4)
        conn.close()
        return {"courses": out, "defaultCourseId": DEFAULT_COURSE_ID}

    @app.post("/api/courses")
    def create_course(body: CourseIn, request: Request):
        require_admin(request)
        cid = (body.id or "").strip().lower() or "".join(ch if ch.isalnum() else "-" for ch in body.name.lower()).strip("-")[:32]
        if not cid or not body.name.strip():
            raise HTTPException(400, "invalid fields")
        conn = db()
        if conn.execute("SELECT id FROM courses WHERE id=?", (cid,)).fetchone():
            conn.close(); raise HTTPException(409, "course id taken")
        now = time.time()
        conn.execute("INSERT INTO courses (id, name, teacher_name, accent, join_code, budget_usd, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (cid, body.name.strip(), (body.teacherName or "").strip(), body.accent or "#98b1ff", new_code(),
                      body.budgetUsd if body.budgetUsd is not None else COURSE_BUDGET_USD, now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
        # every course starts with one active cohort
        conn.execute("INSERT INTO cohorts (id, course_id, label, start, status, join_code, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (cid + "-" + dt.datetime.utcnow().strftime("%Y") + "a", cid, "מחזור א׳ " + dt.datetime.utcnow().strftime("%Y"), "", "active", new_code(), now, now))
        conn.commit()
        conn.close()
        return course_public(row, include_code=True)

    @app.put("/api/courses/{course_id}")
    def update_course(course_id: str, body: CourseUpdateIn, request: Request):
        user = require_teacher(request, course_id)
        conn = db()
        row = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if row is None:
            conn.close(); raise HTTPException(404, "course not found")
        name = body.name.strip() if body.name else row["name"]
        tn = body.teacherName.strip() if body.teacherName is not None else row["teacher_name"]
        accent = body.accent or row["accent"]
        budget = row["budget_usd"]
        active = row["active"]
        if user["role"] == "admin":  # budget + archive are admin-only
            if body.budgetUsd is not None:
                budget = body.budgetUsd
            if body.active is not None:
                active = 1 if body.active else 0
        conn.execute("UPDATE courses SET name=?, teacher_name=?, accent=?, budget_usd=?, active=?, updated_at=? WHERE id=?",
                     (name, tn, accent, budget, active, time.time(), course_id))
        conn.commit()
        row = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        conn.close()
        return course_public(row, include_code=True)

    @app.delete("/api/courses/{course_id}")
    def delete_course(course_id: str, request: Request):
        require_admin(request)
        if course_id == DEFAULT_COURSE_ID:
            raise HTTPException(400, "cannot delete the default course")
        conn = db()
        for t in ("memberships", "llm_usage", "lessons", "cohorts", "materials"):
            conn.execute("DELETE FROM %s WHERE course_id=?" % t, (course_id,))
        conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/courses/{course_id}/code/rotate")
    def rotate_code(course_id: str, request: Request):
        require_teacher(request, course_id)
        conn = db()
        code = new_code()
        conn.execute("UPDATE courses SET join_code=?, updated_at=? WHERE id=?", (code, time.time(), course_id))
        conn.commit(); conn.close()
        return {"joinCode": code}

    @app.post("/api/join")
    def join(body: JoinIn, request: Request):
        """Enrol the caller in the course owning this code. A pending account with a
        valid code is approved on the spot — the code IS the approval."""
        user = current_user_any(request)
        conn = db()
        course = join_by_code(conn, user["id"], body.code)
        if user["status"] == "pending":
            conn.execute("UPDATE users SET status='approved' WHERE id=?", (user["id"],))
            conn.commit()
        conn.close()
        return {"ok": True, "course": course}

    @app.get("/api/courses/{course_id}/members")
    def members(course_id: str, request: Request):
        require_teacher(request, course_id)
        conn = db()
        rows = conn.execute("SELECT u.id, u.username, u.display_name, u.avatar, u.status, u.points, u.completion, u.streak, "
                            "u.last_active, u.email, m.role, m.cohort_id FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.course_id=? "
                            "ORDER BY m.role DESC, u.display_name", (course_id,)).fetchall()
        conn.close()
        return {"members": [{"id": r["id"], "username": r["username"], "displayName": r["display_name"], "avatar": r["avatar"], "email": r["email"] or "",
                             "status": r["status"], "points": r["points"], "completion": r["completion"], "streak": r["streak"],
                             "lastActive": r["last_active"], "role": r["role"], "cohortId": r["cohort_id"] or ""} for r in rows]}

    @app.post("/api/courses/{course_id}/members")
    def add_member(course_id: str, body: MemberIn, request: Request):
        require_teacher(request, course_id)
        role = body.role if body.role in ("teacher", "student") else "student"
        conn = db()
        conn.execute("INSERT INTO memberships (user_id, course_id, role, created_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(user_id, course_id) DO UPDATE SET role=excluded.role", (body.userId, course_id, role, time.time()))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.delete("/api/courses/{course_id}/members/{uid}")
    def remove_member(course_id: str, uid: int, request: Request):
        require_teacher(request, course_id)
        conn = db()
        conn.execute("DELETE FROM memberships WHERE user_id=? AND course_id=?", (uid, course_id))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/courses/{course_id}/import")
    def import_content(course_id: str, body: ImportIn, request: Request):
        """Seed the DB from course-data.js: each lesson becomes a published pipeline row
        (result_json in the same shape the extraction agent produces)."""
        require_admin(request)
        conn = db()
        if conn.execute("SELECT id FROM courses WHERE id=?", (course_id,)).fetchone() is None:
            conn.close(); raise HTTPException(404, "course not found")
        now = time.time()
        n = 0
        for lesson in body.lessons or []:
            num = int(lesson.get("num") or 0)
            if not num:
                continue
            cards = [c for c in (body.flashcards or []) if int(c.get("lesson") or 0) == num]
            ids = {c.get("id") for c in cards}
            mc = {k: v for k, v in (body.distractors or {}).items() if k in ids}
            ins = [i for i in (body.insights or []) if int(i.get("lesson") or 0) == num]
            res = {"lesson": lesson, "flashcards": cards, "distractors": mc, "insights": ins}
            existing = conn.execute("SELECT id, status FROM lessons WHERE course_id=? AND num=?", (course_id, num)).fetchone()
            if existing is not None and existing["status"] == "published" and not body.replace:
                continue
            if existing is None:
                conn.execute("INSERT INTO lessons (course_id, num, date, stage, status, result_json, created_at, updated_at) "
                             "VALUES (?,?,?,'published','published',?,?,?)",
                             (course_id, num, lesson.get("date") or "", json.dumps(res, ensure_ascii=False), now, now))
            else:
                conn.execute("UPDATE lessons SET result_json=?, stage='published', status='published', updated_at=? WHERE id=?",
                             (json.dumps(res, ensure_ascii=False), now, existing["id"]))
            n += 1
        conn.commit(); conn.close()
        return {"ok": True, "imported": n}

    @app.get("/api/courses/{course_id}/costs")
    def course_costs(course_id: str, request: Request, month: Optional[str] = None):
        require_teacher(request, course_id)
        conn = db(); out = costs_report(conn, course_id, month); conn.close()
        return out

    @app.get("/api/admin/costs")
    def admin_costs(request: Request, month: Optional[str] = None):
        require_admin(request)
        conn = db()
        rows = conn.execute("SELECT id, name FROM courses ORDER BY created_at").fetchall()
        reports = []
        for r in rows:
            rep = costs_report(conn, r["id"], month)
            rep["name"] = r["name"]
            reports.append(rep)
        conn.close()
        return {"month": month_prefix(month), "courses": reports,
                "totalUsd": round(sum(r["totalUsd"] for r in reports), 4)}

    @app.get("/api/admin/backups")
    def admin_backups(request: Request):
        require_admin(request)
        return list_backups(db_path)

    @app.post("/api/admin/backups")
    def admin_backup_now(request: Request):
        require_admin(request)
        return backup_now(db_path)

    return {"require_teacher": require_teacher, "require_member": require_member}
