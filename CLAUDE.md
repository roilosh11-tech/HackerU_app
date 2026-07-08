# מוח הקורס (Course Brain) — project conventions

Personal learning hub for a 32-lesson AI Solutions Architect course (HackerU, teacher: Rili).
Single Design Component: `Course Brain.dc.html`. Full UI is Hebrew, RTL, dark-mode-default (light theme toggle exists). Technical terms (React, LLM, PRD, RAG, Vibe Coding, API) stay in English inline.

## Lesson detail template (DEFAULT for every lesson)
Order of the lesson detail page, top to bottom:
1. Back link, lesson number + status badge, title, meta (date · duration · chapter count).
2. **סיכום השיעור first** — the lesson summary is the leading block, right under the meta.
   - Intro = `summary.overview` as a calm larger paragraph (17px), not a dense card.
   - Two columns (stack on mobile): **התוכן העיקרי** (`summary.content`, green check-diamond bullets) and **התובנות של רילי** (`summary.fromRili`, styled in the teacher-insight "דרך חשיבה" teal — her voice, distinct from content).
   - If a lesson has no `summary` yet, show the calm "pending" empty state (already built) — keep the summary-first template intact.
3. Slide preview placeholder + "צפה במצגת המקורית" link.
4. **Collapsible sections**, default collapsed, each a header row with item count + chevron: **פרקים**, **מושגי מפתח**, **ציטוטים בולטים**. Chapters/concepts keep an inner scroll cap (~6 rows) when open.
5. Action buttons: חזרה על השיעור / שאל שאלה על השיעור.

## מהמצגת section (deep slide layer) — per lesson
A collapsible section (same pattern as פרקים / מושגי מפתח / ציטוטים) that is the deep, slide-faithful layer — NOT a rehash of the summary/insights, but the full detail the deck actually contains that the transcript-based summary glossed over or skipped (e.g. lesson 3 lists "7 sins" in insights but the deck breaks down each one). Three parts:
- **Embedded PDF viewer** of the deck (the source of truth).
- **מושגי ליבה מהמצגת** — expandable detailed cards, one per framework/concept the deck develops in depth. Each card = concept name + a full breakdown (e.g. all 7 sins listed with their one-line each; MoSCoW's four categories defined; PERT's formula + why ×4). These are the pieces the summary compressed.
- **מה שהסיכום לא כיסה** — a short, honest callout of crucial slide content that did not surface in the transcript at all (the slide-only chapters already flag some of this), so it's clear this section adds rather than repeats.
Deck data lives in a per-lesson `deck` field, populated by reading the actual lesson PDF: `deck{ file, totalSlides, coreConcepts[{title, detail[]}], gaps[] }`. PDFs are stored under `assets/slides/` with clean ASCII names (`lesson-N.pdf`).

## Data shape per lesson (in `window.CourseData.lessons`, defined in `course-data.js`)
`num, date, title, duration, hook, status ('review'|'mastered'|'new'), progressPct, lastReview, qualityNote?, chapters[], concepts[], quotes[], summary{overview, content[], fromRili[]}, deck?{...}`.
Each chapter/concept/quote carries `source: 'slide'|'speech'|'spoken'|'both'` → renders מצגת/דיבור badge; slide-only chapters have `start:null` → "מהמצגת בלבד" tag. `qualityNote` renders in the dismissible "הערה על איכות המקור" banner.

## Color language (meaningful, sparing)
- לחזרה = review orange, בשליטה = master green, מושג חדש = new blue.
- Four teacher-insight categories each have a fixed color (עקרון מנחה / אזהרה / דרך חשיבה / הכוונה מעשית), consistent across the app; Rili's summary column reuses the דרך חשיבה teal.

## Build notes
- All styling inline (DC rule). Colors via CSS vars set on the root wrapper from `themeCSS()`; both dark + light palettes must stay in sync when adding a color.
- Icons: literal inline `<svg>` in template (React elements in `{{ holes }}` and `dangerouslySetInnerHTML` do NOT render).
- Backend is FastAPI (to be wired later); mock data lives in `course-data.js` (`window.CourseData`), loaded via a `<script src>` in each DC's `<helmet>`; the logic class exposes it through `get data()`.
- Both DCs' `themeCSS()` dark+light palettes must stay in sync (currently a lighter slate dark theme). A `button{color:inherit}` reset in `<helmet>` keeps card text from falling back to the UA default color.
