(function () {
  "use strict";

  const tokenKey = "vibebot_token";
  const themeKey = "vibebot_theme";
  const stateKey = "vibebot_ui_state";

  const state = {
    token: localStorage.getItem(tokenKey) || "",
    view: "chat",
    networks: [],            // [{name, host, port, tls, connected, channels:[…declared]}]
    joined: {},              // {netName: {chanName: [users]}}
    buffers: new Map(),      // key = `${net}\u0001${target}` → [{ts,kind,nick,body,self}]
    open: new Set(),         // expanded network groups
    activeNet: null,
    activeTarget: null,
    unread: {},              // {key: count}
    nicks: {},               // {netName: ownNick}
    ws: null,
    bufferLimit: 500,
    hydrated: new Set(),     // bufKeys whose history has been fetched
    pendingWhois: new Map(), // `${net}\u0001${nickLower}` → placeholder line id
    topics: {},              // {bufKey: {topic, by, set_at}}
  };

  /* ---------------- persistence ---------------- */
  try {
    const saved = JSON.parse(localStorage.getItem(stateKey) || "{}");
    if (saved.activeNet) state.activeNet = saved.activeNet;
    if (saved.activeTarget) state.activeTarget = saved.activeTarget;
    if (Array.isArray(saved.open)) state.open = new Set(saved.open);
  } catch (_) {}

  function persistState() {
    localStorage.setItem(stateKey, JSON.stringify({
      activeNet: state.activeNet,
      activeTarget: state.activeTarget,
      open: Array.from(state.open),
    }));
  }

  /* ---------------- helpers ---------------- */
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  const escapeAttr = escapeHtml;
  const bufKey = (net, target) => `${net}\u0001${target}`;
  const isChannel = (t) => typeof t === "string" && /^[#&!+]/.test(t);
  const fmtTime = (d) => {
    const dt = d instanceof Date ? d : new Date(d || Date.now());
    return dt.toTimeString().slice(0, 5);
  };
  // CTCP ACTION wraps body in \x01ACTION ...\x01. Returns inner text or null.
  function parseAction(message) {
    if (typeof message !== "string") return null;
    const m = message.match(/^\x01ACTION (.*)\x01$/);
    return m ? m[1] : null;
  }
  function nickHash(nick) {
    let h = 0;
    for (let i = 0; i < nick.length; i++) h = (h * 31 + nick.charCodeAt(i)) | 0;
    return (Math.abs(h) % 8) + 1;
  }
  function elt(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function nickSpan(nick) {
    const s = elt("span", "ev-nick h-" + nickHash(nick || ""), nick || "");
    return s;
  }
  function maskSpan(ident, host) {
    if (!ident && !host) return null;
    if ((ident === "*" || !ident) && (host === "*" || !host)) return null;
    return elt("span", "ev-mask", `${ident || "*"}@${host || "*"}`);
  }
  function chanSpan(chan) {
    return elt("span", "ev-chan", chan || "");
  }
  function reasonSpan(text) {
    return elt("span", "ev-reason", `(${text})`);
  }
  function appendTextAndSpaces(parent, parts) {
    // parts: [string | Node] — joins with single spaces, skipping null/empty.
    let first = true;
    for (const p of parts) {
      if (p == null || p === "") continue;
      if (!first) parent.appendChild(document.createTextNode(" "));
      if (typeof p === "string") parent.appendChild(document.createTextNode(p));
      else parent.appendChild(p);
      first = false;
    }
  }

  // Render structured event body from a history/live line. Returns a span element.
  function buildEventBody(l) {
    const body = elt("span", "body");
    const ev = l.event;
    if (ev === "join") {
      appendTextAndSpaces(body, [
        nickSpan(l.user || l.nick || ""),
        maskSpan(l.ident, l.host),
        elt("span", "ev-verb", "joined"),
        chanSpan(l.channel),
      ]);
      return body;
    }
    if (ev === "part") {
      appendTextAndSpaces(body, [
        nickSpan(l.user || l.nick || ""),
        maskSpan(l.ident, l.host),
        elt("span", "ev-verb", "left"),
        chanSpan(l.channel),
        l.reason ? reasonSpan(l.reason) : null,
      ]);
      return body;
    }
    if (ev === "quit") {
      appendTextAndSpaces(body, [
        nickSpan(l.user || l.nick || ""),
        maskSpan(l.ident, l.host),
        elt("span", "ev-verb", "quit"),
        l.reason ? reasonSpan(l.reason) : null,
      ]);
      return body;
    }
    if (ev === "kick") {
      const parts = [
        nickSpan(l.target || ""),
        elt("span", "ev-verb", "kicked from"),
        chanSpan(l.channel),
      ];
      if (l.by) {
        parts.push(elt("span", "ev-verb", "by"));
        parts.push(nickSpan(l.by));
      }
      if (l.reason) parts.push(reasonSpan(l.reason));
      appendTextAndSpaces(body, parts);
      return body;
    }
    if (ev === "mode") {
      const flags = (l.modes || []).map((x) => String(x)).join(" ");
      const parts = [
        elt("span", "ev-verb", "mode"),
        chanSpan(l.channel),
        flags ? elt("span", "ev-flags", flags) : null,
      ];
      if (l.by) {
        parts.push(elt("span", "ev-verb", "by"));
        parts.push(nickSpan(l.by));
      }
      appendTextAndSpaces(body, parts);
      return body;
    }
    if (ev === "nick") {
      appendTextAndSpaces(body, [
        nickSpan(l.old || ""),
        elt("span", "ev-verb", "is now known as"),
        nickSpan(l.new || ""),
      ]);
      return body;
    }
    if (ev === "topic") {
      const parts = [elt("span", "ev-verb", "topic")];
      if (l.by) {
        parts.push(elt("span", "ev-verb", "by"));
        parts.push(nickSpan(l.by));
      }
      body.appendChild(parts[0]);
      for (let i = 1; i < parts.length; i++) {
        body.appendChild(document.createTextNode(" "));
        body.appendChild(parts[i]);
      }
      body.appendChild(document.createTextNode(": "));
      body.appendChild(elt("span", "ev-topic", l.topic || "(cleared)"));
      return body;
    }
    // Fallback to plain body text for unknown events.
    body.textContent = l.body || "";
    return body;
  }
  function setStatus(msg, kind) {
    const el = $("footer-status");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("ok", "err");
    if (kind) el.classList.add(kind);
  }

  /* ---------------- API ---------------- */
  async function api(path, init) {
    init = init || {};
    init.headers = Object.assign({}, init.headers || {});
    if (state.token) init.headers.Authorization = "Bearer " + state.token;
    if (init.body && typeof init.body !== "string") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(init.body);
    }
    const r = await fetch(path, init);
    if (r.status === 401) {
      setStatus("401 unauthorized — set a token", "err");
      const err = new Error("unauthorized");
      err.status = 401; err.path = path; err.body = "";
      throw err;
    }
    if (!r.ok) {
      const text = await r.text();
      // FastAPI default body: {"detail":"..."} — surface the inner detail when
      // possible; otherwise hand back the raw payload verbatim so callers can
      // render full multiline errors.
      let detail = text;
      try {
        const j = JSON.parse(text);
        if (j && typeof j.detail === "string") detail = j.detail;
        else if (j && j.detail) detail = JSON.stringify(j.detail, null, 2);
      } catch (_) {}
      const err = new Error(detail || `${path} → ${r.status}`);
      err.status = r.status; err.path = path; err.body = detail;
      throw err;
    }
    if (r.status === 204) return null;
    const ct = r.headers.get("Content-Type") || "";
    return ct.includes("json") ? r.json() : r.text();
  }

  /* ---------------- theme ---------------- */
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem(themeKey, t);
  }
  function toggleTheme() {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    applyTheme(cur === "dark" ? "light" : "dark");
  }

  /* ---------------- view tabs ---------------- */
  function setView(name) {
    state.view = name;
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === name));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === name));
    if (name === "modules" || name === "repos" || name === "acl") refreshAdminPanels();
  }

  /* ---------------- auth ---------------- */
  function updateAuthBadge() {
    const wrap = $("auth-status");
    const label = $("auth-label");
    if (!wrap || !label) return;
    wrap.classList.remove("ok", "err");
    if (state.token) { label.textContent = "token set"; wrap.classList.add("ok"); }
    else label.textContent = "no token";
  }
  function openTokenDialog() {
    const dlg = $("token-dialog");
    dlg.querySelector("input[name=token]").value = state.token;
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  }
  function closeTokenDialog() {
    const dlg = $("token-dialog");
    if (typeof dlg.close === "function") dlg.close();
    else dlg.removeAttribute("open");
  }

  /* ---------------- buffers ---------------- */
  function getBuffer(net, target) {
    const key = bufKey(net, target);
    let buf = state.buffers.get(key);
    if (!buf) { buf = []; state.buffers.set(key, buf); }
    return buf;
  }
  function removeLineById(id) {
    if (!id) return;
    for (const buf of state.buffers.values()) {
      const idx = buf.findIndex((l) => l.id === id);
      if (idx !== -1) buf.splice(idx, 1);
    }
    const log = $("buffer-log");
    if (log) {
      const el = log.querySelector(`[data-line-id="${CSS.escape(id)}"]`);
      if (el) el.remove();
    }
  }

  function pushLine(net, target, line) {
    const buf = getBuffer(net, target);
    line.ts = line.ts || new Date();
    buf.push(line);
    if (buf.length > state.bufferLimit) buf.splice(0, buf.length - state.bufferLimit);
    if (state.activeNet === net && state.activeTarget === target) {
      appendLineDom(line);
      scrollBufferToBottom();
    } else {
      const k = bufKey(net, target);
      state.unread[k] = (state.unread[k] || 0) + (line.kind === "msg" || line.kind === "action" ? 1 : 0);
      renderTree();
    }
  }

  /* ---------------- network tree render ---------------- */
  function renderTree() {
    const root = $("network-tree");
    if (!root) return;
    if (state.networks.length === 0) {
      root.innerHTML = `<div class="rail-empty">no networks loaded</div>`;
      return;
    }
    const parts = state.networks.map((n) => {
      const open = state.open.has(n.name);
      const declared = new Set(n.channels || []);
      const joined = state.joined[n.name] ? Object.keys(state.joined[n.name]) : [];
      joined.forEach((c) => declared.add(c));
      // Also surface query buffers (non-channel targets we have buffers for)
      for (const key of state.buffers.keys()) {
        const [bn, bt] = key.split("\u0001");
        if (bn === n.name) declared.add(bt);
      }
      const chans = Array.from(declared).sort((a, b) => {
        const ac = isChannel(a), bc = isChannel(b);
        if (ac !== bc) return ac ? -1 : 1;
        return a.localeCompare(b);
      });
      const statusCls = n.connected ? "connected" : "failed";
      const channelHtml = chans.map((c) => {
        const k = bufKey(n.name, c);
        const unread = state.unread[k] || 0;
        const active = (state.activeNet === n.name && state.activeTarget === c) ? "is-active" : "";
        const prefix = isChannel(c) ? c[0] : "@";
        const name = isChannel(c) ? c.slice(1) : c;
        return `<div class="ch-row ${active}" data-net="${escapeAttr(n.name)}" data-target="${escapeAttr(c)}">
          <span class="ch-prefix">${escapeHtml(prefix)}</span>
          <span class="ch-name">${escapeHtml(name)}</span>
          <span class="ch-badge" ${unread ? "" : "hidden"}>${unread}</span>
        </div>`;
      }).join("") || `<div class="rail-empty">no channels</div>`;
      return `<div class="net-group ${open ? "is-open" : ""}" data-net="${escapeAttr(n.name)}">
        <div class="net-row" data-action="toggle-net">
          <span class="net-caret">▶</span>
          <span class="net-name">${escapeHtml(n.name)}</span>
          <span class="net-status ${statusCls}" title="${n.connected ? "connected" : "offline"}"></span>
        </div>
        <div class="net-channels">${channelHtml}</div>
      </div>`;
    });
    root.innerHTML = parts.join("");
  }

  /* ---------------- buffer DOM render ---------------- */
  function renderActiveBuffer() {
    const log = $("buffer-log");
    const composer = $("composer-input");
    const sendBtn = document.querySelector(".composer-send");
    const netLabel = $("active-net");
    const chLabel = $("active-ch");
    const meta = $("active-meta");
    const prompt = $("composer-prompt");

    if (!state.activeNet || !state.activeTarget) {
      netLabel.textContent = "—";
      chLabel.textContent = "select a channel";
      meta.textContent = "";
      composer.disabled = true; sendBtn.disabled = true;
      composer.placeholder = "select a channel first…";
      $("status-net").textContent = "—";
      $("status-ch").textContent = "—";
      $("status-users").textContent = "0";
      log.innerHTML = `<li class="line line-system"><time>--:--</time><span class="kind">*</span><span class="body">Pick a network on the left, then a channel.</span></li>`;
      renderUsers([]);
      hideTopic();
      return;
    }

    netLabel.textContent = state.activeNet;
    chLabel.textContent = state.activeTarget;
    composer.disabled = false; sendBtn.disabled = false;
    composer.placeholder = `message ${state.activeTarget}`;
    prompt.textContent = isChannel(state.activeTarget) ? "›" : "@";
    $("status-net").textContent = state.activeNet;
    $("status-ch").textContent = state.activeTarget;
    renderTopic();

    const buf = getBuffer(state.activeNet, state.activeTarget);
    log.innerHTML = "";
    if (buf.length === 0) {
      log.innerHTML = `<li class="line line-system"><time>${fmtTime()}</time><span class="kind">*</span><span class="body">no messages yet — buffer fills as events arrive.</span></li>`;
    } else {
      const frag = document.createDocumentFragment();
      buf.forEach((l) => frag.appendChild(buildLine(l)));
      log.appendChild(frag);
    }
    scrollBufferToBottom();

    // load users for active channel from server (best-effort)
    loadActiveUsers();

    // mark unread cleared
    delete state.unread[bufKey(state.activeNet, state.activeTarget)];
    renderTree();
    persistState();
  }

  function buildLine(l) {
    const li = document.createElement("li");
    const cls = ["line"];
    if (l.kind === "msg") cls.push(l.self ? "line-msg line-self" : "line-msg");
    else if (l.kind === "action") cls.push("line-action");
    else if (l.kind === "notice") cls.push("line-notice");
    else if (l.kind === "system") cls.push("line-system");
    else if (l.kind === "error") cls.push("line-error");
    else if (l.kind === "whois") cls.push("line-whois");
    else if (l.kind === "help") cls.push("line-help");
    else {
      cls.push("line-event");
      if (l.event) cls.push("line-event-" + l.event);
    }
    li.className = cls.join(" ");
    if (l.id) li.dataset.lineId = l.id;

    const time = document.createElement("time");
    time.textContent = fmtTime(l.ts);
    li.appendChild(time);

    const second = document.createElement("span");
    if (l.kind === "msg" || l.kind === "action" || l.kind === "notice") {
      const prefix = nickPrefixFor(l.nick);
      second.className = "nick h-" + nickHash(l.nick || "");
      second.textContent = (prefix || "") + (l.nick || "");
      second.title = l.nick || "";
    } else if (l.kind === "whois") {
      second.className = "kind whois-glyph";
      second.textContent = "◉";
    } else if (l.kind === "help") {
      second.className = "kind help-glyph";
      second.textContent = "?";
    } else {
      second.className = "kind";
      second.textContent = l.glyph || "*";
    }
    li.appendChild(second);

    if (l.kind === "whois") {
      li.appendChild(buildWhoisCard(l.whois || {}));
    } else if (l.kind === "help") {
      li.appendChild(buildHelpCard());
    } else if (l.kind === "event") {
      li.appendChild(buildEventBody(l));
    } else {
      const body = document.createElement("span");
      body.className = "body";
      body.textContent = l.body || "";
      li.appendChild(body);
    }
    return li;
  }

  function appendLineDom(l) {
    const log = $("buffer-log");
    if (!log) return;
    log.appendChild(buildLine(l));
  }

  function scrollBufferToBottom() {
    const log = $("buffer-log");
    if (!log) return;
    log.scrollTop = log.scrollHeight;
  }

  /* ---------------- topic ---------------- */
  function hideTopic() {
    const row = $("buffer-topic");
    if (row) row.hidden = true;
  }
  function renderTopic() {
    const row = $("buffer-topic");
    if (!row) return;
    if (!state.activeNet || !state.activeTarget || !isChannel(state.activeTarget)) {
      row.hidden = true;
      return;
    }
    const key = bufKey(state.activeNet, state.activeTarget);
    const t = state.topics[key];
    if (!t || !t.topic) {
      row.hidden = true;
      return;
    }
    $("buffer-topic-text").textContent = t.topic;
    const byEl = $("buffer-topic-by");
    byEl.textContent = t.by ? `set by ${t.by}` : "";
    byEl.hidden = !t.by;
    row.hidden = false;
  }
  function setTopic(net, channel, topic, by, setAt) {
    const key = bufKey(net, channel);
    state.topics[key] = { topic: topic || null, by: by || null, set_at: setAt || null };
    if (state.activeNet === net && state.activeTarget === channel) renderTopic();
  }
  async function fetchTopic(net, channel) {
    if (!isChannel(channel)) return;
    try {
      const t = await api(`/api/networks/${encodeURIComponent(net)}/channels/${encodeURIComponent(channel)}/topic`);
      if (t && typeof t === "object") setTopic(net, channel, t.topic, t.by, t.set_at);
    } catch {}
  }

  /* ---------------- users ---------------- */
  // Normalize either ["@alice", …] (legacy) or [{nick,prefix,ident,host}, …]
  // (new shape) into the latter.
  function normalizeUserList(users) {
    return (users || []).map((u) => {
      if (typeof u === "string") {
        let prefix = "";
        if ("@%+".includes(u[0])) { prefix = u[0]; u = u.slice(1); }
        return { nick: u, prefix, ident: "*", host: "*" };
      }
      return {
        nick: u.nick,
        prefix: u.prefix || "",
        ident: u.ident || "*",
        host: u.host || "*",
      };
    });
  }

  // Mode tiers, most → least privileged. Each entry: { prefix, label, css }.
  const USER_TIERS = [
    { prefix: "~", label: "owners",  css: "user-mode-owner" },
    { prefix: "&", label: "admins",  css: "user-mode-admin" },
    { prefix: "@", label: "ops",     css: "user-mode-op"    },
    { prefix: "%", label: "halfops", css: "user-mode-hop"   },
    { prefix: "+", label: "voiced",  css: "user-mode-vc"    },
    { prefix: "",  label: "users",   css: ""                },
  ];

  function renderUsers(users) {
    const list = $("user-list");
    const count = $("user-count");
    if (!list) return;
    const norm = normalizeUserList(users);
    state.activeUsers = norm;
    count.textContent = norm.length;
    $("status-users").textContent = norm.length;
    if (norm.length === 0) {
      list.innerHTML = `<div class="rail-empty">${state.activeTarget && isChannel(state.activeTarget) ? "no users" : "—"}</div>`;
      return;
    }
    const buckets = new Map(USER_TIERS.map((t) => [t.prefix, []]));
    for (const u of norm) {
      const bucket = buckets.has(u.prefix) ? u.prefix : "";
      buckets.get(bucket).push(u);
    }
    const html = USER_TIERS.flatMap((tier) => {
      const items = buckets.get(tier.prefix);
      if (!items || items.length === 0) return [];
      items.sort((a, b) => a.nick.localeCompare(b.nick, undefined, { sensitivity: "base" }));
      const header = `<div class="user-section">${escapeHtml(tier.label)} <span class="user-section-count">${items.length}</span></div>`;
      const rows = items.map((u) => {
        const hue = nickHash(u.nick);
        const mask = `${u.nick}!${u.ident}@${u.host}`;
        const prefix = u.prefix || " ";
        return `<div class="user-row" data-nick="${escapeAttr(u.nick)}" data-mask="${escapeAttr(mask)}" data-prefix="${escapeAttr(u.prefix)}" title="${escapeAttr(mask)}">
          <span class="user-prefix ${tier.css}">${escapeHtml(prefix)}</span>
          <span class="nick h-${hue}">${escapeHtml(u.nick)}</span>
        </div>`;
      }).join("");
      return [header, rows];
    }).join("");
    list.innerHTML = html;
  }

  async function loadActiveUsers() {
    if (!state.activeNet || !state.activeTarget || !isChannel(state.activeTarget)) {
      renderUsers([]);
      return;
    }
    try {
      const list = await api(`/api/networks/${encodeURIComponent(state.activeNet)}/channels/${encodeURIComponent(state.activeTarget)}/users`);
      renderUsers(Array.isArray(list) ? list : []);
    } catch {
      renderUsers([]);
    }
  }

  /* ---------------- selection ---------------- */
  function selectChannel(net, target) {
    state.activeNet = net;
    state.activeTarget = target;
    state.open.add(net);
    renderTree();
    renderActiveBuffer();
    hydrateHistory(net, target);
    if (isChannel(target)) fetchTopic(net, target);
  }

  async function hydrateHistory(net, target) {
    if (!net || !target || !isChannel(target)) return;
    const key = bufKey(net, target);
    if (state.hydrated.has(key)) return;
    state.hydrated.add(key);
    try {
      const lines = await api(`/api/networks/${encodeURIComponent(net)}/channels/${encodeURIComponent(target)}/history`);
      if (!Array.isArray(lines) || lines.length === 0) return;
      const buf = getBuffer(net, target);
      // Merge history at the front while avoiding duplicates of any live lines
      // we may already have collected while the fetch was in flight.
      const liveKeys = new Set(buf.map((l) => `${l.kind}|${l.nick||""}|${l.body||""}`));
      const historical = lines
        .filter((l) => !liveKeys.has(`${l.kind}|${l.nick||""}|${l.body||""}`))
        .map((l) => Object.assign({}, l, { ts: l.ts ? new Date(l.ts) : new Date() }));
      if (historical.length === 0) return;
      buf.splice(0, 0, ...historical);
      if (buf.length > state.bufferLimit) buf.splice(0, buf.length - state.bufferLimit);
      if (state.activeNet === net && state.activeTarget === target) renderActiveBuffer();
    } catch {
      state.hydrated.delete(key);
    }
  }

  /* ---------------- network/channel data load ---------------- */
  function userListToNicks(list) {
    return (list || []).map((u) => (typeof u === "string" ? u : u.nick));
  }

  async function loadNetworks() {
    try {
      const nets = await api("/api/networks");
      state.networks = nets;
      nets.forEach((n) => { if (n.nickname) state.nicks[n.name] = n.nickname; });
      // hydrate joined channels
      await Promise.all(nets.filter((n) => n.connected).map(async (n) => {
        try {
          const chans = await api(`/api/networks/${encodeURIComponent(n.name)}/channels`);
          state.joined[n.name] = {};
          chans.forEach((c) => {
            state.joined[n.name][c.name] = userListToNicks(c.users);
            if (c.topic) setTopic(n.name, c.name, c.topic, c.by, c.set_at);
          });
        } catch {}
      }));
      // pick a sensible default selection if none
      if (!state.activeNet && nets.length > 0) {
        const n0 = nets[0];
        const chans = state.joined[n0.name] ? Object.keys(state.joined[n0.name]) : (n0.channels || []);
        if (chans.length > 0) selectChannel(n0.name, chans[0]);
        else { state.activeNet = n0.name; state.open.add(n0.name); }
      }
      renderTree();
      renderActiveBuffer();
      // Hydrate history for the restored/initial channel (selectChannel only
      // fires on user action, not on load-time restoration).
      if (state.activeNet && state.activeTarget) hydrateHistory(state.activeNet, state.activeTarget);
    } catch (err) {
      setStatus(err.message, "err");
    }
  }

  /* ---------------- admin panels (modules/repos/acl) ---------------- */
  const renderers = {
    module(m) {
      return `<div class="card">
        <h3>${escapeHtml(m.repo)}/${escapeHtml(m.name)}</h3>
        <div class="muted">${escapeHtml(m.description || "(no description)")} — ${m.enabled ? "enabled" : "disabled"}</div>
        <div class="actions">
          <button data-action="${m.enabled ? "disable" : "enable"}" data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">${m.enabled ? "Disable" : "Enable"}</button>
          <button data-action="reload" data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">Reload</button>
          <button data-action="unload" data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">Unload</button>
        </div>
      </div>`;
    },
    repo(r) {
      return `<div class="card">
        <h3>${escapeHtml(r.name)}</h3>
        <div class="muted">${escapeHtml(r.url)} @ ${escapeHtml(r.branch)}${r.enabled ? "" : " (disabled)"}</div>
        <div class="actions">
          <button data-action="pull-repo" data-name="${escapeAttr(r.name)}">Pull</button>
          <button data-action="delete-repo" data-name="${escapeAttr(r.name)}">Delete</button>
        </div>
      </div>`;
    },
    acl(r) {
      return `<div class="card">
        <h3>${escapeHtml(r.mask)}</h3>
        <div class="muted">perm: <code>${escapeHtml(r.permission)}</code>${r.note ? " — " + escapeHtml(r.note) : ""}</div>
        <div class="actions">
          <button data-action="delete-acl" data-id="${r.id}">Delete</button>
        </div>
      </div>`;
    },
  };

  async function loadAdminPanel(el) {
    const tpl = renderers[el.dataset.template];
    if (!tpl) return;
    try {
      const data = await api(el.dataset.api);
      if (!Array.isArray(data) || data.length === 0) {
        el.innerHTML = `<div class="rail-empty">none</div>`;
        return;
      }
      el.innerHTML = `<div class="row">${data.map(tpl).join("")}</div>`;
    } catch (err) {
      el.innerHTML = `<div class="rail-empty">Error: ${escapeHtml(err.message)}</div>`;
    }
  }

  function refreshAdminPanels() {
    document.querySelectorAll("[data-api]").forEach(loadAdminPanel);
  }

  function preserveScroll(fn) {
    const view = document.querySelector(".view.is-active");
    const top = view ? view.scrollTop : 0;
    Promise.resolve(fn()).finally(() => {
      if (view) requestAnimationFrame(() => { view.scrollTop = top; });
    });
  }

  function clearCardExtras(card) {
    card.querySelectorAll(".card-error, .card-ok").forEach((n) => n.remove());
  }

  async function pullRepo(btn) {
    const card = btn.closest(".card");
    if (!card) return;
    clearCardExtras(card);
    const origLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = "pulling…";
    try {
      const res = await api("/api/repos/" + encodeURIComponent(btn.dataset.name) + "/pull", { method: "POST" });
      const ok = document.createElement("div");
      ok.className = "card-ok";
      ok.textContent = res && res.path ? `pulled · ${res.path}` : "pulled OK";
      card.appendChild(ok);
      setStatus(`pulled ${btn.dataset.name}`, "ok");
    } catch (err) {
      const pre = document.createElement("pre");
      pre.className = "card-error";
      pre.textContent = err.body || err.message;
      card.appendChild(pre);
      setStatus(`pull failed: ${btn.dataset.name}`, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = origLabel;
    }
  }

  /* ---------------- user context menu ---------------- */
  function closeUserContextMenu() {
    const m = document.getElementById("user-ctx-menu");
    if (m) m.remove();
    document.removeEventListener("click", _ctxOutside, true);
    document.removeEventListener("keydown", _ctxKey, true);
  }
  function _ctxOutside(ev) {
    if (!ev.target.closest("#user-ctx-menu")) closeUserContextMenu();
  }
  function _ctxKey(ev) {
    if (ev.key === "Escape") closeUserContextMenu();
  }

  function openUserContextMenu(anchor, net, nick, mask, prefix) {
    closeUserContextMenu();
    const channel = state.activeTarget;
    const inChannel = isChannel(channel);
    const isOp     = prefix === "@" || prefix === "&" || prefix === "~";
    const isVoiced = prefix === "+";
    const items = [
      { label: "Query", action: "query" },
      { label: "Whois", action: "whois" },
    ];
    if (inChannel) {
      items.push({ sep: true });
      items.push({ label: isOp ? "Deop" : "Op",          action: isOp ? "deop" : "op" });
      items.push({ label: isVoiced ? "Devoice" : "Voice", action: isVoiced ? "devoice" : "voice" });
      items.push({ sep: true });
      items.push({ label: "Kick…",   action: "kick" });
      items.push({ label: "Ban",     action: "ban" });
      items.push({ label: "Kickban…", action: "kickban" });
    }
    items.push({ sep: true });
    items.push({ label: "Add ACL…", action: "acl" });

    const menu = document.createElement("div");
    menu.id = "user-ctx-menu";
    menu.className = "ctx-menu";
    menu.innerHTML = items.map((it) => it.sep
      ? `<div class="ctx-sep"></div>`
      : `<button type="button" class="ctx-item" data-action="${escapeAttr(it.action)}">${escapeHtml(it.label)}</button>`
    ).join("");
    document.body.appendChild(menu);

    const rect = anchor.getBoundingClientRect();
    const mw = menu.offsetWidth || 180;
    const mh = menu.offsetHeight || 200;
    let left = rect.left - mw - 6;
    if (left < 6) left = rect.right + 6;
    let top = rect.top;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, window.innerHeight - mh - 8);
    menu.style.left = left + "px";
    menu.style.top  = top  + "px";

    menu.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-action]");
      if (!btn) return;
      const action = btn.dataset.action;
      closeUserContextMenu();
      await runUserAction(action, net, channel, nick, mask);
    });
    setTimeout(() => {
      document.addEventListener("click", _ctxOutside, true);
      document.addEventListener("keydown", _ctxKey, true);
    }, 0);
  }

  async function runUserAction(action, net, channel, nick, mask) {
    try {
      switch (action) {
        case "query":
          selectChannel(net, nick);
          return;
        case "acl":
          openUserAclDialog(net, nick, mask);
          return;
        case "whois":
          await api(`/api/networks/${encodeURIComponent(net)}/whois`, { method: "POST", body: { nick } });
          setStatus(`whois ${nick} queued`, "ok");
          return;
        case "kick": {
          const reason = window.prompt(`Kick ${nick} from ${channel} — reason?`, "");
          if (reason === null) return;
          await api(`/api/networks/${encodeURIComponent(net)}/kick`, { method: "POST", body: { channel, nick, reason } });
          setStatus(`kicked ${nick}`, "ok");
          return;
        }
        case "kickban": {
          const reason = window.prompt(`Kickban ${nick} from ${channel} — reason?`, "");
          if (reason === null) return;
          await api(`/api/networks/${encodeURIComponent(net)}/kickban`, { method: "POST", body: { channel, nick, reason } });
          setStatus(`kickbanned ${nick}`, "ok");
          return;
        }
        case "ban":
          await api(`/api/networks/${encodeURIComponent(net)}/ban`, { method: "POST", body: { channel, nick } });
          setStatus(`banned ${nick}`, "ok");
          return;
        case "op": case "deop": case "voice": case "devoice":
          await api(`/api/networks/${encodeURIComponent(net)}/${action}`, { method: "POST", body: { channel, nick } });
          setStatus(`${action} ${nick}`, "ok");
          return;
      }
    } catch (err) {
      setStatus(`${action} ${nick}: ${err.message}`, "err");
    }
  }

  /* ---------------- click-to-ACL dialog ---------------- */
  function openUserAclDialog(net, nick, mask) {
    const dlg = $("user-acl-dialog");
    if (!dlg) return;
    $("user-acl-nick").textContent = nick;
    $("user-acl-net").textContent = net;
    $("user-acl-mask").value = mask;
    const errBox = $("user-acl-error");
    errBox.hidden = true; errBox.textContent = "";
    const form = $("user-acl-form");
    form.elements.permission.value = "";
    form.elements.note.value = "";
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
    setTimeout(() => form.elements.permission.focus(), 0);
  }
  function closeUserAclDialog() {
    const dlg = $("user-acl-dialog");
    if (!dlg) return;
    if (typeof dlg.close === "function") dlg.close();
    else dlg.removeAttribute("open");
  }

  function fmtIdle(sec) {
    const n = Number(sec);
    if (!Number.isFinite(n) || n < 0) return String(sec);
    if (n < 60) return `${n}s`;
    if (n < 3600) {
      const m = Math.floor(n / 60), s = n % 60;
      return s ? `${m}m ${s}s` : `${m}m`;
    }
    if (n < 86400) {
      const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60);
      return m ? `${h}h ${m}m` : `${h}h`;
    }
    const d = Math.floor(n / 86400), h = Math.floor((n % 86400) / 3600);
    return h ? `${d}d ${h}h` : `${d}d`;
  }
  function fmtSignon(ts) {
    const n = Number(ts);
    if (!Number.isFinite(n) || n <= 0) return String(ts);
    const dt = new Date(n * 1000);
    if (Number.isNaN(dt.getTime())) return String(ts);
    const pad = (x) => String(x).padStart(2, "0");
    return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  }
  function buildWhoisCard(p) {
    const card = document.createElement("div");
    card.className = "body whois-card";

    const head = document.createElement("header");
    head.className = "whois-head";
    const label = document.createElement("span");
    label.className = "whois-label";
    label.textContent = "WHOIS";
    head.appendChild(label);
    const nickEl = document.createElement("span");
    nickEl.className = "whois-nick h-" + nickHash(p.nick || "");
    nickEl.textContent = p.nick || "(unknown)";
    head.appendChild(nickEl);
    const badges = document.createElement("span");
    badges.className = "whois-badges";
    const addBadge = (text, cls) => {
      const b = document.createElement("span");
      b.className = "whois-badge " + cls;
      b.textContent = text;
      badges.appendChild(b);
    };
    if (p.oper)       addBadge("oper", "is-oper");
    if (p.secure)     addBadge("secure", "is-secure");
    if (p.identified) addBadge(p.account ? `✓ ${p.account}` : "identified", "is-auth");
    if (p.away)       addBadge("away", "is-away");
    head.appendChild(badges);
    card.appendChild(head);

    const dl = document.createElement("dl");
    dl.className = "whois-rows";
    const addRow = (label, value, valueClass) => {
      if (value === null || value === undefined || value === "") return;
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      if (valueClass) dd.className = valueClass;
      if (typeof value === "string" || typeof value === "number") {
        dd.textContent = String(value);
      } else {
        dd.appendChild(value);
      }
      dl.appendChild(dt);
      dl.appendChild(dd);
    };

    if (p.error) {
      addRow("error", p.error, "whois-error");
      card.appendChild(dl);
      return card;
    }
    if (p.username || p.hostname) addRow("user", `${p.username || "?"}@${p.hostname || "?"}`, "whois-host");
    if (p.realname)  addRow("real", p.realname);
    if (p.account && !p.identified) addRow("account", p.account);
    if (p.server)    addRow("server", p.server + (p.server_info ? ` — ${p.server_info}` : ""));
    if (p.away)      addRow("away", p.away, "whois-away");
    if (p.idle != null)   addRow("idle", fmtIdle(p.idle));
    if (p.signon != null) addRow("signon", fmtSignon(p.signon));
    if (Array.isArray(p.channels) && p.channels.length) {
      const wrap = document.createElement("span");
      wrap.className = "whois-chans";
      p.channels.forEach((c) => {
        const m = /^([~&@%+]*)(.*)$/.exec(String(c)) || [null, "", String(c)];
        const chip = document.createElement("span");
        chip.className = "whois-chan";
        if (m[1]) {
          const pf = document.createElement("span");
          pf.className = "whois-chan-prefix";
          pf.textContent = m[1];
          chip.appendChild(pf);
        }
        const nm = document.createElement("span");
        nm.className = "whois-chan-name";
        nm.textContent = m[2];
        chip.appendChild(nm);
        wrap.appendChild(chip);
      });
      addRow("chans", wrap);
    }
    card.appendChild(dl);
    return card;
  }

  /* ---------------- event routing ---------------- */
  function routeEvent(ev) {
    if (!ev || !ev.kind) return;
    const net = ev.network;
    const p = ev.payload || {};
    switch (ev.kind) {
      case "message": {
        const target = p.target;
        const isPM = !isChannel(target);
        const buffer = isPM ? p.source : target;
        // Drop server echoes of our own outbound. We render outgoing messages
        // optimistically in sendActiveMessage(); the IRC server (or a module
        // rebroadcast) will then echo the same line back under our nick when
        // IRCv3 echo-message is active. With one connection per network, any
        // message bearing our nick IS us — no body or time match needed.
        const ownNick = state.nicks[net];
        if (ownNick && p.source === ownNick) break;
        const body = parseAction(p.message);
        if (body !== null) {
          pushLine(net, buffer, { kind: "action", nick: p.source, body });
        } else {
          pushLine(net, buffer, { kind: "msg", nick: p.source, body: p.message });
        }
        break;
      }
      case "notice": {
        const target = p.target;
        const isPM = !isChannel(target);
        const buffer = isPM ? p.source : target;
        pushLine(net, buffer, { kind: "notice", nick: p.source, body: p.message });
        break;
      }
      case "join":
        if (state.joined[net] && state.joined[net][p.channel]) {
          if (!state.joined[net][p.channel].includes(p.user)) state.joined[net][p.channel].push(p.user);
        }
        pushLine(net, p.channel, {
          kind: "event", event: "join", glyph: "→",
          user: p.user, ident: p.ident, host: p.host, channel: p.channel,
        });
        if (state.activeNet === net && state.activeTarget === p.channel) loadActiveUsers();
        break;
      case "part":
        if (state.joined[net] && state.joined[net][p.channel]) {
          state.joined[net][p.channel] = state.joined[net][p.channel].filter((u) => u !== p.user);
        }
        pushLine(net, p.channel, {
          kind: "event", event: "part", glyph: "←",
          user: p.user, ident: p.ident, host: p.host, channel: p.channel, reason: p.message,
        });
        if (state.activeNet === net && state.activeTarget === p.channel) loadActiveUsers();
        break;
      case "quit": {
        // Broadcast to every channel buffer on this network where the user was present.
        const joined = state.joined[net] || {};
        for (const ch of Object.keys(joined)) {
          if (joined[ch].includes(p.user)) {
            joined[ch] = joined[ch].filter((u) => u !== p.user);
            pushLine(net, ch, {
              kind: "event", event: "quit", glyph: "⤫",
              user: p.user, ident: p.ident, host: p.host, reason: p.message,
            });
            if (state.activeNet === net && state.activeTarget === ch) loadActiveUsers();
          }
        }
        break;
      }
      case "kick":
        pushLine(net, p.channel, {
          kind: "event", event: "kick", glyph: "✕",
          target: p.target, by: p.by, channel: p.channel, reason: p.reason,
        });
        if (state.activeNet === net && state.activeTarget === p.channel) loadActiveUsers();
        break;
      case "nick":
        // broadcast to every buffer of this network the user is in (cheap: log to active)
        for (const key of state.buffers.keys()) {
          const [bn] = key.split("\u0001");
          if (bn === net) pushLine(net, key.split("\u0001")[1], {
            kind: "event", event: "nick", glyph: "↺",
            old: p.old, new: p.new, ident: p.ident, host: p.host,
          });
        }
        break;
      case "connect":
        pushLine(net, "*", { kind: "system", body: `connected to ${net}` });
        loadNetworks();
        break;
      case "host_hidden":
        pushLine(net, "*", { kind: "system", body: `host hidden on ${net}` });
        break;
      case "mode": {
        pushLine(net, p.channel, {
          kind: "event", event: "mode", glyph: "±",
          channel: p.channel, modes: p.modes || [], by: p.by,
        });
        if (state.activeNet === net && state.activeTarget === p.channel) loadActiveUsers();
        break;
      }
      case "names":
        if (state.activeNet === net && state.activeTarget === p.channel) loadActiveUsers();
        break;
      case "topic":
        setTopic(net, p.channel, p.topic, p.by, null);
        // Suppress a history line for the "initial topic" sent on join — it is
        // already shown in the topic bar. Real changes (user-triggered) still
        // log a line so there's an audit trail in the buffer.
        if (!p.initial) {
          pushLine(net, p.channel, {
            kind: "event", event: "topic", glyph: "≡",
            channel: p.channel, topic: p.topic, by: p.by,
          });
        }
        break;
      case "server_reply": {
        const cmd = p.command || "";
        const params = Array.isArray(p.params) ? p.params : [];
        // Most numerics begin with our own nick — drop it so the body reads
        // naturally ("461 PONG Not enough parameters" instead of "... vibebot PONG ...").
        const ownNick = state.nicks[net];
        const rest = (params[0] && ownNick && params[0] === ownNick) ? params.slice(1) : params;
        const body = `${cmd} ${rest.join(" ")}`.trim();
        pushLine(net, "*", { kind: "system", body: `[server] ${body}` });
        break;
      }
      case "whois": {
        const pendKey = `${net}\u0001${(p.nick || "").toLowerCase()}`;
        const pending = state.pendingWhois.get(pendKey);
        if (pending) {
          removeLineById(pending.id);
          state.pendingWhois.delete(pendKey);
        }
        const target = pending
          ? pending.target
          : ((state.activeNet === net && state.activeTarget) ? state.activeTarget : p.nick);
        pushLine(net, target, { kind: "whois", whois: p });
        break;
      }
      default:
        break;
    }
  }

  /* ---------------- websocket ---------------- */
  function wsState(s) {
    const el = $("status-ws");
    if (!el) return;
    el.textContent = s;
    el.classList.toggle("online", s === "online");
    el.classList.toggle("offline", s !== "online");
  }
  function reconnectEvents() {
    if (state.ws) { try { state.ws.close(); } catch {} state.ws = null; }
    if (!state.token) { wsState("offline"); return; }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/events?token=${encodeURIComponent(state.token)}`;
    let sock;
    try { sock = new WebSocket(url); } catch { wsState("offline"); return; }
    state.ws = sock;
    sock.onopen = () => wsState("online");
    sock.onmessage = (m) => {
      let payload = null;
      try { payload = JSON.parse(m.data); } catch { return; }
      // mirror raw to events log
      const log = $("events-log");
      if (log) log.textContent = (m.data + "\n" + log.textContent).slice(0, 16000);
      routeEvent(payload);
    };
    sock.onclose = () => {
      wsState("offline");
      state.ws = null;
      if (state.token) setTimeout(reconnectEvents, 3000);
    };
    sock.onerror = () => wsState("offline");
  }

  /* ---------------- send ---------------- */
  function ownNickFor(net) {
    return state.nicks[net]
      || state.networks.find((n) => n.name === net)?.nickname
      || "(me)";
  }

  function nickPrefixFor(nick) {
    if (!nick || !state.activeUsers) return "";
    const target = nick.toLowerCase();
    const u = state.activeUsers.find((x) => x.nick && x.nick.toLowerCase() === target);
    return (u && u.prefix) || "";
  }

  async function sendActiveMessage(text) {
    if (!state.activeNet || !state.activeTarget) return;
    const net = state.activeNet, target = state.activeTarget;
    if (text.startsWith("//")) {
      // escape: send literal slash message
      return sendPlain(net, target, text.slice(1));
    }
    if (text.startsWith("/")) {
      return runSlashCommand(net, target, text.slice(1));
    }
    return sendPlain(net, target, text);
  }

  async function sendPlain(net, target, text) {
    try {
      await api(`/api/networks/${encodeURIComponent(net)}/send`, {
        method: "POST",
        body: { target, message: text },
      });
      pushLine(net, target, { kind: "msg", self: true, nick: ownNickFor(net), body: text });
      setStatus("sent", "ok");
    } catch (err) {
      pushLine(net, target, { kind: "error", body: err.message });
      setStatus(err.message, "err");
    }
  }

  async function sendAction(net, target, body) {
    try {
      await api(`/api/networks/${encodeURIComponent(net)}/send`, {
        method: "POST",
        body: { target, message: `\x01ACTION ${body}\x01` },
      });
      pushLine(net, target, { kind: "action", self: true, nick: ownNickFor(net), body });
    } catch (err) {
      pushLine(net, target, { kind: "error", body: err.message });
      setStatus(err.message, "err");
    }
  }

  function systemLine(text) {
    if (state.activeNet && state.activeTarget) {
      pushLine(state.activeNet, state.activeTarget, { kind: "system", body: text });
    }
  }
  function errorLine(text) {
    if (state.activeNet && state.activeTarget) {
      pushLine(state.activeNet, state.activeTarget, { kind: "error", body: text });
    }
  }

  const SLASH_HELP_GROUPS = [
    { label: "messaging", items: [
      { cmd: "/me",    args: "<text>",            desc: "emote action" },
      { cmd: "/msg",   args: "<target> <text>",   desc: "send without switching buffer" },
      { cmd: "/query", args: "<nick>",            desc: "open private buffer" },
    ]},
    { label: "channel", items: [
      { cmd: "/join",  args: "<#chan>",           desc: "join channel" },
      { cmd: "/part",  args: "[reason]",          desc: "leave current channel" },
      { cmd: "/topic", args: "[text]",            desc: "get or set topic" },
      { cmd: "/mode",  args: "<flags> [args…]",   desc: "set channel mode" },
    ]},
    { label: "ops", items: [
      { cmd: "/op",      args: "<nick>",          desc: "grant op" },
      { cmd: "/deop",    args: "<nick>",          desc: "revoke op" },
      { cmd: "/voice",   args: "<nick>",          desc: "grant voice" },
      { cmd: "/devoice", args: "<nick>",          desc: "revoke voice" },
      { cmd: "/kick",    args: "<nick> [reason]", desc: "kick user" },
      { cmd: "/ban",     args: "<nick>",          desc: "ban user" },
      { cmd: "/kickban", args: "<nick> [reason]", desc: "kick then ban" },
    ]},
    { label: "meta", items: [
      { cmd: "/nick",  args: "<new>",  desc: "change nickname" },
      { cmd: "/whois", args: "<nick>", desc: "query whois" },
      { cmd: "/raw",   args: "<line>", desc: "send raw IRC line" },
      { cmd: "//text", args: "",       desc: "send literal /-prefixed message" },
      { cmd: "/help",  args: "",       desc: "show this help" },
    ]},
  ];

  function buildHelpCard() {
    const card = document.createElement("div");
    card.className = "body help-card";

    const head = document.createElement("header");
    head.className = "help-head";
    head.appendChild(elt("span", "help-label", "COMMANDS"));
    head.appendChild(elt("span", "help-sub", "type /cmd args · // escapes leading slash"));
    card.appendChild(head);

    const groups = document.createElement("div");
    groups.className = "help-groups";
    for (const g of SLASH_HELP_GROUPS) {
      const col = document.createElement("section");
      col.className = "help-group";
      col.appendChild(elt("h4", "help-group-title", g.label));
      const rows = document.createElement("div");
      rows.className = "help-rows";
      for (const it of g.items) {
        const cell = document.createElement("span");
        cell.className = "help-cmdcell";
        cell.appendChild(elt("span", "help-cmd", it.cmd));
        if (it.args) {
          cell.appendChild(document.createTextNode(" "));
          cell.appendChild(elt("span", "help-args", it.args));
        }
        rows.appendChild(cell);
        rows.appendChild(elt("span", "help-desc", it.desc));
      }
      col.appendChild(rows);
      groups.appendChild(col);
    }
    card.appendChild(groups);
    return card;
  }

  async function runSlashCommand(net, target, raw) {
    const space = raw.indexOf(" ");
    const cmd  = (space === -1 ? raw : raw.slice(0, space)).toLowerCase();
    const rest = space === -1 ? "" : raw.slice(space + 1).trim();
    const argv = rest.length ? rest.split(/\s+/) : [];
    const channel = isChannel(target) ? target : null;
    const need = (cond, msg) => { if (!cond) { errorLine(msg); throw new Error(msg); } };
    const post = (path, body) => api(`/api/networks/${encodeURIComponent(net)}${path}`, { method: "POST", body });

    try {
      switch (cmd) {
        case "help":
        case "?":
          pushLine(net, target, { kind: "help" });
          return;

        case "me":
          need(rest.length > 0, "/me requires text");
          await sendAction(net, target, rest);
          return;

        case "msg": {
          need(argv.length >= 2, "/msg <target> <text>");
          const t = argv[0];
          const body = rest.slice(t.length).trim();
          need(body.length > 0, "/msg requires text");
          await api(`/api/networks/${encodeURIComponent(net)}/send`, {
            method: "POST",
            body: { target: t, message: body },
          });
          pushLine(net, t, { kind: "msg", self: true, nick: ownNickFor(net), body });
          return;
        }

        case "query":
          need(argv.length === 1, "/query <nick>");
          selectChannel(net, argv[0]);
          return;

        case "join":
          need(argv.length === 1, "/join <#channel>");
          await api(`/api/networks/${encodeURIComponent(net)}/join?channel=${encodeURIComponent(argv[0])}`, { method: "POST" });
          setStatus(`joined ${argv[0]}`, "ok");
          return;

        case "part":
          need(channel, "/part requires an active channel");
          await api(
            `/api/networks/${encodeURIComponent(net)}/part?channel=${encodeURIComponent(channel)}`
            + (rest ? `&reason=${encodeURIComponent(rest)}` : ""),
            { method: "POST" },
          );
          setStatus(`parted ${channel}`, "ok");
          return;

        case "op": case "deop": case "voice": case "devoice":
          need(channel, `/${cmd} requires an active channel`);
          need(argv.length === 1, `/${cmd} <nick>`);
          await post(`/${cmd}`, { channel, nick: argv[0] });
          setStatus(`${cmd} ${argv[0]}`, "ok");
          return;

        case "kick": {
          need(channel, "/kick requires an active channel");
          need(argv.length >= 1, "/kick <nick> [reason]");
          const nick = argv[0];
          const reason = rest.slice(nick.length).trim();
          await post("/kick", { channel, nick, reason });
          setStatus(`kicked ${nick}`, "ok");
          return;
        }

        case "ban":
          need(channel, "/ban requires an active channel");
          need(argv.length === 1, "/ban <nick>");
          await post("/ban", { channel, nick: argv[0] });
          setStatus(`banned ${argv[0]}`, "ok");
          return;

        case "kickban": {
          need(channel, "/kickban requires an active channel");
          need(argv.length >= 1, "/kickban <nick> [reason]");
          const nick = argv[0];
          const reason = rest.slice(nick.length).trim();
          await post("/kickban", { channel, nick, reason });
          setStatus(`kickbanned ${nick}`, "ok");
          return;
        }

        case "mode":
          need(channel, "/mode requires an active channel");
          need(argv.length >= 1, "/mode <flags> [args…]");
          await post("/mode", { channel, flags: argv[0], args: argv.slice(1) });
          setStatus(`mode ${argv[0]} ${channel}`, "ok");
          return;

        case "topic":
          need(channel, "/topic requires an active channel");
          await post("/topic", { channel, topic: rest || null });
          return;

        case "nick":
          need(argv.length === 1, "/nick <new>");
          await post("/nick", { nick: argv[0] });
          state.nicks[net] = argv[0];
          return;

        case "whois": {
          need(argv.length === 1, "/whois <nick>");
          await post("/whois", { nick: argv[0] });
          const pendNet = state.activeNet;
          const pendTarget = state.activeTarget || argv[0];
          if (pendNet) {
            state.pendingWhois.set(`${pendNet}\u0001${argv[0].toLowerCase()}`, { id: null, target: pendTarget });
          }
          return;
        }

        case "raw":
          need(rest.length > 0, "/raw <line>");
          await post("/raw", { line: rest });
          return;

        default:
          errorLine(`unknown command: /${cmd} — try /help`);
          return;
      }
    } catch (err) {
      // need() already logged its message; for HTTP failures, surface server detail.
      if (err && err.status && err.message) errorLine(err.message);
      setStatus(err.message || String(err), "err");
    }
  }

  /* ---------------- event wiring ---------------- */
  function wireUI() {
    // theme
    $("theme-toggle").addEventListener("click", toggleTheme);

    // tabs
    document.getElementById("view-tabs").addEventListener("click", (ev) => {
      const t = ev.target.closest(".tab");
      if (!t) return;
      setView(t.dataset.view);
    });

    // network tree
    $("network-tree").addEventListener("click", (ev) => {
      const ch = ev.target.closest(".ch-row");
      if (ch) { selectChannel(ch.dataset.net, ch.dataset.target); return; }
      const row = ev.target.closest("[data-action=toggle-net]");
      if (row) {
        const grp = row.closest(".net-group");
        const name = grp.dataset.net;
        if (state.open.has(name)) state.open.delete(name); else state.open.add(name);
        grp.classList.toggle("is-open");
        persistState();
      }
    });
    $("refresh-networks").addEventListener("click", loadNetworks);

    // composer
    $("send-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const inp = $("composer-input");
      const v = inp.value.trim();
      if (!v) return;
      sendActiveMessage(v);
      inp.value = "";
    });

    // admin panel buttons
    document.addEventListener("click", async (ev) => {
      const b = ev.target.closest("button[data-action]");
      if (!b) return;
      const action = b.dataset.action;
      if (action === "toggle-net") return;
      try {
        if (action === "pull-repo") {
          await pullRepo(b);
          return;
        }
        if (action === "delete-repo") {
          await api("/api/repos/" + encodeURIComponent(b.dataset.name), { method: "DELETE" });
        } else if (action === "delete-acl") {
          await api("/api/acl/" + encodeURIComponent(b.dataset.id), { method: "DELETE" });
        } else if (["enable", "disable", "reload", "unload"].includes(action)) {
          await api("/api/modules/" + action, { method: "POST", body: { repo: b.dataset.repo, name: b.dataset.name } });
        } else { return; }
        preserveScroll(refreshAdminPanels);
      } catch (err) { setStatus(err.message, "err"); }
    });

    // admin forms
    document.addEventListener("submit", async (ev) => {
      if (ev.target.id === "repo-form") {
        ev.preventDefault();
        const fd = new FormData(ev.target);
        try {
          await api("/api/repos", {
            method: "POST",
            body: { name: fd.get("name"), url: fd.get("url"), branch: fd.get("branch") || "main", enabled: true },
          });
          ev.target.reset();
          refreshAdminPanels();
        } catch (err) { setStatus(err.message, "err"); }
      } else if (ev.target.id === "acl-form") {
        ev.preventDefault();
        const fd = new FormData(ev.target);
        try {
          await api("/api/acl", {
            method: "POST",
            body: { mask: fd.get("mask"), permission: fd.get("permission"), note: fd.get("note") || null },
          });
          ev.target.reset();
          refreshAdminPanels();
        } catch (err) { setStatus(err.message, "err"); }
      } else if (ev.target.id === "token-form") {
        ev.preventDefault();
        const fd = new FormData(ev.target);
        const tok = fd.get("token") || "";
        if (tok) { state.token = tok; localStorage.setItem(tokenKey, tok); }
        else { state.token = ""; localStorage.removeItem(tokenKey); }
        updateAuthBadge();
        closeTokenDialog();
        reconnectEvents();
        await loadNetworks();
        refreshAdminPanels();
        setStatus("token saved", "ok");
      }
    });

    // auth dialog buttons
    $("auth-change").addEventListener("click", openTokenDialog);
    $("token-cancel").addEventListener("click", closeTokenDialog);

    // click user → context menu (op/voice/kick/ban/whois/query/+acl)
    $("user-list").addEventListener("click", (ev) => {
      const row = ev.target.closest(".user-row");
      if (!row) return;
      const nick = row.dataset.nick;
      const mask = row.dataset.mask || `${nick}!*@*`;
      const prefix = row.dataset.prefix || "";
      if (!state.activeNet || !nick) return;
      ev.stopPropagation();
      openUserContextMenu(row, state.activeNet, nick, mask, prefix);
    });

    // ACL dialog cancel
    $("user-acl-cancel").addEventListener("click", closeUserAclDialog);

    // ACL dialog submit
    $("user-acl-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      const errBox = $("user-acl-error");
      errBox.hidden = true; errBox.textContent = "";
      try {
        await api("/api/acl", {
          method: "POST",
          body: {
            mask: fd.get("mask"),
            permission: fd.get("permission"),
            note: fd.get("note") || null,
          },
        });
        closeUserAclDialog();
        setStatus(`ACL added: ${fd.get("mask")} → ${fd.get("permission")}`, "ok");
        if (state.view === "acl") refreshAdminPanels();
      } catch (err) {
        errBox.textContent = err.body || err.message;
        errBox.hidden = false;
      }
    });
  }

  /* ---------------- boot ---------------- */
  document.addEventListener("DOMContentLoaded", () => {
    wireUI();
    updateAuthBadge();
    if (!state.token) {
      setStatus("Set an API token to populate panels.", "err");
      openTokenDialog();
    } else {
      loadNetworks();
      reconnectEvents();
      refreshAdminPanels();
    }
    // periodic refresh of network/channel topology
    setInterval(() => { if (state.token) loadNetworks(); }, 10000);
  });
})();
