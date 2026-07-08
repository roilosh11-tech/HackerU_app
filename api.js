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
    // Probe the backend once.
    try {
      var ctrl = new AbortController();
      var t = setTimeout(function () { ctrl.abort(); }, 2500);
      var res = await fetch(BASE + "/api/health", { signal: ctrl.signal });
      clearTimeout(t);
      if (res.ok) {
        API.mode = "online";
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

  // ---- admin --------------------------------------------------------------
  API.adminUsers = async function () {
    if (API.mode === "offline") {
      // demo roster for design preview
      return { users: [
        { id: 1, username: "admin", displayName: "מנהל/ת", avatar: "sun", role: "admin",
          status: "approved", points: 0, completion: 0, streak: 0, achievements: [], lastActive: Date.now()/1000 },
        { id: 2, username: "maya", displayName: "מאיה כהן", avatar: "leaf", role: "student",
          status: "approved", points: 340, completion: 62, streak: 6, achievements: ["first","streak7"], lastActive: Date.now()/1000,
          notes: { 1: "הפרדוקס של רילי על אסטרטגיה מול טקטיקה מאוד חיבר לי. לבדוק שוב לפני הפרויקט.", 3: "מודל ה-Hook — לחשוב איך ליישם במוצר שלי." } },
        { id: 3, username: "danny", displayName: "דני לוי", avatar: "wave", role: "student",
          status: "approved", points: 180, completion: 34, streak: 2, achievements: ["first"], lastActive: Date.now()/1000-86400*3,
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

  // Redirect helper — call from the app if not logged in.
  API.gotoLogin = function () {
    if (location.pathname.indexOf("Login") === -1) {
      location.href = "Login.dc.html";
    }
  };

  window.CourseAPI = API;
})();
