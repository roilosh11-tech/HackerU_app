"""
Pilot API — real student feedback + active-time tracking for the pilot dashboard
(Course Brain Platform → school → פיילוט).

Tables:
  feedback     — NPS 0–10, what helped, free text, anonymous flag (student chooses)
  usage_daily  — active seconds per user/course/day (client pings every ~60 s while the tab is visible + used)

Mounted via register(app, db, current_user, require_admin).
"""

import json
import time
import datetime as dt
from typing import List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    course_id  TEXT NOT NULL,
    cohort_id  TEXT DEFAULT '',
    nps        INTEGER NOT NULL,
    helped     TEXT DEFAULT '[]',
    text       TEXT DEFAULT '',
    anonymous  INTEGER DEFAULT 0,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_feedback_cohort ON feedback(course_id, cohort_id);
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id   INTEGER NOT NULL,
    course_id TEXT NOT NULL,
    cohort_id TEXT DEFAULT '',
    day       TEXT NOT NULL,
    secs      INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, course_id, day)
);
"""


class FeedbackIn(BaseModel):
    courseId: str
    cohortId: Optional[str] = ""
    nps: int
    helped: List[str] = []
    text: Optional[str] = ""
    anonymous: bool = False


class UsageIn(BaseModel):
    courseId: str
    cohortId: Optional[str] = ""
    secs: int


def register(app, db, current_user, require_admin):
    conn = db(); conn.executescript(SCHEMA); conn.commit(); conn.close()

    @app.post("/api/feedback")
    def send_feedback(body: FeedbackIn, request: Request):
        user = current_user(request)
        if not 0 <= body.nps <= 10:
            raise HTTPException(400, "nps must be 0–10")
        helped = [str(h)[:60] for h in (body.helped or [])][:10]
        conn = db()
        conn.execute("INSERT INTO feedback (user_id, course_id, cohort_id, nps, helped, text, anonymous, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (user["id"], body.courseId, body.cohortId or "", body.nps, json.dumps(helped, ensure_ascii=False),
                      (body.text or "").strip()[:2000], 1 if body.anonymous else 0, time.time()))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.post("/api/usage/ping")
    def usage_ping(body: UsageIn, request: Request):
        user = current_user(request)
        secs = max(0, min(int(body.secs or 0), 300))
        if not secs:
            return {"ok": True}
        day = dt.date.today().isoformat()
        conn = db()
        conn.execute("INSERT INTO usage_daily (user_id, course_id, cohort_id, day, secs) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(user_id, course_id, day) DO UPDATE SET secs = secs + excluded.secs, cohort_id = excluded.cohort_id",
                     (user["id"], body.courseId, body.cohortId or "", day, secs))
        conn.execute("UPDATE users SET last_active=? WHERE id=?", (time.time(), user["id"]))
        conn.commit(); conn.close()
        return {"ok": True}

    @app.get("/api/pilot")
    def pilot(request: Request, courseId: str, cohortId: Optional[str] = "", start: Optional[str] = ""):
        """Admin view: all feedback + daily usage for one cohort. Anonymous feedback never carries a name."""
        require_admin(request)
        conn = db()
        q = " AND f.cohort_id=?" if cohortId else ""
        args = (courseId, cohortId) if cohortId else (courseId,)
        fb = conn.execute("SELECT f.*, u.display_name FROM feedback f LEFT JOIN users u ON u.id=f.user_id "
                          "WHERE f.course_id=?" + q + " ORDER BY f.created_at", args).fetchall()
        us = conn.execute("SELECT d.day, d.secs, u.display_name FROM usage_daily d LEFT JOIN users u ON u.id=d.user_id "
                          "WHERE d.course_id=?" + q.replace("f.", "d.") + " ORDER BY d.day", args).fetchall()
        # weekly flashcard accuracy (good/easy share of all ratings) from each student's retLog, weeks counted from the pilot start
        accuracy = []
        try:
            t0 = dt.datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000 if start else None
        except ValueError:
            t0 = None
        if t0 is not None:
            mq = "SELECT u.data_json FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.course_id=? AND m.role='student'" + (" AND m.cohort_id=?" if cohortId else "")
            weeks = {}
            for row in conn.execute(mq, args).fetchall():
                try:
                    log = (json.loads(row["data_json"] or "{}") or {}).get("retLog") or []
                except Exception:
                    log = []
                for e in log:
                    if not isinstance(e, dict) or e.get("r") not in ("again", "hard", "good", "easy"):
                        continue
                    t = e.get("t") or 0
                    if t < t0:
                        continue
                    w = int((t - t0) // (7 * 86400000))
                    ok, n = weeks.get(w, (0, 0))
                    weeks[w] = (ok + (1 if e["r"] in ("good", "easy") else 0), n + 1)
            if weeks:
                cur = int((time.time() * 1000 - t0) // (7 * 86400000))
                prev = None
                for w in range(0, max(cur, max(weeks)) + 1):
                    ok, n = weeks.get(w, (0, 0))
                    prev = round(ok / n * 100) if n else prev
                    accuracy.append(prev if prev is not None else 0)
        conn.close()
        return {
            "accuracy": accuracy,
            "feedback": [{"at": int(r["created_at"] * 1000), "nps": r["nps"], "helped": json.loads(r["helped"] or "[]"),
                          "text": r["text"] or "", "anon": bool(r["anonymous"]),
                          "name": None if r["anonymous"] else (r["display_name"] or "")} for r in fb],
            "usage": [{"name": r["display_name"] or "", "day": r["day"], "secs": r["secs"]} for r in us],
        }
