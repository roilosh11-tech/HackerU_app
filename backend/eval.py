"""
Score an agent-generated lesson against the hand-built one in course-data.js.

  python -m backend.eval --num 13 --gen out/lesson_13.json [--course-data course-data.js]

Prints structural counts side by side, concept-term overlap, chapter timing overlap,
and the flashcard/distractor sanity checks from the add-a-lesson checklist. This is
deliberately cheap and deterministic — an LLM-judged rubric can sit on top later.
"""

import re
import json
import argparse
import subprocess


def load_course_data(path: str) -> dict:
    """course-data.js is a browser script; evaluate it with node to get real JSON."""
    js = ("const window={};" + open(path, encoding="utf-8").read() +
          ";process.stdout.write(JSON.stringify(window.CourseData));")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def toks(s: str) -> set:
    return {w for w in re.findall(r"[\w\u0590-\u05FF']+", (s or "").lower()) if len(w) > 2}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / max(1, len(a | b))


def best_match(term: str, pool: list) -> float:
    t = toks(term)
    return max((jaccard(t, toks(p)) for p in pool), default=0.0)


def secs(ts):
    if not ts or ts == "—":
        return None
    h, m, s = (list(map(int, ts.split(":"))) + [0, 0])[:3]
    return h * 3600 + m * 60 + s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--num", type=int, required=True)
    ap.add_argument("--gen", required=True)
    ap.add_argument("--course-data", default="course-data.js")
    ap.add_argument("--distractors", default="mc-distractors.js")
    a = ap.parse_args()

    gen = json.load(open(a.gen, encoding="utf-8"))["result"]
    G, gf = gen["lesson"], gen["flashcards"]
    cd = load_course_data(a.course_data)
    H = next(l for l in cd["lessons"] if l["num"] == a.num)
    hf = [c for c in cd["flashcards"] if c["lesson"] == a.num]

    def row(label, g, h):
        print("  %-22s gen %3s | hand %3s" % (label, g, h))

    print("Lesson %d — %s  vs  %s" % (a.num, G["title"], H["title"]))
    print("counts:")
    row("chapters", len(G["chapters"]), len(H["chapters"]))
    row("concepts", len(G["concepts"]), len(H["concepts"]))
    row("quotes", len(G["quotes"]), len(H["quotes"]))
    row("summary.content", len(G["summary"].get("content", [])), len(H.get("summary", {}).get("content", [])))
    row("summary.fromRili", len(G["summary"].get("fromRili", [])), len(H.get("summary", {}).get("fromRili", [])))
    row("deck.coreConcepts", len(G["deck"].get("coreConcepts", [])), len(H.get("deck", {}).get("coreConcepts", [])))
    row("deck.gaps", len(G["deck"].get("gaps", [])), len(H.get("deck", {}).get("gaps", [])))
    row("flashcards", len(gf), len(hf))

    print("concept coverage (hand-built terms found in generated, jaccard≥.34):")
    gen_terms = [c["term"] for c in G["concepts"]]
    hit = 0
    for c in H["concepts"]:
        s = best_match(c["term"], gen_terms)
        hit += s >= 0.34
        print("  %s %.2f  %s" % ("✓" if s >= 0.34 else "✗", s, c["term"]))
    print("  → %d/%d" % (hit, len(H["concepts"])))

    print("chapter boundaries (hand-built chapter starts within 5 min of a generated start):")
    gstarts = [secs(c.get("start")) for c in G["chapters"] if secs(c.get("start")) is not None]
    near = sum(1 for c in H["chapters"] if secs(c.get("start")) is not None
               and any(abs(secs(c["start"]) - g) <= 300 for g in gstarts))
    print("  → %d/%d" % (near, len([c for c in H["chapters"] if secs(c.get("start")) is not None])))

    print("summary overlap (token jaccard):")
    print("  overview %.2f | content %.2f | fromRili %.2f" % (
        jaccard(toks(G["summary"].get("overview")), toks(H["summary"].get("overview"))),
        jaccard(toks(" ".join(G["summary"].get("content", []))), toks(" ".join(H["summary"].get("content", [])))),
        jaccard(toks(" ".join(G["summary"].get("fromRili", []))), toks(" ".join(H["summary"].get("fromRili", []))))))

    print("flashcard checks:")
    mc = gen["distractors"]
    bad = [c["id"] for c in gf if len(mc.get(c["id"], [])) != 3 or any(d.strip() == c["a"].strip() for d in mc.get(c["id"], []))]
    print("  distractors: %s" % ("all 3, none equal to answer" if not bad else "PROBLEM " + ", ".join(bad)))
    diffs = [c["difficulty"] for c in gf]
    print("  difficulty mix: קל %d / בינוני %d / קשה %d" % (diffs.count("קל"), diffs.count("בינוני"), diffs.count("קשה")))
    print("  card tags vs hand-built tags: %.2f" % jaccard(toks(" ".join(c["tag"] for c in gf)), toks(" ".join(c["tag"] for c in hf))))
    if gen["meta"].get("cardProblems"):
        print("  validator dropped:", gen["meta"]["cardProblems"])


if __name__ == "__main__":
    main()
