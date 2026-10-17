/*
 * CourseAPI — front-end client for the מוח הקורס backend.
 *
 * Loaded via <script src="api.js"> in each DC's <helmet>; exposes window.CourseAPI.
 *
 * TWO MODES, chosen automatically:
 *   • ONLINE  — a backend is reachable (deployed on Railway, or running locally).
 *               Auth is real (JWT in localStorage), data is saved to the server,
 *               and it syncs across devices/users.
 *   • OFFLINE — no backend (e.g. the design preview, or the file opened directly).
 *               Everything falls back to localStorage so the app still works for
 *               design review. A "guest" profile is auto-created.
 *
 * The app should call `await CourseAPI.boot()` once on mount, then read
 * CourseAPI.user / CourseAPI.mode.
 */
(function () {
  var BASE = ""; // same origin — backend serves the static files too
  var TOKEN_KEY = "cb_token";
  var LOCAL_USER_KEY = "cb_local_user";
  var LOCAL_DATA_KEY = "cb_local_data";

  var API = {
    mode: null, // 'online' | 'offline'
    user: null,
    token: null,
  };

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
  }
  function setToken(t) {
    try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (e) {}
    API.token = t || null;
  }

  async function req(method, path, body) {
    var headers = { "Content-Type": "application/json" };
    var tk = getToken();
    if (tk) headers["Authorization"] = "Bearer " + tk;
    var res = await fetch(BASE + path, {
      method: method,
      headers: headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    var data = null;
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      var msg = (data && (data.detail || data.message)) || ("שגיאה " + res.status);
      var err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ---- local (offline) helpers -------------------------------------------
  function localUser() {
    try {
      var raw = localStorage.getItem(LOCAL_USER_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) {}
    return null;
  }
  function saveLocalUser(u) {
    try { localStorage.setItem(LOCAL_USER_KEY, JSON.stringify(u)); } catch (e) {}
  }
  function ensureGuest() {
    var u = localUser();
    if (!u) {
      u = { id: 0, username: "guest", displayName: "אורח/ת", avatar: "spark",
            role: "student", status: "approved", points: 0, completion: 0,
            streak: 0, achievements: [] };
      saveLocalUser(u);
    }
    return u;
  }

  // ---- detection ----------------------------------------------------------
  API.boot = async function () {
    // Probe the backend once. Only treat as ONLINE when /api/health returns a
    // real JSON {ok:true} — a static host that answers unknown paths with a 200
    // HTML shell (common in previews) must NOT be mistaken for a live backend.
    try {
      var ctrl = new AbortController();
      var t = setTimeout(function () { ctrl.abort(); }, 2500);
      var res = await fetch(BASE + "/api/health", {
        signal: ctrl.signal,
        headers: { "Accept": "application/json" },
      });
      clearTimeout(t);
      var payload = null;
      try { payload = await res.json(); } catch (e) { payload = null; }
      if (res.ok && payload && payload.ok === true) {
        API.mode = "online";
        if (typeof payload.signupOpen === "boolean") API.signupOpen = payload.signupOpen;
        API.chatServer = payload.chat === true;
        API.googleClientId = payload.googleClientId || null;
        API.googleOnly = payload.googleOnly === true;
        API.defaultCourseId = payload.defaultCourseId || null;
      } else {
        API.mode = "offline";
      }
    } catch (e) {
      API.mode = "offline";
    }

    if (API.mode === "online") {
      API.token = getToken();
      if (API.token) {
        try {
          API.user = await req("GET", "/api/me");
          var mc = (API.user && API.user.courses) || [];
          API.courseId = mc.length ? mc[0].id : API.defaultCourseId;
        } catch (e) {
          setToken(null);
          API.user = null;
        }
      }
    } else {
      // offline: auto guest so the app renders in preview
      API.user = ensureGuest();
    }
    return API.user;
  };

  API.isOnline = function () { return API.mode === "online"; };
  API.isLoggedIn = function () { return !!API.user; };
  API.isAdmin = function () { return !!API.user && API.user.role === "admin"; };
  API.signupOpen = true;
  API.chatServer = false;

  // ---- auth ---------------------------------------------------------------
  API.register = async function (payload) {
    if (API.mode === "offline") {
      var u = { id: 0, username: payload.username, displayName: payload.displayName,
                avatar: payload.avatar || "spark", role: "student", status: "approved",
                points: 0, completion: 0, streak: 0, achievements: [] };
      saveLocalUser(u);
      return { ok: true, status: "approved", offline: true,
               message: "מצב תצוגה — החשבון נשמר מקומית." };
    }
    return req("POST", "/api/register", payload);
  };

  API.login = async function (payload) {
    if (API.mode === "offline") {
      var u = localUser() || ensureGuest();
      u.displayName = u.displayName || payload.username;
      saveLocalUser(u);
      API.user = u;
      return { user: u, offline: true };
    }
    var r = await req("POST", "/api/login", payload);
    setToken(r.token);
    API.user = r.user;
    return r;
  };

  API.logout = function () {
    setToken(null);
    API.user = null;
  };

  // ---- Google Sign-In -----------------------------------------------------
  // googleClientId is discovered in boot() from /api/health; null = disabled.
  API.googleClientId = null;

  // Sign in with a Google ID token. Resolves to {token,user} (logged in) or
  // {status:'unlinked', email, name} — the caller then offers claim / create.
  API.googleAuth = async function (payload) {
    if (API.mode === "offline") return { offline: true };
    var r = await req("POST", "/api/auth/google", payload);
    if (r && r.token) { setToken(r.token); API.user = r.user; }
    return r;
  };

  // One-time link of an existing account to this Google account.
  API.googleClaim = async function (payload) {
    if (API.mode === "offline") return { offline: true };
    var r = await req("POST", "/api/auth/google/claim", payload);
    if (r && r.token) { setToken(r.token); API.user = r.user; }
    return r;
  };

  // Brand-new student signing up via Google (lands pending).
  API.googleCreate = async function (payload) {
    if (API.mode === "offline") return { offline: true };
    return req("POST", "/api/auth/google/create", payload);
  };

  API.updateProfile = async function (payload) {
    if (API.mode === "offline") {
      var u = localUser() || ensureGuest();
      Object.assign(u, payload);
      saveLocalUser(u);
      API.user = u;
      return u;
    }
    var r = await req("PUT", "/api/me", payload);
    API.user = Object.assign({}, API.user, r);
    return r;
  };

  // ---- per-user data ------------------------------------------------------
  API.getData = async function () {
    if (API.mode === "offline") {
      try {
        var raw = localStorage.getItem(LOCAL_DATA_KEY);
        var u = localUser() || ensureGuest();
        return { data: raw ? JSON.parse(raw) : {}, points: u.points || 0,
                 completion: u.completion || 0, streak: u.streak || 0,
                 achievements: u.achievements || [] };
      } catch (e) { return { data: {}, points: 0, completion: 0, streak: 0, achievements: [] }; }
    }
    return req("GET", "/api/data");
  };

  // Debounced save so rapid state changes don't spam the server.
  var _saveTimer = null;
  var _pending = null;
  API.saveData = function (payload) {
    _pending = payload;
    if (_saveTimer) clearTimeout(_saveTimer);
    _saveTimer = setTimeout(function () { API.flush(); }, 700);
  };
  API.flush = async function () {
    if (!_pending) return;
    var payload = _pending;
    _pending = null;
    if (API.mode === "offline") {
      try {
        localStorage.setItem(LOCAL_DATA_KEY, JSON.stringify(payload.data || {}));
        var u = localUser() || ensureGuest();
        if (payload.points != null) u.points = payload.points;
        if (payload.completion != null) u.completion = payload.completion;
        if (payload.streak != null) u.streak = payload.streak;
        if (payload.achievements != null) u.achievements = payload.achievements;
        saveLocalUser(u);
        API.user = u;
      } catch (e) {}
      return;
    }
    try { await req("PUT", "/api/data", payload); } catch (e) { /* keep local UI */ }
  };

  // ---- LLM chat proxy -----------------------------------------------------
  // Online: POST to the backend, which calls Anthropic with the server-side key.
  // Offline (preview): return null so the caller falls back to window.claude.
  API.chat = async function (payload) {
    if (API.mode !== "online") return null;
    if (!payload.courseId && API.courseId) payload = Object.assign({}, payload, { courseId: API.courseId });
    return req("POST", "/api/chat", payload);
  };

  // ---- courses / enrolment / content / costs -------------------------------
  // API.courseId = the course the current session works in (first membership, or the
  // deployment default). Set in boot() once /api/me returns memberships.
  API.courseId = null;
  API.defaultCourseId = null;
  API.myCourses = function () { return (API.user && API.user.courses) || []; };
  API.isTeacher = function (cid) {
    if (API.isAdmin()) return true;
    return API.myCourses().some(function (c) { return c.role === "teacher" && (!cid || c.id === cid); });
  };
  API.courses = function () {
    if (API.mode === "offline") return Promise.resolve({ courses: [{ id: "ai-arch", name: "AI Solutions Architect", teacherName: "רילי", accent: "#98b1ff", joinCode: "DEMO42", budgetUsd: 40, spentUsd: 6.4, students: 24, teachers: 1, published: 16, role: "admin", active: true }], defaultCourseId: "ai-arch" });
    return req("GET", "/api/courses");
  };
  API.createCourse = function (payload) { return req("POST", "/api/courses", payload); };
  API.updateCourse = function (id, payload) { return req("PUT", "/api/courses/" + encodeURIComponent(id), payload); };
  API.deleteCourse = function (id) { return req("DELETE", "/api/courses/" + encodeURIComponent(id)); };
  API.rotateCode = function (id) { return req("POST", "/api/courses/" + encodeURIComponent(id) + "/code/rotate"); };
  API.join = function (code) {
    if (API.mode === "offline") return Promise.resolve({ ok: true, offline: true });
    return req("POST", "/api/join", { code: code });
  };
  API.members = function (id) { return req("GET", "/api/courses/" + encodeURIComponent(id) + "/members"); };
  API.setMember = function (id, userId, role) { return req("POST", "/api/courses/" + encodeURIComponent(id) + "/members", { userId: userId, role: role }); };
  API.removeMember = function (id, userId) { return req("DELETE", "/api/courses/" + encodeURIComponent(id) + "/members/" + userId); };
  // Published lesson content (server is the source of truth once seeded).
  API.published = function (id) {
    if (API.mode === "offline") return Promise.resolve(null);
    return req("GET", "/api/courses/" + encodeURIComponent(id || API.courseId || API.defaultCourseId) + "/published");
  };
  // One-time seed of the DB from the static course-data.js (admin).
  API.importCourse = function (id, payload) { return req("POST", "/api/courses/" + encodeURIComponent(id) + "/import", payload); };
  API.costs = function (id, month) {
    if (API.mode === "offline") return Promise.resolve({ courseId: id, month: "2026-09", budgetUsd: 40, totalUsd: 6.4, calls: 212, tokensIn: 1420000, tokensOut: 96000, pct: 16, perModel: [{ model: "claude-sonnet-4-5", usd: 6.1, calls: 190 }, { model: "claude-haiku-4-5", usd: 0.3, calls: 22 }], perKind: [{ kind: "chat", usd: 6.4, calls: 212 }], perUser: [{ displayName: "מאיה כהן", avatar: "leaf", usd: 2.1, calls: 70 }, { displayName: "דני לוי", avatar: "wave", usd: 1.3, calls: 44 }], daily: [] });
    return req("GET", "/api/courses/" + encodeURIComponent(id) + "/costs" + (month ? "?month=" + month : ""));
  };
  API.adminCosts = function (month) { return req("GET", "/api/admin/costs" + (month ? "?month=" + month : "")); };
  API.backups = function () {
    if (API.mode === "offline") return Promise.resolve({ count: 3, files: [] });
    return req("GET", "/api/admin/backups");
  };
  API.backupNow = function () { return req("POST", "/api/admin/backups"); };

  // ---- pilot: student feedback + active time --------------------------------
  API.sendFeedback = function (p) { if (API.mode !== "online") return Promise.resolve({ ok: true, offline: true }); return req("POST", "/api/feedback", p); };
  API.usagePing = function (p) { if (API.mode !== "online") return Promise.resolve({ ok: true, offline: true }); return req("POST", "/api/usage/ping", p); };
  API.pilot = function (courseId, cohortId, start) { return req("GET", "/api/pilot?courseId=" + encodeURIComponent(courseId) + "&cohortId=" + encodeURIComponent(cohortId || "") + "&start=" + encodeURIComponent(start || "")); };

  // ---- platform (Course Brain Platform live mode) --------------------------
  API.platform = function (month) { return req("GET", "/api/platform" + (month ? "?month=" + month : "")); };
  API.createCohort = function (cid, payload) { return req("POST", "/api/courses/" + encodeURIComponent(cid) + "/cohorts", payload); };
  API.updateCohort = function (cid, coid, payload) { return req("PUT", "/api/courses/" + encodeURIComponent(cid) + "/cohorts/" + encodeURIComponent(coid), payload); };
  API.deleteCohort = function (cid, coid) { return req("DELETE", "/api/courses/" + encodeURIComponent(cid) + "/cohorts/" + encodeURIComponent(coid)); };
  API.rotateCohortCode = function (cid, coid) { return req("POST", "/api/courses/" + encodeURIComponent(cid) + "/cohorts/" + encodeURIComponent(coid) + "/code/rotate"); };
  API.setRole = function (cid, uid, role, cohortId) { return req("PUT", "/api/courses/" + encodeURIComponent(cid) + "/members/" + uid + "/role", { role: role, cohortId: cohortId || null }); };
  API.moveCohort = function (cid, uid, cohortId) { return req("PUT", "/api/courses/" + encodeURIComponent(cid) + "/members/" + uid + "/cohort", { role: "student", cohortId: cohortId }); };
  API.userApprove = function (uid) { return req("POST", "/api/users/" + uid + "/approve"); };
  API.userReject = function (uid) { return req("POST", "/api/users/" + uid + "/reject"); };
  API.userDisable = function (uid) { return req("POST", "/api/users/" + uid + "/disable"); };
  API.userEnable = function (uid) { return req("POST", "/api/users/" + uid + "/enable"); };
  API.userTempPassword = function (uid) { return req("POST", "/api/users/" + uid + "/temp-password"); };
  API.addMaterial = function (cid, payload) { return req("POST", "/api/courses/" + encodeURIComponent(cid) + "/materials", payload); };
  API.deleteMaterial = function (cid, mid) { return req("DELETE", "/api/courses/" + encodeURIComponent(cid) + "/materials/" + mid); };
  API.settings = function () { return req("GET", "/api/settings"); };
  API.saveSettings = function (patch) { return req("PUT", "/api/settings", { patch: patch }); };
  // pipeline (teacher)
  API.lesson = function (cid, num) { return req("GET", "/api/courses/" + encodeURIComponent(cid) + "/lessons/" + num); };
  API.approveLesson = function (cid, num) { return req("POST", "/api/courses/" + encodeURIComponent(cid) + "/lessons/" + num + "/approve"); };
  API.reworkLesson = function (cid, num, reason, note) { return req("POST", "/api/courses/" + encodeURIComponent(cid) + "/lessons/" + num + "/rework", { reason: reason, note: note || "" }); };
  API.editLesson = function (cid, num, edited) { return req("PUT", "/api/courses/" + encodeURIComponent(cid) + "/lessons/" + num + "/edit", { edited: edited }); };
  API.runLesson = function (cid, num, fromStage) { return req("POST", "/api/courses/" + encodeURIComponent(cid) + "/lessons/" + num + "/run?from_stage=" + (fromStage || "transcript")); };
  API.uploadSources = async function (cid, num, files) {
    var fd = new FormData();
    if (files.srt) fd.append("srt", files.srt);
    if (files.pdf) fd.append("pdf", files.pdf);
    fd.append("date", files.date || ""); fd.append("teacher_notes", files.notes || "");
    var res = await fetch(BASE + "/api/courses/" + encodeURIComponent(cid) + "/lessons/" + num + "/sources", { method: "POST", headers: { Authorization: "Bearer " + getToken() }, body: fd });
    var data = null; try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error((data && data.detail) || ("שגיאה " + res.status));
    return data;
  };

  // ---- admin --------------------------------------------------------------
  API.adminUsers = async function () {
    if (API.mode === "offline") {
      // demo roster for design preview
      return { users: [
        { id: 1, username: "admin", displayName: "מנהל/ת", avatar: "sun", role: "admin",
          status: "approved", points: 0, completion: 0, streak: 0, achievements: [], lastActive: Date.now()/1000,
          opens: 61, activeDays: 22, activeDays30: 14 },
        { id: 2, username: "maya", displayName: "מאיה כהן", avatar: "leaf", role: "student",
          status: "approved", points: 340, completion: 62, streak: 6, achievements: ["first","streak7"], lastActive: Date.now()/1000,
          reviewsLifetime: 148, recallPct: 84, recallRecentPct: 88, recallRecentN: 42, lastReview: Date.now()/1000-86400,
          opens: 47, activeDays: 19, activeDays30: 13,
          weakLessons: [{lesson:"3",total:9,fails:4},{lesson:"5",total:6,fails:2}],
          lessonStatus: {1:"mastered",2:"mastered",3:"review",4:"mastered",5:"review",6:"new"},
          notes: { 1: "הפרדוקס של רילי על אסטרטגיה מול טקטיקה מאוד חיבר לי. לבדוק שוב לפני הפרויקט.", 3: "מודל ה-Hook — לחשוב איך ליישם במוצר שלי." } },
        { id: 3, username: "danny", displayName: "דני לוי", avatar: "wave", role: "student",
          status: "approved", points: 180, completion: 34, streak: 2, achievements: ["first"], lastActive: Date.now()/1000-86400*3,
          reviewsLifetime: 53, recallPct: 61, recallRecentPct: 55, recallRecentN: 18, lastReview: Date.now()/1000-86400*3,
          opens: 15, activeDays: 7, activeDays30: 4,
          weakLessons: [{lesson:"2",total:8,fails:5},{lesson:"1",total:7,fails:3},{lesson:"4",total:4,fails:2}],
          lessonStatus: {1:"review",2:"review",3:"new"},
          notes: { 2: "שלושת המבחנים לערך — שווה לזכור למבחן." } },
        { id: 4, username: "noa", displayName: "נועה בר", avatar: "spark", role: "student",
          status: "pending", points: 0, completion: 0, streak: 0, achievements: [], createdAt: Date.now()/1000 },
      ] };
    }
    return req("GET", "/api/admin/users");
  };
  API.adminApprove = function (id) {
    if (API.mode === "offline") return Promise.resolve({ ok: true });
    return req("POST", "/api/admin/users/" + id + "/approve");
  };
  API.adminReject = function (id) {
    if (API.mode === "offline") return Promise.resolve({ ok: true });
    return req("POST", "/api/admin/users/" + id + "/reject");
  };
  API.adminDelete = function (id) {
    if (API.mode === "offline") return Promise.resolve({ ok: true });
    return req("DELETE", "/api/admin/users/" + id);
  };

  // ---- project gallery (shared class-wide feed) --------------------------
  var GALLERY_KEY = "cb_gallery_v1";
  function localGallery() {
    try {
      var raw = localStorage.getItem(GALLERY_KEY);
      var list = raw ? JSON.parse(raw) : null;
      if (Array.isArray(list)) return list;
    } catch (e) {}
    var now = Date.now();
    var seed = [
      { id: "seed1", authorId: -2, authorName: "מאיה כהן", authorAvatar: "leaf",
        title: "מתכנן ארוחות עם AI",
        body: "בניתי Artifact שמקבל מה שיש במקרר ומחזיר תפריט שבועי + רשימת קניות. השתמשתי ב-MoSCoW כדי לחתוך פיצ׳רים — עזר בטירוף למקד.",
        link: "", ts: now - 3600000 * 20, kudos: [-3, -4],
        comments: [{ id: "c1", authorId: -3, authorName: "דני לוי", authorAvatar: "wave",
          text: "מגניב! איך פתרת את הקלט של כמויות?", ts: now - 3600000 * 18 }] },
      { id: "seed2", authorId: -3, authorName: "דני לוי", authorAvatar: "wave",
        title: "בוט תמיכה ל-PRD",
        body: "ניסיתי את מודל ה-Instructions מול Prompt מהשיעור האחרון — הפרדתי את ההנחיות הקבועות מהשיחה וזה שינה לגמרי את העקביות של התשובות.",
        link: "", ts: now - 3600000 * 44, kudos: [-2], comments: [] },
    ];
    try { localStorage.setItem(GALLERY_KEY, JSON.stringify(seed)); } catch (e) {}
    return seed;
  }
  function saveLocalGallery(list) {
    try { localStorage.setItem(GALLERY_KEY, JSON.stringify(list)); } catch (e) {}
    return list;
  }
  function localMe() {
    var u = API.user || localUser() || ensureGuest();
    return { id: u.id, name: u.displayName, avatar: u.avatar || "spark" };
  }
  function newId() { return "p" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5); }

  API.galleryList = async function () {
    if (API.mode === "offline") return { posts: localGallery() };
    return req("GET", "/api/gallery");
  };
  API.galleryCreate = async function (payload) {
    if (API.mode === "offline") {
      var me = localMe();
      var post = { id: newId(), authorId: me.id, authorName: me.name, authorAvatar: me.avatar,
        title: (payload.title || "").trim(), body: (payload.body || "").trim(),
        link: (payload.link || "").trim(), ts: Date.now(), kudos: [], comments: [] };
      saveLocalGallery([post].concat(localGallery()));
      return post;
    }
    return req("POST", "/api/gallery", payload);
  };
  API.galleryDelete = async function (id) {
    if (API.mode === "offline") {
      saveLocalGallery(localGallery().filter(function (p) { return p.id !== id; }));
      return { ok: true };
    }
    return req("DELETE", "/api/gallery/" + id);
  };
  API.galleryKudos = async function (id) {
    if (API.mode === "offline") {
      var me = localMe();
      var list = localGallery().map(function (p) {
        if (p.id !== id) return p;
        var k = Array.isArray(p.kudos) ? p.kudos.slice() : [];
        var i = k.indexOf(me.id);
        if (i >= 0) k.splice(i, 1); else k.push(me.id);
        return Object.assign({}, p, { kudos: k });
      });
      saveLocalGallery(list);
      return list.filter(function (p) { return p.id === id; })[0];
    }
    return req("POST", "/api/gallery/" + id + "/kudos");
  };
  API.galleryComment = async function (id, text) {
    if (API.mode === "offline") {
      var me = localMe();
      var cm = { id: "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 4),
        authorId: me.id, authorName: me.name, authorAvatar: me.avatar, text: text, ts: Date.now() };
      var list = localGallery().map(function (p) {
        return p.id === id ? Object.assign({}, p, { comments: (p.comments || []).concat([cm]) }) : p;
      });
      saveLocalGallery(list);
      return list.filter(function (p) { return p.id === id; })[0];
    }
    return req("POST", "/api/gallery/" + id + "/comments", { text: text });
  };
  API.galleryCommentDelete = async function (pid, cid) {
    if (API.mode === "offline") {
      var list = localGallery().map(function (p) {
        return p.id === pid ? Object.assign({}, p, {
          comments: (p.comments || []).filter(function (c) { return c.id !== cid; }) }) : p;
      });
      saveLocalGallery(list);
      return list.filter(function (p) { return p.id === pid; })[0];
    }
    return req("DELETE", "/api/gallery/" + pid + "/comments/" + cid);
  };

  // Redirect helper — call from the app if not logged in.
  API.gotoLogin = function () {
    if (location.pathname.indexOf("Login") === -1) {
      location.href = "Login.dc.html";
    }
  };

  window.CourseAPI = API;
})();
