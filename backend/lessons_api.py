"""
Lesson pipeline API — upload sources, run the extraction agent in the background,
teacher approves / sends back to a stage. Mounted onto the main FastAPI app via
`register(app, db, current_user, require_admin)` so main.py stays small.

Tables:
  lessons  — one row per (course, num): stage, status, sources, artifacts, result
  jobs     — one row per pipeline run (for the log / rework rounds)
"""

import os
import json
import time
import threading
import traceback
from typing import Optional

from fastapi import HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel

try:
    from . import extract as X
except ImportError:
    import extract as X

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(X.__file__), "..", "assets", "slides"))
SRC_DIR = os.environ.get("SOURCE_DIR", os.path.join(os.path.dirname(UPLOAD_DIR), "sources"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   TEXT NOT NULL,
    num         INTEGER NOT NULL,
    date        TEXT DEFAULT '',
    stage       TEXT DEFAULT 'transcript',   -- transcript | extract | cards | review | published
    status      TEXT DEFAULT 'idle',         -- idle | running | error | review | published
    error       TEXT DEFAULT '',
    srt_path    TEXT DEFAULT '',
    pdf_path    TEXT DEFAULT '',
    pdf_name    TEXT DEFAULT '',
    teacher_notes TEXT DEFAULT '',
    artifacts_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    edited_json TEXT DEFAULT '{}',           -- teacher edits on top of result (summary etc.)
    rework_json TEXT DEFAULT '[]',           -- [{round, reason, note, stage, at}]
    created_at  REAL, updated_at REAL,
    UNIQUE(course_id, num)
);
CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER NOT NULL,
    from_stage TEXT, status TEXT, log TEXT DEFAULT '',
    started_at REAL, finished_at REAL
);
"""


class ReworkIn(BaseModel):
    reason: str
    note: Optional[str] = ""


class EditIn(BaseModel):
    edited: dict


# reason → stage to rerun from (same map the UI uses)
REASON_STAGE = {"הסיכום לא מדויק": "extract", "פספס תוכן מהמצגת": "extract",
                "התובנות לא בקול שלי": "extract", "כרטיסיות חלשות": "cards", "שגיאות בתמלול": "transcript"}


def public(row) -> dict:
    res = json.loads(row["result_json"] or "{}")
    return {"id": row["id"], "courseId": row["course_id"], "num": row["num"], "date": row["date"],
            "stage": row["stage"], "status": row["status"], "error": row["error"],
            "hasSrt": bool(row["srt_path"]), "hasPdf": bool(row["pdf_path"]), "pdfName": row["pdf_name"],
            "teacherNotes": row["teacher_notes"], "rework": json.loads(row["rework_json"] or "[]"),
            "edited": json.loads(row["edited_json"] or "{}"),
            "title": (res.get("lesson") or {}).get("title", ""),
            "result": res, "updatedAt": row["updated_at"]}


def register(app, db, current_user, require_admin, require_teacher=None, require_member=None, usage_sink=None):
    # Course-scoped guards: a course teacher (or platform admin) runs the pipeline; any
    # enrolled member (or admin) reads published content. Fall back to the admin-only guards.
    if require_teacher is None:
        require_teacher = lambda request, course_id: require_admin(request)
    if require_member is None:
        require_member = lambda request, course_id: current_user(request)
    conn = db(); conn.executescript(SCHEMA); conn.commit(); conn.close()
    os.makedirs(SRC_DIR, exist_ok=True); os.makedirs(UPLOAD_DIR, exist_ok=True)

    def get(conn, course_id, num):
        row = conn.execute("SELECT * FROM lessons WHERE course_id=? AND num=?", (course_id, num)).fetchone()
        if row is None:
            raise HTTPException(404, "lesson not found")
        return row

    def worker(lesson_id: int, from_stage: str):
        conn = db()
        row = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        job = conn.execute("INSERT INTO jobs (lesson_id, from_stage, status, started_at) VALUES (?,?,?,?)",
                           (lesson_id, from_stage, "running", time.time())).lastrowid
        conn.commit()
        lines = []

        def log(s):
            lines.append("%s %s" % (time.strftime("%H:%M:%S"), s))
            conn.execute("UPDATE jobs SET log=? WHERE id=?", ("\n".join(lines), job)); conn.commit()

        try:
            srt = open(row["srt_path"], encoding="utf-8").read() if row["srt_path"] else ""
            pdf = open(row["pdf_path"], "rb").read() if row["pdf_path"] else None
            # course teacher voice for the prompts + token accounting per course
            crow = conn.execute("SELECT teacher_name FROM courses WHERE id=?", (row["course_id"],)).fetchone()
            teacher = None
            if crow and crow["teacher_name"]:
                tn = crow["teacher_name"]
                teacher = {"name": tn, "short": tn.split(" ")[0], "gender": "f"}
            if usage_sink:
                X.USAGE_HOOK = lambda model, usage, cid=row["course_id"]: usage_sink(cid, model, usage)
            out = X.run(row["num"], srt, pdf, row["pdf_name"], row["date"], teacher, row["teacher_notes"],
                        from_stage, json.loads(row["artifacts_json"] or "{}"), log)
            conn.execute("UPDATE lessons SET artifacts_json=?, result_json=?, stage='review', status='review', "
                         "error='', edited_json='{}', updated_at=? WHERE id=?",
                         (json.dumps(out["artifacts"], ensure_ascii=False),
                          json.dumps(out["result"], ensure_ascii=False), time.time(), lesson_id))
            conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id=?", (time.time(), job))
        except Exception as e:
            traceback.print_exc()
            conn.execute("UPDATE lessons SET status='error', error=?, updated_at=? WHERE id=?",
                         (type(e).__name__ + ": " + str(e), time.time(), lesson_id))
            conn.execute("UPDATE jobs SET status='error', log=?, finished_at=? WHERE id=?",
                         ("\n".join(lines + [str(e)]), time.time(), job))
        conn.commit(); conn.close()

    def start(lesson_id: int, from_stage: str):
        conn = db()
        conn.execute("UPDATE lessons SET status='running', stage=?, error='', updated_at=? WHERE id=?",
                     (from_stage, time.time(), lesson_id))
        conn.commit(); conn.close()
        threading.Thread(target=worker, args=(lesson_id, from_stage), daemon=True).start()

    @app.get("/api/courses/{course_id}/lessons")
    def list_lessons(course_id: str, request: Request):
        require_member(request, course_id)
        conn = db()
        rows = conn.execute("SELECT * FROM lessons WHERE course_id=? ORDER BY num", (course_id,)).fetchall()
        conn.close()
        out = [public(r) for r in rows]
        for o in out:  # list view: strip the heavy payload
            o.pop("result", None); o.pop("edited", None)
        return {"lessons": out}

    @app.get("/api/courses/{course_id}/lessons/{num}")
    def get_lesson(course_id: str, num: int, request: Request):
        require_member(request, course_id)
        conn = db(); row = get(conn, course_id, num)
        jobs = conn.execute("SELECT * FROM jobs WHERE lesson_id=? ORDER BY id DESC LIMIT 5", (row["id"],)).fetchall()
        conn.close()
        o = public(row)
        o["jobs"] = [dict(j) for j in jobs]
        return o

    @app.post("/api/courses/{course_id}/lessons/{num}/sources")
    async def upload_sources(course_id: str, num: int, request: Request,
                             srt: Optional[UploadFile] = File(None), pdf: Optional[UploadFile] = File(None),
                             date: str = Form(""), teacher_notes: str = Form(""), run: str = Form("1")):
        """Teacher uploads the transcript and/or deck for a lesson; starts the agent unless run=0."""
        require_teacher(request, course_id)
        conn = db()
        row = conn.execute("SELECT * FROM lessons WHERE course_id=? AND num=?", (course_id, num)).fetchone()
        now = time.time()
        if row is None:
            conn.execute("INSERT INTO lessons (course_id, num, created_at, updated_at) VALUES (?,?,?,?)",
                         (course_id, num, now, now))
            row = conn.execute("SELECT * FROM lessons WHERE course_id=? AND num=?", (course_id, num)).fetchone()
        srt_path, pdf_path, pdf_name = row["srt_path"], row["pdf_path"], row["pdf_name"]
        if srt is not None:
            srt_path = os.path.join(SRC_DIR, "%s-lesson-%d.srt" % (course_id, num))
            open(srt_path, "wb").write(await srt.read())
        if pdf is not None:
            pdf_name = "lesson-%d.pdf" % num
            pdf_path = os.path.join(UPLOAD_DIR, pdf_name)
            open(pdf_path, "wb").write(await pdf.read())
        if not srt_path:
            conn.close()
            raise HTTPException(400, "transcript (SRT) is required")
        conn.execute("UPDATE lessons SET srt_path=?, pdf_path=?, pdf_name=?, date=COALESCE(NULLIF(?,''),date), "
                     "teacher_notes=COALESCE(NULLIF(?,''),teacher_notes), updated_at=? WHERE id=?",
                     (srt_path, pdf_path, pdf_name, date, teacher_notes, now, row["id"]))
        conn.commit(); conn.close()
        if run != "0":
            start(row["id"], "transcript")
        return {"ok": True, "id": row["id"], "running": run != "0"}

    @app.post("/api/courses/{course_id}/lessons/{num}/run")
    def run_lesson(course_id: str, num: int, request: Request, from_stage: str = "transcript"):
        require_teacher(request, course_id)
        if from_stage not in X.STAGES:
            raise HTTPException(400, "bad stage")
        conn = db(); row = get(conn, course_id, num); conn.close()
        if row["status"] == "running":
            raise HTTPException(409, "already running")
        start(row["id"], from_stage)
        return {"ok": True}

    @app.post("/api/courses/{course_id}/lessons/{num}/rework")
    def rework(course_id: str, num: int, body: ReworkIn, request: Request):
        """'החזר לתיקון': log the reason, append it to the teacher notes, rerun from the stage that produced it."""
        require_teacher(request, course_id)
        stage = REASON_STAGE.get(body.reason, "extract")
        conn = db(); row = get(conn, course_id, num)
        rw = json.loads(row["rework_json"] or "[]")
        rw.append({"round": len(rw) + 1, "reason": body.reason, "note": body.note or "", "stage": stage, "at": time.time()})
        notes = row["teacher_notes"] or ""
        fix = "תיקון סבב %d — %s%s" % (len(rw), body.reason, (": " + body.note) if body.note else "")
        conn.execute("UPDATE lessons SET rework_json=?, teacher_notes=?, updated_at=? WHERE id=?",
                     (json.dumps(rw, ensure_ascii=False), (notes + "\n" + fix).strip(), time.time(), row["id"]))
        conn.commit(); conn.close()
        start(row["id"], stage)
        return {"ok": True, "round": len(rw), "stage": stage}

    @app.put("/api/courses/{course_id}/lessons/{num}/edit")
    def edit(course_id: str, num: int, body: EditIn, request: Request):
        require_teacher(request, course_id)
        conn = db(); row = get(conn, course_id, num)
        conn.execute("UPDATE lessons SET edited_json=?, updated_at=? WHERE id=?",
                     (json.dumps(body.edited, ensure_ascii=False), time.time(), row["id"]))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/courses/{course_id}/lessons/{num}/approve")
    def approve(course_id: str, num: int, request: Request):
        require_teacher(request, course_id)
        conn = db(); row = get(conn, course_id, num)
        if row["status"] != "review":
            conn.close(); raise HTTPException(409, "nothing to approve")
        res = json.loads(row["result_json"] or "{}")
        res["lesson"] = {**res.get("lesson", {}), **json.loads(row["edited_json"] or "{}")}
        conn.execute("UPDATE lessons SET result_json=?, stage='published', status='published', updated_at=? WHERE id=?",
                     (json.dumps(res, ensure_ascii=False), time.time(), row["id"]))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.get("/api/courses/{course_id}/published")
    def published(course_id: str, request: Request):
        """What students load: published lessons + flashcards + distractors, course-data.js shape."""
        require_member(request, course_id)
        conn = db()
        rows = conn.execute("SELECT result_json FROM lessons WHERE course_id=? AND status='published' ORDER BY num",
                            (course_id,)).fetchall()
        conn.close()
        lessons, cards, mc, insights = [], [], {}, []
        for r in rows:
            res = json.loads(r["result_json"] or "{}")
            lessons.append(res.get("lesson")); cards += res.get("flashcards", [])
            mc.update(res.get("distractors", {})); insights += res.get("insights", [])
        return {"lessons": lessons, "flashcards": cards, "distractors": mc, "insights": insights}
