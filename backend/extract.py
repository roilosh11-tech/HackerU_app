"""
Lesson-extraction agent for מוח הקורס.

Input:  an SRT transcript + the lesson deck (PDF).
Output: one lesson object in the exact shape of course-data.js, plus flashcards and
        pre-baked MC distractors — ready for the teacher's approval step.

Stages (mirror platform-data.js `stages`):
  transcript → parse SRT + PDF into clean text        (no LLM)
  extract    → chapters → content → deck layer         (3 LLM calls)
  cards      → flashcards + 3 distractors each         (1 LLM call) + validator

Every stage's artifact is kept so "החזר לתיקון" can rerun from that stage only.

CLI (run from repo root):
  ANTHROPIC_API_KEY=... python -m backend.extract --num 13 \
      --srt "uploads/lesson 13 srt.srt" --pdf "uploads/lesson 13.pdf" --out out/lesson_13.json
  add --from cards to rerun only the flashcards stage on an existing --out file.
"""

import os
import re
import io
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from typing import Callable, Optional

try:
    from . import prompts as P
except ImportError:
    import prompts as P

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("EXTRACT_MODEL", os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"))
STAGES = ["transcript", "extract", "cards"]
N_CARDS = 10


# ---------------------------------------------------------------------------
# Stage 1 — transcript: SRT + PDF → clean text
# ---------------------------------------------------------------------------
_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3}) --> (\d{2}):(\d{2}):(\d{2})")


def parse_srt(text: str) -> list:
    """→ [{start_s, text}] with filler cues dropped (Whisper hallucinates 'תודה.' on silence)."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        m = _TS.search(lines[1] if _TS.search(lines[1]) else lines[0])
        if not m:
            continue
        start = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        body = " ".join(l.strip() for l in lines[2:] if not _TS.search(l)).strip()
        if body:
            cues.append({"start_s": start, "text": body})
    # drop hallucinated filler: a very short cue repeated ≥ 5× across the file
    freq = {}
    for c in cues:
        if len(c["text"]) <= 12:
            freq[c["text"]] = freq.get(c["text"], 0) + 1
    junk = {t for t, n in freq.items() if n >= 5}
    return [c for c in cues if c["text"] not in junk]


def fmt_ts(s: int) -> str:
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def transcript_text(cues: list, block_s: int = 60) -> str:
    """Collapse cues into one line per `block_s` seconds: '[HH:MM:SS] words…'."""
    out, cur, cur_t = [], [], None
    for c in cues:
        b = c["start_s"] - c["start_s"] % block_s
        if cur_t is None:
            cur_t = b
        if b != cur_t:
            out.append("[%s] %s" % (fmt_ts(cur_t), " ".join(cur)))
            cur, cur_t = [], b
        cur.append(c["text"])
    if cur:
        out.append("[%s] %s" % (fmt_ts(cur_t), " ".join(cur)))
    return "\n".join(out)


def pdf_text(data: bytes) -> tuple:
    """→ (text with '--- שקופית N ---' separators, slide count). Needs pypdf."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        t = (page.extract_text() or "").strip()
        t = re.sub(r"[ \t]+", " ", t)
        parts.append("--- שקופית %d ---\n%s" % (i, t or "(ללא טקסט)"))
    return "\n".join(parts), len(reader.pages)


