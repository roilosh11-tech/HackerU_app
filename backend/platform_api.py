"""
Platform API — everything "Course Brain Platform.dc.html" needs in LIVE mode, in the
same shape as platform-data.js so the front-end swaps data sources, not screens.

Tables:
  cohorts          — a course run: label, start, status, per-cohort teacher override, join code
  materials        — course files/links shown in חומרי הקורס
  school_settings  — key/value JSON (school name, AI budget ₪, per-student cap, alert %, fx)
Also: memberships.cohort_id (migration), users.status='disabled' (account ops).

Mounted via register(app, db, current_user, current_user_any, require_admin, courses_api, learning_metrics).
"""

import os
import json
import time
import secrets
import datetime as dt
from typing import Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohorts (
    id             TEXT PRIMARY KEY,
    course_id      TEXT NOT NULL,
    label          TEXT NOT NULL,
    start          TEXT DEFAULT '',
    status         TEXT DEFAULT 'active',       -- active | upcoming | finished
    current_lesson INTEGER DEFAULT 0,
    teacher_json   TEXT DEFAULT '',             -- optional override {name, short, gender, title}
    join_code      TEXT UNIQUE,
    created_at     REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_cohorts_course ON cohorts(course_id);
CREATE TABLE IF NOT EXISTS materials (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    desc       TEXT DEFAULT '',
    link       TEXT DEFAULT '',
    file       TEXT DEFAULT '',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS school_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

STAGES = [
    {"key": "recording", "label": "הקלטה", "desc": "הורדת הקלטת השיעור והמצגת מפורטל הקורס"},
    {"key": "transcript", "label": "תמלול", "desc": "תמלול אוטומטי (Whisper) לעברית עם חותמות זמן"},
    {"key": "extract", "label": "חילוץ תוכן", "desc": "פרקים, סיכום, מושגים, ציטוטים, תובנות המרצה + שכבת המצגת"},
    {"key": "cards", "label": "כרטיסיות", "desc": "~10 כרטיסיות + 3 מסיחים לכל אחת, מאומתים"},
    {"key": "review", "label": "אישור מרצה", "desc": "המרצה מאשר/ת ומתקן/ת לפני פרסום"},
    {"key": "published", "label": "פורסם", "desc": "זמין לסטודנטים"},
]
STAGE_IDX = {"transcript": 1, "extract": 2, "cards": 3, "review": 4, "published": 5}

DEFAULT_SETTINGS = {
    "schoolName": os.environ.get("SCHOOL_NAME", "HackerU"),
    "schoolNameHe": os.environ.get("SCHOOL_NAME_HE", "האקריו"),
    "tagline": "מוח הקורס לכל קורס במכללה",
    "budgetIls": 900,       # school-wide monthly AI budget (₪) shown on the AI screen
    "capDaily": 25,         # per-student chat cap / day (mirrors CHAT_DAILY_LIMIT for display)
    "alertPct": 80,
    "alertOn": True,
    "capMode": "block",     # block | warn
    "fxUsdIls": 3.7,
    "pilot": {},            # {courseId, cohortId, start: 'YYYY-MM-DD'} — the pilot dashboard's cohort + start date
}


def new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def slug(s: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in (s or "").lower()).strip("-")
    return out[:40] or "c" + secrets.token_hex(3)


def ago_he(ts) -> str:
    if not ts:
        return "—"
    secs = time.time() - ts
    if secs < 3600:
        return "לפני פחות משעה"
    if secs < 86400:
        return "היום"
    d = int(secs // 86400)
    if d == 1:
        return "אתמול"
    if d < 7:
        return "לפני %d ימים" % d
    if d < 30:
        return "לפני %d שבועות" % (d // 7)
    return "לפני חודש+"


def get_settings(conn) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key, value FROM school_settings").fetchall():
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


def set_settings(conn, patch: dict):
    for k, v in patch.items():
        if k not in DEFAULT_SETTINGS:
            continue
        conn.execute("INSERT INTO school_settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (k, json.dumps(v, ensure_ascii=False)))
    conn.commit()


def cohort_public(r) -> dict:
    d = {"id": r["id"], "courseId": r["course_id"], "label": r["label"], "start": r["start"] or "—",
         "status": r["status"], "currentLesson": r["current_lesson"] or 0, "joinCode": r["join_code"]}
    if r["teacher_json"]:
        try:
            d["teacher"] = json.loads(r["teacher_json"])
        except Exception:
            pass
    return d


def ensure_default_cohort(conn, course_id: str):
    """Every course has at least one active cohort; unassigned members are placed in it."""
    row = conn.execute("SELECT id FROM cohorts WHERE course_id=? ORDER BY created_at LIMIT 1", (course_id,)).fetchone()
    now = time.time()
    if row is None:
        cid = course_id + "-" + dt.datetime.utcnow().strftime("%Y") + "a"
        conn.execute("INSERT INTO cohorts (id, course_id, label, start, status, join_code, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (cid, course_id, "מחזור א׳ " + dt.datetime.utcnow().strftime("%Y"), "", "active", new_code(), now, now))
        row = {"id": cid}
    conn.execute("UPDATE memberships SET cohort_id=? WHERE course_id=? AND (cohort_id IS NULL OR cohort_id='')", (row["id"], course_id))
    conn.commit()
    return row["id"]


def lesson_stage(row) -> dict:
    """Pipeline row → {stage 0-5, error, rework}. stage 4 = awaiting teacher approval."""
    if row is None:
        return {"stage": 0, "error": "", "rework": None}
    st, stage = row["status"], row["stage"]
    if st == "published":
        n = 5
    elif st == "review":
        n = 4
    elif st == "running":
        n = STAGE_IDX.get(stage, 1)
    elif st == "error":
        n = STAGE_IDX.get(stage, 1)
    else:  # idle — sources uploaded, nothing ran
        n = 1 if row["srt_path"] else 0
    rw = json.loads(row["rework_json"] or "[]")
    last = rw[-1] if rw else None
    return {"stage": n, "error": row["error"] or "", "running": st == "running",
            "rework": ({"round": last["round"], "reason": last["reason"] + ((" — " + last["note"]) if last.get("note") else ""),
                        "stageLabel": STAGES[STAGE_IDX.get(last["stage"], 2)]["label"], "at": int(last["at"] * 1000),
                        "open": st != "published"} if last and st != "published" else None)}


# ---------------------------------------------------------------------------
class CohortIn(BaseModel):
    label: str
    start: Optional[str] = ""
    status: Optional[str] = "active"
    teacher: Optional[dict] = None       # {name, short, gender}
    currentLesson: Optional[int] = None


class MaterialIn(BaseModel):
    title: str
    desc: Optional[str] = ""
    link: Optional[str] = ""


class SettingsIn(BaseModel):
    patch: dict


class RoleIn(BaseModel):
    role: str                             # teacher | student
    cohortId: Optional[str] = None


def register(app, db, current_user, current_user_any, require_admin, C, learning_metrics):
    conn = db(); conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memberships)").fetchall()}
    if "cohort_id" not in cols:
        conn.execute("ALTER TABLE memberships ADD COLUMN cohort_id TEXT DEFAULT ''")
    conn.commit()
    for r in conn.execute("SELECT id FROM courses").fetchall():
        ensure_default_cohort(conn, r["id"])
    conn.close()

    # ---- role helpers -----------------------------------------------------
    def roles_of(conn, uid):
        return {r["course_id"]: (r["role"], r["cohort_id"]) for r in
                conn.execute("SELECT course_id, role, cohort_id FROM memberships WHERE user_id=?", (uid,)).fetchall()}

    def is_teacher(user, conn, course_id):
        if user["role"] == "admin":
            return True
        r = roles_of(conn, user["id"]).get(course_id)
        return bool(r and r[0] == "teacher")

    def require_teacher(request, course_id):
        user = current_user(request)
        conn = db(); ok = is_teacher(user, conn, course_id); conn.close()
        if not ok:
            raise HTTPException(403, "teacher only")
        return user

    def teacher_of_user(conn, actor, uid):
        """Admin, or a teacher sharing a course with `uid`."""
        if actor["role"] == "admin":
            return True
        mine = {k for k, v in roles_of(conn, actor["id"]).items() if v[0] == "teacher"}
        theirs = set(roles_of(conn, uid).keys())
        return bool(mine & theirs)

    # ---- payload builders ---------------------------------------------------
    def roster_row(conn, u, spend_by_user):
        try:
            data = json.loads(u["data_json"] or "{}")
        except Exception:
            data = {}
        m = learning_metrics(data) if isinstance(data, dict) else {}
        return {"id": u["id"], "name": u["display_name"], "email": u["email"] or "", "username": u["username"],
                "avatar": u["avatar"] or "", "method": "google" if u["google_sub"] else "password",
                "completion": u["completion"] or 0, "points": u["points"] or 0, "streak": u["streak"] or 0,
                "reviews": data.get("reviewsTotal") or m.get("reviewsLogged") or 0,
                "recallPct": m.get("recallPct"), "activeDays30": m.get("activeDays30", 0), "opens": m.get("opens", 0),
                "weakLessons": m.get("weakLessons", []),
                "lastActive": ago_he(u["last_active"]), "lastActiveTs": u["last_active"] or 0,
                "status": u["status"], "aiUsd": round(spend_by_user.get(u["id"], 0), 4)}

    def build_course(conn, c, viewer, month):
        cid = c["id"]
        spend = {r["user_id"]: r["c"] for r in conn.execute(
            "SELECT user_id, COALESCE(SUM(cost_usd),0) c FROM llm_usage WHERE course_id=? AND day LIKE ? GROUP BY user_id",
            (cid, month + "-%")).fetchall()}
        members = conn.execute(
            "SELECT u.*, m.role AS mrole, m.cohort_id FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.course_id=?", (cid,)).fetchall()
        cohorts = [cohort_public(r) for r in conn.execute("SELECT * FROM cohorts WHERE course_id=? ORDER BY created_at", (cid,)).fetchall()]
        for co in cohorts:
            mem = [m for m in members if m["cohort_id"] == co["id"] and m["mrole"] == "student"]
            co["roster"] = [roster_row(conn, m, spend) for m in mem if m["status"] == "approved"]
            co["pendingAccounts"] = [{"id": m["id"], "name": m["display_name"], "email": m["email"] or ""} for m in mem if m["status"] == "pending"]
            co["disabledAccounts"] = [{"id": m["id"], "name": m["display_name"]} for m in mem if m["status"] == "disabled"]
        teachers = [{"id": m["id"], "name": m["display_name"], "email": m["email"] or "", "avatar": m["avatar"] or ""} for m in members if m["mrole"] == "teacher"]
        tname = c["teacher_name"] or (teachers[0]["name"] if teachers else "")
        # lessons: published content + pipeline state
        rows = conn.execute("SELECT * FROM lessons WHERE course_id=? ORDER BY num", (cid,)).fetchall()
        lessons, cards, mc, insights = [], [], {}, []
        for r in rows:
            meta = lesson_stage(r)
            res = json.loads(r["result_json"] or "{}")
            L = dict(res.get("lesson") or {"num": r["num"], "title": (res.get("lesson") or {}).get("title", "") or "שיעור %d" % r["num"], "date": r["date"]})
            L["num"] = r["num"]
            L.update({"stage": meta["stage"], "pipelineError": meta["error"], "running": meta["running"], "rework": meta["rework"],
                      "edited": json.loads(r["edited_json"] or "{}"), "teacherNotes": r["teacher_notes"] or "",
                      "hasSrt": bool(r["srt_path"]), "hasPdf": bool(r["pdf_path"]), "updatedAt": r["updated_at"]})
            if meta["stage"] < 4:
                L.setdefault("title", "בעיבוד")
            lessons.append(L)
            if meta["stage"] >= 5 or is_teacher(viewer, conn, cid):
                cards += res.get("flashcards", []); mc.update(res.get("distractors", {})); insights += res.get("insights", [])
        mats = [{"id": r["id"], "title": r["title"], "desc": r["desc"], "link": r["link"], "file": r["file"]} for r in
                conn.execute("SELECT * FROM materials WHERE course_id=? ORDER BY created_at", (cid,)).fetchall()]
        total = max(int(os.environ.get("DEFAULT_TOTAL_LESSONS", "32")), len(lessons))
        return {"id": cid, "name": c["name"], "nameHe": c["name"], "accent": c["accent"] or "#98b1ff",
                "teacher": {"name": tname, "short": tname.split(" ")[0] if tname else "", "gender": "f", "title": "מרצה"},
                "teachers": teachers, "joinCode": c["join_code"], "budgetUsd": c["budget_usd"], "active": bool(c["active"]),
                "insightCats": ["עקרון מנחה", "אזהרה", "דרך חשיבה", "הכוונה מעשית"],
                "totalLessons": total, "students": sum(len(co["roster"]) for co in cohorts),
                "cohorts": cohorts, "lessons": lessons, "flashcards": cards, "distractors": mc, "insights": insights,
                "materials": mats, "process": [], "spentUsd": round(sum(spend.values()), 4)}

    @app.get("/api/platform")
    def platform(request: Request, month: Optional[str] = None):
        """Full platform payload for the caller: school + courses (scoped by role) + me."""
        user = current_user(request)
        conn = db()
        month = C.month_prefix(month)
        settings = get_settings(conn)
        mine = roles_of(conn, user["id"])
        if user["role"] == "admin":
            crows = conn.execute("SELECT * FROM courses WHERE active=1 ORDER BY created_at").fetchall()
        else:
            crows = [r for r in conn.execute("SELECT * FROM courses WHERE active=1 ORDER BY created_at").fetchall() if r["id"] in mine]
        courses = [build_course(conn, c, user, month) for c in crows]
        # students never see join codes / other students' emails
        if user["role"] != "admin":
            for c in courses:
                if mine.get(c["id"], ("student",))[0] != "teacher":
                    c.pop("joinCode", None); c.pop("budgetUsd", None)
                    for co in c["cohorts"]:
                        co.pop("joinCode", None); co["roster"] = [{"name": r["name"], "completion": r["completion"]} for r in co["roster"]]
                        co["pendingAccounts"] = []
        assignments = []
        for cid, (role, coid) in mine.items():
            if any(c["id"] == cid for c in courses):
                assignments.append({"courseId": cid, "cohortId": coid or "", "role": role})
        role = "school" if user["role"] == "admin" else ("teacher" if any(a["role"] == "teacher" for a in assignments) else "student")
        me = {"id": user["id"], "name": user["display_name"], "email": user["email"] or user["username"], "avatar": user["avatar"] or "",
              "role": role, "title": "אדמין" if role == "school" else ("מרצה" if role == "teacher" else ""),
              "assignments": [a for a in assignments if a["role"] == ("teacher" if role == "teacher" else "student")] or assignments}
        costs = None
        if role == "school":
            costs = {"month": month, "courses": [dict(C.costs_report(conn, c["id"], month), name=c["name"], accent=c["accent"]) for c in courses]}
            costs["totalUsd"] = round(sum(x["totalUsd"] for x in costs["courses"]), 4)
            costs["daily"] = [{"day": r["day"], "usd": round(r["c"], 4)} for r in conn.execute(
                "SELECT day, COALESCE(SUM(cost_usd),0) c FROM llm_usage WHERE day >= ? GROUP BY day ORDER BY day",
                ((dt.datetime.utcnow() - dt.timedelta(days=30)).strftime("%Y-%m-%d"),)).fetchall()]
        elif role == "teacher":
            costs = {"month": month, "courses": [dict(C.costs_report(conn, a["courseId"], month), name=next(c["name"] for c in courses if c["id"] == a["courseId"]))
                                                for a in me["assignments"]]}
        conn.close()
        return {"school": {"name": settings["schoolName"], "nameHe": settings["schoolNameHe"], "tagline": settings["tagline"]},
                "settings": settings if role == "school" else None, "stages": STAGES, "courses": courses, "me": me,
                "costs": costs, "generatedAt": time.time()}

    # ---- cohorts ------------------------------------------------------------
    @app.post("/api/courses/{course_id}/cohorts")
    def create_cohort(course_id: str, body: CohortIn, request: Request):
        require_admin(request)
        conn = db()
        if conn.execute("SELECT id FROM courses WHERE id=?", (course_id,)).fetchone() is None:
            conn.close(); raise HTTPException(404, "course not found")
        base = course_id + "-" + slug(body.label)[:12]
        cid, i = base, 1
        while conn.execute("SELECT id FROM cohorts WHERE id=?", (cid,)).fetchone():
            i += 1; cid = base + "-" + str(i)
        now = time.time()
        conn.execute("INSERT INTO cohorts (id, course_id, label, start, status, current_lesson, teacher_json, join_code, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (cid, course_id, body.label.strip(), body.start or "", body.status if body.status in ("active", "upcoming", "finished") else "active",
                      body.currentLesson or 0, json.dumps(body.teacher, ensure_ascii=False) if body.teacher else "", new_code(), now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM cohorts WHERE id=?", (cid,)).fetchone(); conn.close()
        return cohort_public(row)

    @app.put("/api/courses/{course_id}/cohorts/{cohort_id}")
    def update_cohort(course_id: str, cohort_id: str, body: CohortIn, request: Request):
        require_teacher(request, course_id)
        conn = db()
        row = conn.execute("SELECT * FROM cohorts WHERE id=? AND course_id=?", (cohort_id, course_id)).fetchone()
        if row is None:
            conn.close(); raise HTTPException(404, "cohort not found")
        conn.execute("UPDATE cohorts SET label=?, start=?, status=?, current_lesson=?, teacher_json=?, updated_at=? WHERE id=?",
                     (body.label.strip() or row["label"], body.start if body.start is not None else row["start"],
                      body.status if body.status in ("active", "upcoming", "finished") else row["status"],
                      body.currentLesson if body.currentLesson is not None else row["current_lesson"],
                      json.dumps(body.teacher, ensure_ascii=False) if body.teacher else ("" if body.teacher == {} else row["teacher_json"]),
                      time.time(), cohort_id))
        conn.commit()
        row = conn.execute("SELECT * FROM cohorts WHERE id=?", (cohort_id,)).fetchone(); conn.close()
        return cohort_public(row)

    @app.delete("/api/courses/{course_id}/cohorts/{cohort_id}")
    def delete_cohort(course_id: str, cohort_id: str, request: Request):
        require_admin(request)
        conn = db()
        n = conn.execute("SELECT COUNT(*) n FROM cohorts WHERE course_id=?", (course_id,)).fetchone()["n"]
        if n <= 1:
            conn.close(); raise HTTPException(400, "לקורס חייב להישאר מחזור אחד לפחות")
        conn.execute("DELETE FROM memberships WHERE course_id=? AND cohort_id=? AND role='student'", (course_id, cohort_id))
        conn.execute("DELETE FROM cohorts WHERE id=? AND course_id=?", (cohort_id, course_id))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/courses/{course_id}/cohorts/{cohort_id}/code/rotate")
    def rotate_cohort_code(course_id: str, cohort_id: str, request: Request):
        require_teacher(request, course_id)
        conn = db(); code = new_code()
        conn.execute("UPDATE cohorts SET join_code=?, updated_at=? WHERE id=? AND course_id=?", (code, time.time(), cohort_id, course_id))
        conn.commit(); conn.close()
        return {"joinCode": code}

    # ---- members / accounts -------------------------------------------------
    @app.put("/api/courses/{course_id}/members/{uid}/role")
    def set_role(course_id: str, uid: int, body: RoleIn, request: Request):
        """Admin promotes a user to teacher of a course (or back), optionally moving cohort."""
        require_admin(request)
        role = body.role if body.role in ("teacher", "student") else "student"
        conn = db()
        coid = body.cohortId or (conn.execute("SELECT cohort_id FROM memberships WHERE user_id=? AND course_id=?", (uid, course_id)).fetchone() or {"cohort_id": ""})["cohort_id"] or ensure_default_cohort(conn, course_id)
        conn.execute("INSERT INTO memberships (user_id, course_id, role, cohort_id, created_at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(user_id, course_id) DO UPDATE SET role=excluded.role, cohort_id=excluded.cohort_id", (uid, course_id, role, coid, time.time()))
        conn.execute("UPDATE users SET status='approved' WHERE id=? AND status='pending'", (uid,))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/users/{uid}/approve")
    def approve_user(uid: int, request: Request):
        actor = current_user(request)
        conn = db()
        if not teacher_of_user(conn, actor, uid):
            conn.close(); raise HTTPException(403, "not allowed")
        conn.execute("UPDATE users SET status='approved' WHERE id=?", (uid,)); conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/users/{uid}/reject")
    def reject_user(uid: int, request: Request):
        actor = current_user(request)
        conn = db()
        if not teacher_of_user(conn, actor, uid):
            conn.close(); raise HTTPException(403, "not allowed")
        conn.execute("UPDATE users SET status='rejected' WHERE id=?", (uid,)); conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/users/{uid}/disable")
    def disable_user(uid: int, request: Request):
        actor = current_user(request)
        if actor["id"] == uid:
            raise HTTPException(400, "cannot disable yourself")
        conn = db()
        if not teacher_of_user(conn, actor, uid):
            conn.close(); raise HTTPException(403, "not allowed")
        conn.execute("UPDATE users SET status='disabled' WHERE id=? AND role<>'admin'", (uid,)); conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/users/{uid}/enable")
    def enable_user(uid: int, request: Request):
        actor = current_user(request)
        conn = db()
        if not teacher_of_user(conn, actor, uid):
            conn.close(); raise HTTPException(403, "not allowed")
        conn.execute("UPDATE users SET status='approved' WHERE id=?", (uid,)); conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/users/{uid}/temp-password")
    def temp_password(uid: int, request: Request):
        """One-time temporary password for a password account (shown once to the teacher/admin)."""
        actor = current_user(request)
        conn = db()
        if not teacher_of_user(conn, actor, uid):
            conn.close(); raise HTTPException(403, "not allowed")
        words = ["Sky", "Nova", "Delta", "Pixel", "Orbit", "Lumen", "Quartz", "Atlas"]
        pw = secrets.choice(words) + "-" + str(secrets.randbelow(9000) + 1000) + "-" + secrets.choice(words).lower()
        from importlib import import_module
        try:
            main = import_module("backend.main")
        except Exception:
            main = import_module("main")
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (main.hash_pw(pw), uid)); conn.commit(); conn.close()
        return {"temp": pw}

    @app.put("/api/courses/{course_id}/members/{uid}/cohort")
    def move_cohort(course_id: str, uid: int, body: RoleIn, request: Request):
        require_teacher(request, course_id)
        conn = db()
        if conn.execute("SELECT id FROM cohorts WHERE id=? AND course_id=?", (body.cohortId, course_id)).fetchone() is None:
            conn.close(); raise HTTPException(404, "cohort not found")
        conn.execute("UPDATE memberships SET cohort_id=? WHERE user_id=? AND course_id=?", (body.cohortId, uid, course_id))
        conn.commit(); conn.close()
        return {"ok": True}

    # ---- materials ------------------------------------------------------------
    @app.post("/api/courses/{course_id}/materials")
    def add_material(course_id: str, body: MaterialIn, request: Request):
        require_teacher(request, course_id)
        if not body.title.strip():
            raise HTTPException(400, "title required")
        conn = db()
        mid = conn.execute("INSERT INTO materials (course_id, title, desc, link, created_at) VALUES (?,?,?,?,?)",
                           (course_id, body.title.strip(), body.desc or "", body.link or "", time.time())).lastrowid
        conn.commit(); conn.close()
        return {"id": mid}

    @app.delete("/api/courses/{course_id}/materials/{mid}")
    def del_material(course_id: str, mid: int, request: Request):
        require_teacher(request, course_id)
        conn = db(); conn.execute("DELETE FROM materials WHERE id=? AND course_id=?", (mid, course_id)); conn.commit(); conn.close()
        return {"ok": True}

    # ---- settings -------------------------------------------------------------
    @app.get("/api/settings")
    def settings_get(request: Request):
        require_admin(request)
        conn = db(); s = get_settings(conn); conn.close()
        return s

    @app.put("/api/settings")
    def settings_put(body: SettingsIn, request: Request):
        require_admin(request)
        conn = db(); set_settings(conn, body.patch or {}); s = get_settings(conn); conn.close()
        return s

    return {"ensure_default_cohort": ensure_default_cohort}