def stage_transcript(srt_text: str, pdf_bytes: Optional[bytes]) -> dict:
    cues = parse_srt(srt_text)
    deck, n = ("", 0)
    if pdf_bytes:
        deck, n = pdf_text(pdf_bytes)
    dur = cues[-1]["start_s"] if cues else 0
    return {
        "transcript": transcript_text(cues),
        "cueCount": len(cues),
        "firstSpeech": fmt_ts(cues[0]["start_s"]) if cues else None,
        "duration": "%dש׳ %02dד׳" % (dur // 3600, (dur % 3600) // 60),
        "deck": deck,
        "slideCount": n,
    }


# ---------------------------------------------------------------------------
# LLM plumbing
# ---------------------------------------------------------------------------
# Optional usage sink: lessons_api sets this per run so pipeline token spend lands in llm_usage.
USAGE_HOOK = None  # callable(model, usage_dict)


def llm(prompt: str, max_tokens: int = 8000, system: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    payload = {"model": MODEL, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    if system:
        payload["system"] = system
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if USAGE_HOOK:
                try:
                    USAGE_HOOK(MODEL, data.get("usage") or {})
                except Exception:
                    pass
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            if e.code in (429, 529, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError("llm error %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:300]))


def parse_json(raw: str) -> dict:
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(llm(P.REPAIR_JSON.format(raw=s[:60000]), max_tokens=8000))


def ask(prompt: str, max_tokens: int = 8000) -> dict:
    return parse_json(llm(prompt, max_tokens=max_tokens))


# ---------------------------------------------------------------------------
# Stage 2 — extract: chapters → content → deck layer
# ---------------------------------------------------------------------------
def stage_extract(t: dict, teacher: dict, teacher_notes: str, log: Callable = print) -> dict:
    deck = t.get("deck") or "(אין מצגת)"
    log("extract/chapters")
    ch = ask(P.CHAPTERS.format(rules=P.COMMON_RULES, slide_count=t.get("slideCount", 0),
                               deck=deck, transcript=t["transcript"]))
    log("extract/content")
    content = ask(P.CONTENT.format(
        rules=P.COMMON_RULES, teacher_name=teacher["name"], voice=teacher["voice"],
        cats=" / ".join(teacher["insight_cats"]), teacher_notes=teacher_notes or "—",
        chapters=json.dumps(ch.get("chapters", []), ensure_ascii=False),
        deck=deck, transcript=t["transcript"]), max_tokens=12000)
    deck_layer = {"coreConcepts": [], "gaps": []}
    if t.get("deck"):
        log("extract/deck")
        deck_layer = ask(P.DECK.format(
            rules=P.COMMON_RULES, slide_count=t.get("slideCount", 0), deck=deck,
            content=json.dumps({"summary": content.get("summary"), "concepts": content.get("concepts")},
                               ensure_ascii=False)), max_tokens=10000)
    cats = teacher["insight_cats"]
    for ins in content.get("insights", []):
        if ins.get("category") not in cats:
            ins["category"] = cats[2] if len(cats) > 2 else cats[0]
    return {"chapters": ch.get("chapters", []), "qualityNote": ch.get("qualityNote", ""),
            **{k: content.get(k) for k in ("title", "hook", "summary", "concepts", "quotes", "insights")},
            "deck": deck_layer}


# ---------------------------------------------------------------------------
# Stage 3 — cards: flashcards + distractors, validated
# ---------------------------------------------------------------------------
def validate_cards(cards: list) -> list:
    """Drop broken cards; returns list of problems found (empty = clean)."""
    problems, keep = [], []
    for i, c in enumerate(cards):
        a = (c.get("a") or "").strip()
        ds = [d.strip() for d in (c.get("distractors") or []) if isinstance(d, str) and d.strip()]
        ds = [d for d in dict.fromkeys(ds) if d != a]
        if not a or not c.get("q"):
            problems.append("card %d: empty q/a" % i); continue
        if len(ds) != 3:
            problems.append("card %d (%s): %d valid distractors" % (i, c.get("tag"), len(ds))); continue
        if c.get("difficulty") not in ("קל", "בינוני", "קשה"):
            c["difficulty"] = "בינוני"
        c["distractors"] = ds
        keep.append(c)
    cards[:] = keep
    return problems


def stage_cards(ex: dict, log: Callable = print) -> dict:
    log("cards")
    out = ask(P.CARDS.format(
        rules=P.COMMON_RULES, n_cards=N_CARDS,
        summary=json.dumps(ex.get("summary"), ensure_ascii=False),
        concepts=json.dumps(ex.get("concepts"), ensure_ascii=False),
        deck_concepts=json.dumps((ex.get("deck") or {}).get("coreConcepts", []), ensure_ascii=False)),
        max_tokens=12000)
    cards = out.get("flashcards", [])
    problems = validate_cards(cards)
    if len(cards) < N_CARDS - 2:  # one retry if too many got dropped
        log("cards/retry (%d valid)" % len(cards))
        out = ask(P.CARDS.format(rules=P.COMMON_RULES, n_cards=N_CARDS,
                                 summary=json.dumps(ex.get("summary"), ensure_ascii=False),
                                 concepts=json.dumps(ex.get("concepts"), ensure_ascii=False),
                                 deck_concepts=json.dumps((ex.get("deck") or {}).get("coreConcepts", []),
                                                          ensure_ascii=False)), max_tokens=12000)
        cards = out.get("flashcards", [])
        problems += validate_cards(cards)
    return {"flashcards": cards[:N_CARDS], "problems": problems}


# ---------------------------------------------------------------------------
# Assemble → course-data.js shape
# ---------------------------------------------------------------------------
def assemble(num: int, date: str, t: dict, ex: dict, cards: dict, pdf_name: str, teacher: dict) -> dict:
    letters = "abcdefghijklmnopqrstuvwxyz"
    concepts = [dict(c, first=num, times=1, isRepeat=False) for c in ex.get("concepts") or []]
    flash, mc = [], {}
    for i, c in enumerate(cards.get("flashcards", [])):
        cid = "L%d%s" % (num, letters[i])
        flash.append({"id": cid, "lesson": num, "tag": c["tag"], "difficulty": c["difficulty"],
                      "q": c["q"], "a": c["a"], "due": 0, "ease": 2.5, "interval": 0, "lapses": 0})
        mc[cid] = c["distractors"]
    lesson = {
        "num": num, "date": date, "title": ex.get("title", ""), "duration": t.get("duration", ""),
        "hook": ex.get("hook", ""), "status": "new", "progressPct": 0, "lastReview": 1,
        "chapters": ex.get("chapters", []), "concepts": concepts, "quotes": ex.get("quotes", []),
        "summary": ex.get("summary", {}),
        "deck": {"file": pdf_name, "totalSlides": t.get("slideCount", 0),
                 **(ex.get("deck") or {"coreConcepts": [], "gaps": []})},
    }
    if ex.get("qualityNote"):
        lesson["qualityNote"] = ex["qualityNote"]
    return {"lesson": lesson, "flashcards": flash, "distractors": mc,
            "insights": [dict(i, lesson=num) for i in ex.get("insights") or []],
            "meta": {"model": MODEL, "cueCount": t.get("cueCount"), "firstSpeech": t.get("firstSpeech"),
                     "cardProblems": cards.get("problems", []), "generatedAt": time.time()}}


def run(num: int, srt_text: str = "", pdf_bytes: bytes = None, pdf_name: str = "", date: str = "",
        teacher: dict = None, teacher_notes: str = "", from_stage: str = "transcript",
        artifacts: dict = None, log: Callable = print) -> dict:
    """Run the pipeline from `from_stage`, reusing earlier artifacts. Returns
    {'artifacts': {transcript, extract, cards}, 'result': assembled}."""
    teacher = teacher or P.DEFAULT_TEACHER
    art = dict(artifacts or {})
    start = STAGES.index(from_stage)
    if start <= 0 or "transcript" not in art:
        log("transcript")
        art["transcript"] = stage_transcript(srt_text, pdf_bytes)
    if start <= 1 or "extract" not in art:
        art["extract"] = stage_extract(art["transcript"], teacher, teacher_notes, log)
    if start <= 2 or "cards" not in art:
        art["cards"] = stage_cards(art["extract"], log)
    pdf_name = pdf_name or "lesson-%d.pdf" % num
    return {"artifacts": art, "result": assemble(num, date, art["transcript"], art["extract"], art["cards"], pdf_name, teacher)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--num", type=int, required=True)
    ap.add_argument("--srt")
    ap.add_argument("--pdf")
    ap.add_argument("--date", default="")
    ap.add_argument("--notes", default="", help="teacher's agent notes")
    ap.add_argument("--from", dest="from_stage", default="transcript", choices=STAGES)
    ap.add_argument("--out", required=True, help="output json (also read for artifacts when --from > transcript)")
    a = ap.parse_args()
    prev = {}
    if a.from_stage != "transcript" and os.path.exists(a.out):
        prev = json.load(open(a.out, encoding="utf-8")).get("artifacts", {})
    srt = open(a.srt, encoding="utf-8").read() if a.srt else ""
    pdf = open(a.pdf, "rb").read() if a.pdf else None
    t0 = time.time()
    res = run(a.num, srt, pdf, os.path.basename(a.pdf or ""), a.date, None, a.notes, a.from_stage, prev,
              log=lambda s: print("· " + s, file=sys.stderr))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(res, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    r = res["result"]
    print("done in %ds → %s" % (time.time() - t0, a.out), file=sys.stderr)
    print("  title: %s | chapters %d | concepts %d | quotes %d | cards %d | deck concepts %d | gaps %d" % (
        r["lesson"]["title"], len(r["lesson"]["chapters"]), len(r["lesson"]["concepts"]),
        len(r["lesson"]["quotes"]), len(r["flashcards"]), len(r["lesson"]["deck"]["coreConcepts"]),
        len(r["lesson"]["deck"].get("gaps", []))), file=sys.stderr)
    if r["meta"]["cardProblems"]:
        print("  card problems:", r["meta"]["cardProblems"], file=sys.stderr)
