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
    pendingEchoes: new Map(),// bufKey → [{kind, body, expires}] — FIFO of our own sends
                             // awaiting the pydle-synthesized `on_message` echo so
                             // we can dedup it against the optimistic local render.
                             // Any own-nick message NOT in the queue originated
                             // from a bot module and must be displayed.
  };
  const ECHO_TTL_MS = 10000;

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
    el.classList.remove("ok", "err", "warn");
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
    if (name === "settings") refreshSettingsPanel();
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

  function recordPendingEcho(net, target, kind, body) {
    const key = bufKey(net, target);
    let q = state.pendingEchoes.get(key);
    if (!q) { q = []; state.pendingEchoes.set(key, q); }
    q.push({ kind, body, expires: Date.now() + ECHO_TTL_MS });
  }
  // Match a pydle-synthesized echo against the oldest recorded send for this
  // buffer. Expired entries are dropped first. Returns true iff the head
  // matched — meaning the UI already rendered this message optimistically and
  // the echo should be suppressed.
  function consumePendingEcho(net, target, kind, body) {
    const key = bufKey(net, target);
    const q = state.pendingEchoes.get(key);
    if (!q || q.length === 0) return false;
    const now = Date.now();
    while (q.length && q[0].expires < now) q.shift();
    if (q.length === 0) return false;
    if (q[0].kind === kind && q[0].body === body) {
      q.shift();
      return true;
    }
    return false;
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

  async function closeQuery(net, target) {
    if (!net || !target || isChannel(target) || target === "*") return;
    try {
      await api(`/api/networks/${encodeURIComponent(net)}/queries/${encodeURIComponent(target)}`,
                { method: "DELETE" });
    } catch (err) {
      setStatus(`close ${target}: ${err.message}`, "err");
      return;
    }
    const key = bufKey(net, target);
    state.buffers.delete(key);
    state.hydrated.delete(key);
    state.pendingEchoes.delete(key);
    delete state.unread[key];
    delete state.topics[key];
    if (state.activeNet === net && state.activeTarget === target) {
      state.activeTarget = "*";
    }
    renderTree();
    renderActiveBuffer();
    persistState();
    setStatus(`closed query ${target}`, "ok");
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
      // Also surface query buffers (non-channel targets we have buffers for).
      // The "*" target is the network status bucket — it goes in its own group.
      for (const key of state.buffers.keys()) {
        const [bn, bt] = key.split("\u0001");
        if (bn === n.name && bt !== "*") declared.add(bt);
      }
      const all = Array.from(declared);
      const chans = all.filter(isChannel).sort((a, b) =>
        a.localeCompare(b, undefined, { sensitivity: "base" }));
      const queries = all.filter((t) => !isChannel(t)).sort((a, b) =>
        a.localeCompare(b, undefined, { sensitivity: "base" }));

      const renderRow = (target, prefixGlyph, displayName, closable) => {
        const k = bufKey(n.name, target);
        const unread = state.unread[k] || 0;
        const active = (state.activeNet === n.name && state.activeTarget === target) ? "is-active" : "";
        const prefix = prefixGlyph != null
          ? prefixGlyph
          : (isChannel(target) ? target[0] : "@");
        const name = displayName != null
          ? displayName
          : (isChannel(target) ? target.slice(1) : target);
        const cls = "ch-row " + active + (closable ? " is-closable" : "");
        const closeBtn = closable
          ? `<button type="button" class="ch-close" data-action="close-query" title="Close query (clears history)" aria-label="Close query">×</button>`
          : "";
        return `<div class="${cls}" data-net="${escapeAttr(n.name)}" data-target="${escapeAttr(target)}">
          <span class="ch-prefix">${escapeHtml(prefix)}</span>
          <span class="ch-name">${escapeHtml(name)}</span>
          <span class="ch-badge" ${unread ? "" : "hidden"}>${unread}</span>
          ${closeBtn}
        </div>`;
      };

      const section = (label, rowsHtml) =>
        rowsHtml ? `<div class="ch-section-head">${escapeHtml(label)}</div>${rowsHtml}` : "";

      // Status bucket is always available per network — server replies, connect
      // notices, and other non-targeted messages land on the "*" target.
      const statusHtml = section("status",
        renderRow("*", "§", "status"));
      const chansHtml = section("channels",
        chans.map((c) => renderRow(c)).join(""));
      const queriesHtml = section("queries",
        queries.map((c) => renderRow(c, null, null, true)).join(""));

      const body = (statusHtml + chansHtml + queriesHtml)
        || `<div class="rail-empty">no channels</div>`;
      const statusCls = n.connected ? "connected" : "failed";
      return `<div class="net-group ${open ? "is-open" : ""}" data-net="${escapeAttr(n.name)}">
        <div class="net-row" data-action="toggle-net">
          <span class="net-caret">▶</span>
          <span class="net-name">${escapeHtml(n.name)}</span>
          <span class="net-status ${statusCls}" title="${n.connected ? "connected" : "offline"}"></span>
        </div>
        <div class="net-channels">${body}</div>
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

    const isStatus = state.activeTarget === "*";
    const displayTarget = isStatus ? "status" : state.activeTarget;
    netLabel.textContent = state.activeNet;
    chLabel.textContent = displayTarget;
    composer.disabled = false; sendBtn.disabled = false;
    composer.placeholder = isStatus
      ? "status — slash commands only (try /help)"
      : `message ${state.activeTarget}`;
    prompt.textContent = isStatus ? "§" : (isChannel(state.activeTarget) ? "›" : "@");
    $("status-net").textContent = state.activeNet;
    $("status-ch").textContent = displayTarget;
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
    focusComposer();
  }

  function focusComposer() {
    const composer = $("composer-input");
    if (!composer || composer.disabled) return;
    // Defer so render passes and any pending layout settle first.
    setTimeout(() => {
      composer.focus({ preventScroll: true });
      const len = composer.value.length;
      composer.setSelectionRange(len, len);
    }, 0);
  }

  async function hydrateHistory(net, target) {
    // Status bucket ("*") has no server-side history — live events only.
    if (!net || !target || target === "*") return;
    const key = bufKey(net, target);
    if (state.hydrated.has(key)) return;
    state.hydrated.add(key);
    const url = isChannel(target)
      ? `/api/networks/${encodeURIComponent(net)}/channels/${encodeURIComponent(target)}/history`
      : `/api/networks/${encodeURIComponent(net)}/queries/${encodeURIComponent(target)}/history`;
    try {
      const lines = await api(url);
      if (!Array.isArray(lines) || lines.length === 0) return;
      const buf = getBuffer(net, target);
      // Merge history at the front while avoiding duplicates of any live lines
      // we may already have collected while the fetch was in flight. Event lines
      // carry structured fields in live payloads but a rendered `body` string in
      // history, so key them on stable identity fields instead of body text.
      const dedupKey = (l) => l.kind === "event"
        ? `event|${l.event||""}|${l.channel||""}|${l.user||l.target||""}|${l.old||""}|${l.new||""}|${l.by||""}`
        : `${l.kind}|${l.nick||""}|${l.body||""}`;
      const liveKeys = new Set(buf.map(dedupKey));
      const historical = lines
        .filter((l) => !liveKeys.has(dedupKey(l)))
        .map((l) => Object.assign({}, l, { ts: l.ts ? new Date(l.ts) : new Date() }));
      if (historical.length === 0) return;
      buf.splice(0, 0, ...historical);
      if (buf.length > state.bufferLimit) buf.splice(0, buf.length - state.bufferLimit);
      if (state.activeNet === net && state.activeTarget === target) renderActiveBuffer();
      else renderTree();
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
      // Discover query (PM) buffers the server knows about, so they re-appear
      // in the sidebar after a page refresh. Hydrating populates the local
      // buffer with stored lines and makes renderTree pick up the row.
      await Promise.all(nets.map(async (n) => {
        try {
          const queries = await api(`/api/networks/${encodeURIComponent(n.name)}/queries`);
          if (!Array.isArray(queries)) return;
          await Promise.all(queries.map((q) => hydrateHistory(n.name, q.peer)));
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
      if (state.activeNet && state.activeTarget) {
        hydrateHistory(state.activeNet, state.activeTarget);
        focusComposer();
      }
    } catch (err) {
      setStatus(err.message, "err");
    }
  }

  /* ---------------- admin panels (modules/repos/acl) ---------------- */
  const renderers = {
    module(m) {
      const tasks = Number(m.scheduled_task_count) || 0;
      const userScheds = Number(m.user_schedule_count) || 0;
      const total = tasks + userScheds;
      const implements_ = m.implements_schedules || tasks > 0 || userScheds > 0;
      const badge = total > 0
        ? `<span class="module-sched-badge" title="${tasks} task${tasks === 1 ? "" : "s"} · ${userScheds} user schedule${userScheds === 1 ? "" : "s"}">⏱ ${total}</span>`
        : "";
      const schedBtn = implements_
        ? `<button data-action="open-schedules" data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">Schedules</button>`
        : "";
      return `<div class="card" data-module-card data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">
        <h3>${escapeHtml(m.repo)}/${escapeHtml(m.name)}${badge}</h3>
        <div class="muted">${escapeHtml(m.description || "(no description)")} — ${m.enabled ? "enabled" : "disabled"}</div>
        <div class="actions">
          <button data-action="${m.enabled ? "disable" : "enable"}" data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">${m.enabled ? "Disable" : "Enable"}</button>
          <button data-action="reload" data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">Reload</button>
          <button data-action="unload" data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">Unload</button>
          <button data-action="open-settings" data-repo="${escapeAttr(m.repo)}" data-name="${escapeAttr(m.name)}">Settings</button>
          ${schedBtn}
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

  /* ---------- per-module schedules (lazy) ---------- */

  function fetchModuleSchedules(repo, name) {
    return api(`/api/modules/${encodeURIComponent(repo)}/${encodeURIComponent(name)}/schedules`);
  }

  function relTime(iso) {
    if (!iso) return "—";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "—";
    const diff = then - Date.now();
    const abs = Math.abs(diff);
    const sec = Math.round(abs / 1000);
    const min = Math.round(sec / 60);
    const hr = Math.round(min / 60);
    const day = Math.round(hr / 24);
    let txt;
    if (sec < 60) txt = `${sec} s`;
    else if (min < 60) txt = `${min} min`;
    else if (hr < 48) txt = `${hr} h`;
    else txt = `${day} d`;
    return diff >= 0 ? `in ${txt}` : `${txt} ago`;
  }

  function triggerSummary(t) {
    if (!t || typeof t !== "object") return "";
    const type = t.type || "";
    const bits = Object.entries(t)
      .filter(([k]) => k !== "type")
      .map(([k, v]) => `${k}=${v}`);
    return `${type}${bits.length ? " " + bits.join(",") : ""}`;
  }

  const UPCOMING_SCHEDULE_STATUSES = new Set(["scheduled", "paused"]);
  const PAST_SCHEDULE_LIMIT = 5;

  function renderUserScheduleRow(s) {
    const label = s.title || (s.id ? s.id.slice(0, 8) : "(unnamed)");
    const status = s.status || "scheduled";
    const actions = [];
    if (status === "scheduled") {
      actions.push(`<button data-action="schedule-pause" data-id="${escapeAttr(s.id)}">Pause</button>`);
    } else if (status === "paused") {
      actions.push(`<button data-action="schedule-resume" data-id="${escapeAttr(s.id)}">Resume</button>`);
    }
    if (status === "scheduled" || status === "paused") {
      actions.push(`<button data-action="schedule-run-now" data-id="${escapeAttr(s.id)}">Run now</button>`);
      actions.push(`<button data-action="schedule-cancel" data-id="${escapeAttr(s.id)}">Cancel</button>`);
    }
    const timeIso = UPCOMING_SCHEDULE_STATUSES.has(status)
      ? (s.next_run_at || "")
      : (s.last_run_at || s.updated_at || "");
    return `
      <li class="schedule-row">
        <span class="schedule-name" title="${escapeAttr(s.id)}">${escapeHtml(label)}</span>
        <span class="status-pill is-${escapeAttr(status)}">${escapeHtml(status)}</span>
        <span class="schedule-trigger">${escapeHtml(triggerSummary(s.trigger))}</span>
        <span class="schedule-next" title="${escapeAttr(timeIso)}">${escapeHtml(relTime(timeIso))}</span>
        <span class="schedule-actions">${actions.join("")}</span>
      </li>`;
  }

  function partitionSchedules(schedules) {
    const upcoming = [];
    const past = [];
    for (const s of schedules) {
      const status = s.status || "scheduled";
      if (UPCOMING_SCHEDULE_STATUSES.has(status)) upcoming.push(s);
      else past.push(s);
    }
    past.sort((a, b) => {
      const at = new Date(a.last_run_at || a.updated_at || 0).getTime();
      const bt = new Date(b.last_run_at || b.updated_at || 0).getTime();
      return bt - at;
    });
    return { upcoming, past };
  }

  function renderModuleScheduleBody(bodyEl, data) {
    const tasks = Array.isArray(data.module_tasks) ? data.module_tasks : [];
    const schedules = Array.isArray(data.user_schedules) ? data.user_schedules : [];
    const tasksRows = tasks.map((t) => `
      <li class="schedule-row">
        <span class="schedule-name">${escapeHtml(t.task_name)}</span>
        <span class="schedule-trigger" title="${escapeAttr(t.job_id)}">${escapeHtml(t.trigger || "")}</span>
        <span class="schedule-next" title="${escapeAttr(t.next_run_at || "")}">${t.paused ? "paused" : escapeHtml(relTime(t.next_run_at))}</span>
      </li>`).join("");
    const { upcoming, past } = partitionSchedules(schedules);
    const shownPast = past.slice(0, PAST_SCHEDULE_LIMIT);
    const upcomingRows = upcoming.map(renderUserScheduleRow).join("");
    const pastRows = shownPast.map(renderUserScheduleRow).join("");
    const pastHeading = past.length > shownPast.length
      ? `Past user schedules (${shownPast.length} of ${past.length})`
      : `Past user schedules (${past.length})`;
    bodyEl.innerHTML = `
      <div class="module-schedules-group">
        <div class="module-schedules-heading">Periodic tasks (${tasks.length})</div>
        ${tasks.length
          ? `<ul class="module-schedules-list">${tasksRows}</ul>
             <div class="module-schedules-hint">Disable the module to stop these.</div>`
          : `<div class="rail-empty">none</div>`}
      </div>
      <div class="module-schedules-group">
        <div class="module-schedules-heading">Upcoming user schedules (${upcoming.length})</div>
        ${upcoming.length
          ? `<ul class="module-schedules-list">${upcomingRows}</ul>`
          : `<div class="rail-empty">none</div>`}
      </div>
      <div class="module-schedules-group">
        <div class="module-schedules-heading">${pastHeading}</div>
        ${shownPast.length
          ? `<ul class="module-schedules-list">${pastRows}</ul>`
          : `<div class="rail-empty">none</div>`}
      </div>`;
  }

  /* ---------- per-module schedules dialog (modal overlay) ---------- */

  const moduleSchedulesDialog = { repo: null, name: null };

  function getSchedulesDialogEls() {
    return {
      dlg: document.getElementById("module-schedules-dialog"),
      title: document.getElementById("module-schedules-title"),
      sub: document.getElementById("module-schedules-sub"),
      body: document.getElementById("module-schedules-body"),
      feedback: document.getElementById("module-schedules-feedback"),
      refresh: document.getElementById("module-schedules-refresh"),
    };
  }

  async function openModuleSchedules(btn) {
    const repo = btn.dataset.repo;
    const name = btn.dataset.name;
    const els = getSchedulesDialogEls();
    if (!els.dlg) return;
    moduleSchedulesDialog.repo = repo;
    moduleSchedulesDialog.name = name;
    els.title.textContent = `${repo}/${name}`;
    els.sub.textContent = "loading…";
    els.feedback.textContent = "";
    els.feedback.classList.remove("is-err", "is-ok");
    els.body.innerHTML = `<div class="module-settings-loading">fetching schedules…</div>`;
    if (typeof els.dlg.showModal === "function" && !els.dlg.open) els.dlg.showModal();
    await loadModuleSchedulesIntoDialog();
  }

  async function loadModuleSchedulesIntoDialog() {
    const els = getSchedulesDialogEls();
    const repo = moduleSchedulesDialog.repo;
    const name = moduleSchedulesDialog.name;
    if (!els.dlg || !repo || !name) return;
    try {
      const data = await fetchModuleSchedules(repo, name);
      const tasks = Array.isArray(data.module_tasks) ? data.module_tasks.length : 0;
      const scheds = Array.isArray(data.user_schedules) ? data.user_schedules.length : 0;
      els.sub.textContent = `${tasks} declared task${tasks === 1 ? "" : "s"} · ${scheds} user schedule${scheds === 1 ? "" : "s"}`;
      renderModuleScheduleBody(els.body, data);
    } catch (err) {
      els.sub.textContent = "";
      els.body.innerHTML = `<div class="card-error">Error: ${escapeHtml(err.message)}</div>`;
    }
  }

  function moduleRefForScheduleButton(btn) {
    if (btn.closest("#module-schedules-dialog")) {
      if (!moduleSchedulesDialog.repo) return null;
      return { repo: moduleSchedulesDialog.repo, name: moduleSchedulesDialog.name, inDialog: true };
    }
    const card = btn.closest("[data-module-card]");
    if (!card) return null;
    return { repo: card.dataset.repo, name: card.dataset.name, inDialog: false };
  }

  function wireModuleSchedulesDialog() {
    const els = getSchedulesDialogEls();
    if (!els.dlg) return;
    if (els.refresh) els.refresh.addEventListener("click", loadModuleSchedulesIntoDialog);
    els.dlg.addEventListener("close", () => {
      moduleSchedulesDialog.repo = null;
      moduleSchedulesDialog.name = null;
    });
    els.dlg.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-schedules-close]")) { els.dlg.close(); return; }
      if (ev.target === els.dlg) els.dlg.close();
    });
  }

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

  /* ---------- per-module settings editor (modal overlay) ---------- */

  const moduleSettingsDialog = {
    repo: null,
    name: null,
    declared: false,
  };

  function getDialogEls() {
    return {
      dlg: document.getElementById("module-settings-dialog"),
      title: document.getElementById("module-settings-title"),
      sub: document.getElementById("module-settings-sub"),
      body: document.getElementById("module-settings-body"),
      feedback: document.getElementById("module-settings-feedback"),
      save: document.getElementById("module-settings-save"),
      reload: document.getElementById("module-settings-reload"),
    };
  }

  async function openModuleSettings(btn) {
    const repo = btn.dataset.repo;
    const name = btn.dataset.name;
    const els = getDialogEls();
    if (!els.dlg) return;
    moduleSettingsDialog.repo = repo;
    moduleSettingsDialog.name = name;
    moduleSettingsDialog.declared = false;
    els.title.textContent = `${repo}/${name}`;
    els.sub.textContent = "loading…";
    els.feedback.textContent = "";
    els.feedback.classList.remove("is-err", "is-ok");
    els.save.disabled = true;
    if (els.reload) {
      els.reload.disabled = false;
      els.reload.classList.remove("is-ok", "is-err", "is-busy");
      els.reload.textContent = "Reload";
    }
    els.body.innerHTML = `<div class="module-settings-loading">fetching schema…</div>`;
    if (typeof els.dlg.showModal === "function" && !els.dlg.open) els.dlg.showModal();

    const base = `/api/modules/${encodeURIComponent(repo)}/${encodeURIComponent(name)}/settings`;
    try {
      const [schema, current] = await Promise.all([api(base + "/schema"), api(base)]);
      if (!schema.declared) {
        els.sub.textContent = "no typed settings declared";
        els.body.innerHTML = `<div class="module-settings-empty">
          <div class="module-settings-empty-glyph">∅</div>
          <div>This module does not declare typed settings.</div>
          <div class="muted">Modules can expose settings by declaring a schema in <code>settings.py</code>.</div>
        </div>`;
        return;
      }
      moduleSettingsDialog.declared = true;
      const description = schema.description || schema.title || "Configure this module's runtime settings.";
      els.sub.textContent = description;
      els.body.innerHTML = renderSettingsFormBody(schema, current.values || {});
      els.save.disabled = false;
    } catch (err) {
      els.sub.textContent = "";
      els.body.innerHTML = `<div class="card-error">Error: ${escapeHtml(err.message)}</div>`;
    }
  }

  function renderSettingsFormBody(schema, values) {
    const props = schema.properties || {};
    const entries = Object.entries(props);
    if (entries.length === 0) {
      return `<div class="module-settings-empty">
        <div class="module-settings-empty-glyph">∅</div>
        <div>This module declares a schema but defines no fields.</div>
      </div>`;
    }
    const fields = entries.map(([key, prop]) => {
      const label = escapeHtml(prop.title || key);
      const desc = prop.description ? `<span class="module-settings-desc">${escapeHtml(prop.description)}</span>` : "";
      const value = values[key];
      const isSecret = prop.secret === true;
      const type = prop.type;
      const hasValue = value !== undefined && value !== null && value !== "";
      const valueTag = isSecret
        ? (hasValue ? `<span class="module-settings-pill is-secret">set</span>` : `<span class="module-settings-pill is-empty">unset</span>`)
        : "";
      let input;
      let control = "";
      if (isSecret) {
        input = `<input type="password" name="${escapeAttr(key)}" placeholder="${hasValue ? "•••• (leave blank to keep current)" : "enter value…"}" autocomplete="new-password">`;
      } else if (prop.format === "uri" || (type === "string" && key.toLowerCase().includes("url"))) {
        input = `<input type="url" name="${escapeAttr(key)}" value="${escapeAttr(value ?? "")}" placeholder="https://…">`;
      } else if (type === "integer") {
        const minAttr = prop.minimum != null ? ` min="${prop.minimum}"` : "";
        const maxAttr = prop.maximum != null ? ` max="${prop.maximum}"` : "";
        input = `<input type="number" step="1"${minAttr}${maxAttr} name="${escapeAttr(key)}" value="${escapeAttr(value ?? "")}">`;
      } else if (type === "number") {
        input = `<input type="number" step="any" name="${escapeAttr(key)}" value="${escapeAttr(value ?? "")}">`;
      } else if (type === "boolean") {
        control = "switch";
        input = `<span class="module-settings-switch">
          <input type="checkbox" name="${escapeAttr(key)}"${value ? " checked" : ""}>
          <span class="module-settings-switch-track"><span class="module-settings-switch-thumb"></span></span>
        </span>`;
      } else {
        input = `<input type="text" name="${escapeAttr(key)}" value="${escapeAttr(value ?? "")}" placeholder="${escapeAttr(prop.default ?? "")}">`;
      }
      const typeBadge = `<span class="module-settings-type">${escapeHtml(type || "string")}${isSecret ? " · secret" : ""}</span>`;
      return `<label class="module-settings-field ${control === "switch" ? "is-switch" : ""}" data-key="${escapeAttr(key)}" data-type="${escapeAttr(type || "string")}" data-secret="${isSecret ? "1" : "0"}">
        <span class="module-settings-field-head">
          <span class="module-settings-label">${label}</span>
          <span class="module-settings-meta">${valueTag}${typeBadge}</span>
        </span>
        <span class="module-settings-control">${input}</span>
        ${desc}
      </label>`;
    }).join("");
    return `<form class="module-settings-form" onsubmit="return false">
      ${fields}
    </form>`;
  }

  async function saveOpenModuleSettings() {
    const els = getDialogEls();
    if (!els.dlg || !moduleSettingsDialog.declared) return;
    const form = els.body.querySelector(".module-settings-form");
    if (!form) return;
    const repo = moduleSettingsDialog.repo;
    const name = moduleSettingsDialog.name;
    const patch = {};
    form.querySelectorAll(".module-settings-field").forEach((label) => {
      const key = label.dataset.key;
      const type = label.dataset.type;
      const isSecret = label.dataset.secret === "1";
      const input = label.querySelector("input");
      if (!input) return;
      if (input.type === "checkbox") { patch[key] = input.checked; return; }
      const raw = input.value;
      if (isSecret && raw === "") return;
      if (raw === "" && !isSecret) return;
      if (type === "integer") { const n = Number.parseInt(raw, 10); if (!Number.isNaN(n)) patch[key] = n; return; }
      if (type === "number") { const n = Number.parseFloat(raw); if (!Number.isNaN(n)) patch[key] = n; return; }
      patch[key] = raw;
    });
    els.feedback.textContent = "saving…";
    els.feedback.classList.remove("is-err", "is-ok");
    els.save.disabled = true;
    try {
      await api(
        `/api/modules/${encodeURIComponent(repo)}/${encodeURIComponent(name)}/settings`,
        { method: "PUT", body: patch },
      );
      els.feedback.textContent = "saved — reload module to apply.";
      els.feedback.classList.add("is-ok");
    } catch (err) {
      els.feedback.textContent = "Error: " + err.message;
      els.feedback.classList.add("is-err");
    } finally {
      els.save.disabled = false;
    }
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
        // pydle's IRCv3.2 `message()` synthesizes `on_message(own_nick, …)`
        // locally for every outbound send (no echo-message cap needed). The
        // web UI also renders its own sends optimistically, so echoes that
        // match a queued send are suppressed. Unmatched own-nick messages
        // originate from bot modules (e.g. !help handler) and MUST render.
        const ownNick = state.nicks[net];
        // PM buffer is keyed by the peer nick: on inbound source is peer, on
        // outbound (own-nick echo) target is peer. Routing by source alone
        // opens a phantom query with the bot itself and splits a conversation
        // across two buffers.
        const buffer = isPM ? (ownNick && p.source === ownNick ? target : p.source) : target;
        const actionBody = parseAction(p.message);
        const echoKind = actionBody !== null ? "action" : "msg";
        const echoBody = actionBody !== null ? actionBody : p.message;
        if (ownNick && p.source === ownNick) {
          if (consumePendingEcho(net, buffer, echoKind, echoBody)) break;
          pushLine(net, buffer, actionBody !== null
            ? { kind: "action", self: true, nick: p.source, body: actionBody }
            : { kind: "msg",    self: true, nick: p.source, body: p.message });
          break;
        }
        if (actionBody !== null) {
          pushLine(net, buffer, { kind: "action", nick: p.source, body: actionBody });
        } else {
          pushLine(net, buffer, { kind: "msg", nick: p.source, body: p.message });
        }
        break;
      }
      case "notice": {
        const target = p.target;
        const isPM = !isChannel(target);
        const ownNick = state.nicks[net];
        const buffer = isPM ? (ownNick && p.source === ownNick ? target : p.source) : target;
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
      case "nick": {
        // Keep own-nick tracking in sync — the server just accepted our rename.
        if (state.nicks[net] === p.old) state.nicks[net] = p.new;
        // Update the per-network joined cache so the sidebar count stays right.
        const joined = state.joined[net] || {};
        for (const ch of Object.keys(joined)) {
          const idx = joined[ch].indexOf(p.old);
          if (idx !== -1) joined[ch][idx] = p.new;
        }
        // broadcast to every buffer of this network the user is in (cheap: log to active)
        for (const key of state.buffers.keys()) {
          const [bn] = key.split("\u0001");
          if (bn === net) pushLine(net, key.split("\u0001")[1], {
            kind: "event", event: "nick", glyph: "↺",
            old: p.old, new: p.new, ident: p.ident, host: p.host,
          });
        }
        // Refresh the active user list so rename reflects immediately in the
        // roster panel (server-side ChannelRoster already applied the rename).
        if (state.activeNet === net && isChannel(state.activeTarget)) {
          loadActiveUsers();
        }
        break;
      }
      case "connect": {
        const meta = state.networks.find((x) => x.name === net);
        if (meta) meta.connected = true;
        pushLine(net, "*", { kind: "system", body: `connected to ${net}` });
        loadNetworks();
        break;
      }
      case "disconnect": {
        const meta = state.networks.find((x) => x.name === net);
        if (meta) meta.connected = false;
        state.joined[net] = {};
        pushLine(net, "*", { kind: "system", body: `disconnected from ${net}${p.expected ? "" : " (unexpected)"}` });
        renderTree();
        break;
      }
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
      case "roster":
        // Server emits "roster" after /WHO completes on join so the UI can
        // refresh once the authoritative masks + modes are known.
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
      case "settings_changed":
        // Fire-and-forget: only refresh when the Settings tab is the active view.
        if (state.view === "settings") refreshSettingsPanel();
        break;
      case "rate_limit_disabled_warning":
        setStatus(`${net}: outgoing rate limit is DISABLED — server may kill the bot`, "warn");
        if (state.view === "settings") refreshSettingsPanel();
        break;
      case "rate_limit_drop":
        setStatus(`${net}: rate-limit queue overflow — dropped message to ${p.target || p.command || "?"}`, "err");
        break;
      default:
        break;
    }
  }

  /* ---------------- settings view ---------------- */
  const settings = {
    data: null,            // full /api/settings snapshot
    activeNetwork: null,   // selected network name
  };

  async function refreshSettingsPanel() {
    try {
      const data = await api("/api/settings");
      settings.data = data;
      const networks = data.networks || [];
      if (settings.activeNetwork && !networks.some((n) => n.name === settings.activeNetwork)) {
        settings.activeNetwork = null;
      }
      if (!settings.activeNetwork && networks.length > 0) {
        settings.activeNetwork = networks[0].name;
      }
      renderSettingsList();
      renderSettingsBody();
      syncGlobalBar();
    } catch (err) {
      const list = $("settings-network-list");
      if (list) list.innerHTML = `<div class="rail-empty">${escapeHtml(err.message)}</div>`;
    }
  }

  function syncGlobalBar() {
    const bar = $("settings-global-bar");
    const saveBtn = $("settings-save-btn");
    const pathEl = $("settings-config-path");
    const dirty = !!(settings.data && settings.data.dirty);
    const path = (settings.data && settings.data.config_path) || "config.toml";
    if (bar) bar.classList.toggle("is-dirty", dirty);
    if (saveBtn) {
      saveBtn.disabled = !dirty;
      saveBtn.title = dirty ? `Write runtime config to ${path}` : "No unsaved changes";
    }
    if (pathEl) pathEl.textContent = path.split("/").pop() || path;
  }

  function renderSettingsList() {
    const list = $("settings-network-list");
    if (!list) return;
    const networks = (settings.data && settings.data.networks) || [];
    if (!networks.length) {
      list.innerHTML = '<div class="rail-empty">no networks — use + to add</div>';
      return;
    }
    list.innerHTML = "";
    for (const n of networks) {
      const row = elt("div", "settings-net-row" + (n.name === settings.activeNetwork ? " is-active" : ""));
      row.dataset.network = n.name;
      row.dataset.connected = n.connected ? "true" : "false";
      const pill = elt("span", "conn-pill " + (n.connected ? "is-up" : "is-down"),
                       n.connected ? "online" : "offline");
      const title = elt("span", "settings-net-name", n.name);
      const meta = elt("span", "settings-net-meta",
                       n.current_server ? `${n.current_server.host}:${n.current_server.port}` : "—");
      row.appendChild(pill);
      row.appendChild(title);
      row.appendChild(meta);
      list.appendChild(row);
    }
  }

  function syncSettingsHead(net) {
    const head = $("settings-per-network-actions");
    const connectBtn = $("settings-connect-btn");
    const discBtn = $("settings-disconnect-btn");
    const rmBtn = $("settings-remove-btn");
    if (!head || !connectBtn || !discBtn || !rmBtn) return;
    const hasNet = !!net;
    const connected = !!(net && net.connected);
    head.classList.toggle("is-connected", hasNet && connected);
    head.classList.toggle("is-disconnected", hasNet && !connected);
    connectBtn.disabled = !hasNet || connected;
    discBtn.disabled = !hasNet || !connected;
    rmBtn.disabled = !hasNet;
    if (!hasNet) settingsClearRemoveArm();

    const path = $("settings-config-path");
    if (path) {
      path.textContent = (settings.data && settings.data.config_path) || "config.toml";
    }
  }

  function renderSettingsBody() {
    const body = $("settings-body");
    const title = $("settings-active-title");
    if (!body || !title) return;
    const net = (settings.data && settings.data.networks || []).find((n) => n.name === settings.activeNetwork);
    syncSettingsHead(net);
    if (!net) {
      title.textContent = "no network selected";
      body.innerHTML = '<p class="muted">Pick a network on the left, or use the + button to add one.</p>';
      return;
    }
    title.textContent = net.name;
    const auth = net.auth || { method: "none" };
    body.innerHTML = `
      <form id="settings-identity-form" class="settings-form">
        <div class="form-grid">
          <label>Nick <input name="nick" value="${escapeAttr(net.nick || "")}" required></label>
          <label>Username (ident) <input name="username" value="${escapeAttr(net.username || "")}" placeholder="(same as nick)"></label>
          <label>Realname <input name="realname" value="${escapeAttr(net.realname || "")}"></label>
          <label>Hostname <input name="hostname" value="${escapeAttr(net.hostname || "")}" placeholder="(auto)"></label>
          <label>Protocol
            <select name="protocol">
              <option value="ircv3"${net.protocol === "ircv3" ? " selected" : ""}>ircv3</option>
              <option value="rfc1459"${net.protocol === "rfc1459" ? " selected" : ""}>rfc1459</option>
            </select>
          </label>
          <label>Auth method
            <select name="auth_method">
              <option value="none"${auth.method === "none" ? " selected" : ""}>none</option>
              <option value="sasl"${auth.method === "sasl" ? " selected" : ""}>sasl</option>
              <option value="q"${auth.method === "q" ? " selected" : ""}>q (quakenet)</option>
              <option value="nickserv"${auth.method === "nickserv" ? " selected" : ""}>nickserv</option>
            </select>
          </label>
        </div>
        <div id="settings-auth-extra" class="form-grid"></div>
        <p class="muted form-hint">
          Nick applies live. Username, realname, protocol, and auth require a reconnect to take effect.
        </p>
        <div class="form-actions">
          <button type="submit" data-intent="apply">Apply</button>
          <button type="submit" data-intent="reconnect" id="settings-reconnect-btn">Apply and Reconnect</button>
        </div>
      </form>

      <section class="settings-card">
        <header class="settings-card-head">
          <h3>Servers</h3>
          <span class="muted">default is tried first; others are fallbacks</span>
        </header>
        <ol class="server-rows" id="settings-server-rows"></ol>
        <form id="settings-add-server-form" class="inline-form">
          <input name="host" placeholder="host" required>
          <input name="port" type="number" value="6697" min="1" max="65535" required>
          <label class="chk"><input type="checkbox" name="tls" checked> tls</label>
          <label class="chk"><input type="checkbox" name="tls_verify" checked> verify</label>
          <label class="chk"><input type="checkbox" name="is_default"> default</label>
          <button type="submit">Add server</button>
        </form>
      </section>

      <section class="settings-card">
        <header class="settings-card-head">
          <h3>Channels</h3>
          <span class="muted">joined on connect; live edits JOIN/PART immediately</span>
        </header>
        <ul class="channel-rows" id="settings-channel-rows"></ul>
        <form id="settings-add-channel-form" class="inline-form">
          <input name="channel" placeholder="#channel" required>
          <button type="submit">Add channel</button>
        </form>
      </section>

      <section class="settings-card" id="settings-ratelimit-card">
        <header class="settings-card-head">
          <h3>Outgoing rate limit</h3>
          <span class="muted">token bucket — protects against server flood-kill</span>
        </header>
        ${renderRateLimitWarning(net.rate_limit || {})}
        <form id="settings-ratelimit-form" class="settings-form">
          <div class="form-grid">
            <label class="chk">
              <input type="checkbox" name="rl_enabled"${(net.rate_limit && net.rate_limit.enabled !== false) ? " checked" : ""}>
              enabled
            </label>
            <label>Burst
              <input name="rl_burst" type="number" min="1" max="50" value="${(net.rate_limit && net.rate_limit.burst) || 5}" required>
            </label>
            <label>Period (s)
              <input name="rl_period" type="number" min="0.1" max="30" step="0.1" value="${(net.rate_limit && net.rate_limit.period) || 2.0}" required>
            </label>
          </div>
          <p class="muted form-hint">
            Defaults (burst 5, period 2s) match irssi. Disabling risks a K-line on strict networks (Libera, OFTC).
          </p>
          <div class="form-actions">
            <button type="submit">Apply</button>
          </div>
        </form>
      </section>
    `;
    renderAuthExtra(net.auth || { method: "none" });
    renderServerRows(net);
    renderChannelRows(net);
  }

  function renderRateLimitWarning(rl) {
    if (rl.enabled === false) {
      return '<div class="settings-warning">⚠ Rate limiting is DISABLED on this network — server may kill or K-line the bot on high-traffic bursts.</div>';
    }
    return "";
  }

  function renderAuthExtra(auth) {
    const box = $("settings-auth-extra");
    if (!box) return;
    if (auth.method === "none") { box.innerHTML = ""; return; }
    if (auth.method === "sasl") {
      box.innerHTML = `
        <label>Mechanism
          <select name="sasl_mechanism">
            ${["PLAIN","EXTERNAL","SCRAM-SHA-256","SCRAM-SHA-1"].map((m) =>
              `<option${auth.mechanism === m ? " selected" : ""}>${m}</option>`).join("")}
          </select>
        </label>
        <label>Username <input name="sasl_username" value="${escapeAttr(auth.username || "")}"></label>
        <label>Password <input name="sasl_password" type="password" value="${escapeAttr(auth.password || "")}"></label>
        <label>Cert path <input name="sasl_cert_path" value="${escapeAttr(auth.cert_path || "")}" placeholder="(EXTERNAL only)"></label>
        <label class="chk"><input type="checkbox" name="sasl_required"${auth.required ? " checked" : ""}> required</label>
      `;
    } else if (auth.method === "q") {
      box.innerHTML = `
        <label>Q user <input name="q_username" value="${escapeAttr(auth.username || "")}" required></label>
        <label>Q pass <input name="q_password" type="password" value="${escapeAttr(auth.password || "")}" required></label>
        <label>Service <input name="q_service" value="${escapeAttr(auth.service || "Q@CServe.quakenet.org")}"></label>
        <label class="chk"><input type="checkbox" name="q_hidehost"${auth.hidehost !== false ? " checked" : ""}> hidehost</label>
        <label class="chk"><input type="checkbox" name="q_required"${auth.required ? " checked" : ""}> required</label>
      `;
    } else if (auth.method === "nickserv") {
      box.innerHTML = `
        <label>Username <input name="ns_username" value="${escapeAttr(auth.username || "")}" required></label>
        <label>Password <input name="ns_password" type="password" value="${escapeAttr(auth.password || "")}" required></label>
        <label>Service nick <input name="ns_service_nick" value="${escapeAttr(auth.service_nick || "NickServ")}"></label>
        <label class="chk"><input type="checkbox" name="ns_required"${auth.required ? " checked" : ""}> required</label>
      `;
    }
  }

  function renderServerRows(net) {
    const ol = $("settings-server-rows");
    if (!ol) return;
    ol.innerHTML = "";
    const servers = net.servers || [];
    if (!servers.length) {
      const empty = document.createElement("li");
      empty.className = "server-row is-empty";
      empty.innerHTML = `<span class="muted">no servers yet — add one below to enable connect.</span>`;
      ol.appendChild(empty);
      return;
    }
    servers.forEach((s, i) => {
      const li = document.createElement("li");
      li.className = "server-row" + (s.is_default ? " is-default" : "");
      li.dataset.index = String(i);
      li.innerHTML = `
        <span class="server-badge">${s.is_default ? "★ default" : "fallback"}</span>
        <form class="server-edit-form" data-index="${i}">
          <input name="host" value="${escapeAttr(s.host)}" required>
          <input name="port" type="number" min="1" max="65535" value="${s.port}" required>
          <label class="chk"><input type="checkbox" name="tls"${s.tls ? " checked" : ""}> tls</label>
          <label class="chk"><input type="checkbox" name="tls_verify"${s.tls_verify ? " checked" : ""}> verify</label>
          <button type="submit" class="server-save" disabled>Save</button>
        </form>
        <span class="server-actions">
          ${s.is_default ? "" : `<button type="button" data-settings-action="server-default" data-index="${i}">Make default</button>`}
          <button type="button" data-settings-action="server-remove" data-index="${i}" class="danger">Remove</button>
        </span>
      `;
      ol.appendChild(li);
      const form = li.querySelector(".server-edit-form");
      const saveBtn = form.querySelector(".server-save");
      const initial = JSON.stringify({
        host: s.host, port: s.port, tls: !!s.tls, tls_verify: !!s.tls_verify,
      });
      const checkDirty = () => {
        const current = JSON.stringify({
          host: form.elements["host"].value,
          port: parseInt(form.elements["port"].value || "0", 10),
          tls: !!form.elements["tls"].checked,
          tls_verify: !!form.elements["tls_verify"].checked,
        });
        saveBtn.disabled = current === initial;
      };
      form.addEventListener("input", checkDirty);
      form.addEventListener("change", checkDirty);
    });
  }

  function renderChannelRows(net) {
    const ul = $("settings-channel-rows");
    if (!ul) return;
    ul.innerHTML = "";
    (net.channels || []).forEach((ch) => {
      const li = document.createElement("li");
      li.className = "channel-row";
      li.innerHTML = `
        <span class="channel-name">${escapeHtml(ch)}</span>
        <button type="button" data-settings-action="channel-remove" data-channel="${escapeAttr(ch)}" class="danger">Remove</button>
      `;
      ul.appendChild(li);
    });
  }

  function buildAuthPayloadFromForm(form) {
    const method = form.elements["auth_method"].value;
    if (method === "none") return { method: "none" };
    if (method === "sasl") {
      return {
        method: "sasl",
        mechanism: form.elements["sasl_mechanism"]?.value || "PLAIN",
        username: form.elements["sasl_username"]?.value || null,
        password: form.elements["sasl_password"]?.value || null,
        cert_path: form.elements["sasl_cert_path"]?.value || null,
        required: !!form.elements["sasl_required"]?.checked,
      };
    }
    if (method === "q") {
      return {
        method: "q",
        username: form.elements["q_username"].value,
        password: form.elements["q_password"].value,
        service: form.elements["q_service"]?.value || "Q@CServe.quakenet.org",
        hidehost: !!form.elements["q_hidehost"]?.checked,
        required: !!form.elements["q_required"]?.checked,
      };
    }
    return {
      method: "nickserv",
      username: form.elements["ns_username"].value,
      password: form.elements["ns_password"].value,
      service_nick: form.elements["ns_service_nick"]?.value || "NickServ",
      required: !!form.elements["ns_required"]?.checked,
    };
  }

  async function settingsApplyIdentity(form, { reconnect = false } = {}) {
    const name = settings.activeNetwork;
    if (!name) return;
    const body = {
      nick: form.elements["nick"].value,
      username: form.elements["username"].value || null,
      realname: form.elements["realname"].value || null,
      hostname: form.elements["hostname"].value || null,
      protocol: form.elements["protocol"].value,
      auth: buildAuthPayloadFromForm(form),
      reconnect: false,
    };
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}`, { method: "PATCH", body });
      if (reconnect) {
        await api(`/api/settings/networks/${encodeURIComponent(name)}/reconnect`, { method: "POST" });
        setStatus(`${name}: applied, reconnecting`, "ok");
      } else {
        setStatus(`${name}: identity updated`, "ok");
      }
      refreshSettingsPanel();
    } catch (err) {
      setStatus(err.message, "err");
    }
  }

  async function settingsApplyRateLimit(form) {
    const name = settings.activeNetwork;
    if (!name) return;
    const enabled = !!form.elements["rl_enabled"].checked;
    if (!enabled) {
      const ok = window.confirm(
        "Disable outgoing rate limiting? IRC servers may kill or K-line the bot under bursty traffic."
      );
      if (!ok) return;
    }
    const body = {
      rate_limit: {
        enabled,
        burst: parseInt(form.elements["rl_burst"].value || "5", 10),
        period: parseFloat(form.elements["rl_period"].value || "2.0"),
      },
    };
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}`, { method: "PATCH", body });
      setStatus(`${name}: rate limit updated`, enabled ? "ok" : "warn");
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  async function settingsAddServer(form) {
    const name = settings.activeNetwork;
    if (!name) return;
    const fd = new FormData(form);
    const body = {
      host: fd.get("host"),
      port: parseInt(fd.get("port") || "6697", 10),
      tls: !!form.elements["tls"].checked,
      tls_verify: !!form.elements["tls_verify"].checked,
      is_default: !!form.elements["is_default"].checked,
    };
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}/servers`, { method: "POST", body });
      form.reset();
      setStatus(`${name}: server added`, "ok");
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  async function settingsServerAction(action, index) {
    const name = settings.activeNetwork;
    if (!name) return;
    try {
      if (action === "server-default") {
        await api(`/api/settings/networks/${encodeURIComponent(name)}/servers/${index}/default`, { method: "POST" });
      } else if (action === "server-remove") {
        await api(`/api/settings/networks/${encodeURIComponent(name)}/servers/${index}`, { method: "DELETE" });
      }
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  async function settingsUpdateServer(form) {
    const name = settings.activeNetwork;
    if (!name) return;
    const index = parseInt(form.dataset.index, 10);
    const net = (settings.data && settings.data.networks || []).find((n) => n.name === name);
    const existing = net && net.servers && net.servers[index];
    const body = {
      host: form.elements["host"].value.trim(),
      port: parseInt(form.elements["port"].value || "6697", 10),
      tls: !!form.elements["tls"].checked,
      tls_verify: !!form.elements["tls_verify"].checked,
      is_default: !!(existing && existing.is_default),
    };
    if (!body.host) {
      setStatus("host required", "err");
      return;
    }
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}/servers/${index}`, {
        method: "PATCH", body,
      });
      setStatus(`${name}: server updated`, "ok");
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  function normalizeChannelName(raw) {
    const trimmed = (raw || "").trim();
    if (!trimmed) return "";
    return "#&!+".includes(trimmed[0]) ? trimmed : "#" + trimmed;
  }

  async function settingsAddChannel(form) {
    const name = settings.activeNetwork;
    if (!name) return;
    const ch = normalizeChannelName(form.elements["channel"].value);
    if (!ch) return;
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}/channels`, {
        method: "POST", body: { channel: ch },
      });
      form.reset();
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  async function settingsRemoveChannel(channel) {
    const name = settings.activeNetwork;
    if (!name) return;
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}/channels/${encodeURIComponent(channel)}`,
                { method: "DELETE" });
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  async function settingsConnect() {
    const name = settings.activeNetwork;
    if (!name) return;
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}/connect`, { method: "POST" });
      setStatus(`${name}: connecting…`, "ok");
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  async function settingsDisconnect() {
    const name = settings.activeNetwork;
    if (!name) return;
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}/disconnect`, { method: "POST" });
      setStatus(`${name}: disconnecting…`, "ok");
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  let _removeArmTimer = null;
  function settingsClearRemoveArm() {
    const rmBtn = $("settings-remove-btn");
    if (rmBtn) {
      rmBtn.classList.remove("is-armed");
      rmBtn.textContent = "Remove network";
    }
    if (_removeArmTimer) {
      clearTimeout(_removeArmTimer);
      _removeArmTimer = null;
    }
  }
  async function settingsRemoveNetwork() {
    const name = settings.activeNetwork;
    if (!name) return;
    const rmBtn = $("settings-remove-btn");
    if (!rmBtn) return;
    if (!rmBtn.classList.contains("is-armed")) {
      rmBtn.classList.add("is-armed");
      rmBtn.textContent = `Confirm remove ${name}`;
      _removeArmTimer = setTimeout(settingsClearRemoveArm, 4000);
      return;
    }
    settingsClearRemoveArm();
    try {
      await api(`/api/settings/networks/${encodeURIComponent(name)}`, { method: "DELETE" });
      settings.activeNetwork = null;
      refreshSettingsPanel();
    } catch (err) { setStatus(err.message, "err"); }
  }

  function settingsShowCreate() {
    const panel = $("settings-create");
    const err = $("settings-create-error");
    const form = $("settings-create-form");
    if (!panel || !form) return;
    settings.activeNetwork = null;
    renderSettingsList();
    renderSettingsBody();
    panel.hidden = false;
    if (err) { err.hidden = true; err.textContent = ""; }
    form.reset();
    const first = form.querySelector('input[name="name"]');
    if (first) first.focus();
  }
  function settingsHideCreate() {
    const panel = $("settings-create");
    const err = $("settings-create-error");
    const form = $("settings-create-form");
    if (panel) panel.hidden = true;
    if (err) { err.hidden = true; err.textContent = ""; }
    if (form) form.reset();
  }
  async function settingsCreateSubmit(form) {
    const name = form.elements["name"].value.trim();
    const nick = form.elements["nick"].value.trim();
    const err = $("settings-create-error");
    if (err) { err.hidden = true; err.textContent = ""; }
    if (!name || !nick) return;
    try {
      await api("/api/settings/networks", {
        method: "POST",
        body: { name, nick, servers: [], channels: [] },
      });
      settings.activeNetwork = name;
      settingsHideCreate();
      refreshSettingsPanel();
    } catch (e) {
      if (err) {
        err.textContent = e.message;
        err.hidden = false;
      } else {
        setStatus(e.message, "err");
      }
    }
  }

  async function settingsSaveToDisk() {
    const btn = $("settings-save-btn");
    const feedback = $("settings-save-feedback");
    if (btn) btn.disabled = true;
    if (feedback) { feedback.textContent = "saving…"; feedback.classList.remove("is-err"); }
    try {
      const r = await api("/api/settings/save", { method: "POST" });
      const stamp = new Date().toLocaleTimeString();
      const path = r.path || "config.toml";
      if (feedback) feedback.textContent = `saved ${stamp} → ${path}`;
      setStatus(`saved → ${path}`, "ok");
      refreshSettingsPanel();
    } catch (err) {
      if (feedback) {
        feedback.textContent = err.message;
        feedback.classList.add("is-err");
      }
      setStatus(err.message, "err");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function settingsReloadFromDisk() {
    if (!window.confirm("Discard unsaved runtime edits and reload config.toml from disk?")) return;
    const btn = $("settings-reload-btn");
    const feedback = $("settings-save-feedback");
    if (btn) btn.disabled = true;
    if (feedback) { feedback.textContent = "reloading…"; feedback.classList.remove("is-err"); }
    try {
      const r = await api("/api/settings/reload", { method: "POST" });
      const stamp = new Date().toLocaleTimeString();
      const path = (r && r.config_path) || "config.toml";
      if (feedback) feedback.textContent = `reloaded ${stamp} ← ${path}`;
      setStatus(`reloaded ← ${path}`, "ok");
      refreshSettingsPanel();
      loadNetworks();
    } catch (err) {
      if (feedback) {
        feedback.textContent = err.message;
        feedback.classList.add("is-err");
      }
      setStatus(err.message, "err");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function wireSettingsUI() {
    const list = $("settings-network-list");
    if (list) {
      list.addEventListener("click", (ev) => {
        const row = ev.target.closest(".settings-net-row");
        if (!row) return;
        settings.activeNetwork = row.dataset.network;
        settingsHideCreate();
        settingsClearRemoveArm();
        renderSettingsList();
        renderSettingsBody();
      });
    }
    const addNetBtn = $("settings-add-network");
    if (addNetBtn) addNetBtn.addEventListener("click", settingsShowCreate);
    const cancelCreate = $("settings-create-cancel");
    if (cancelCreate) cancelCreate.addEventListener("click", settingsHideCreate);
    const connectBtn = $("settings-connect-btn");
    if (connectBtn) connectBtn.addEventListener("click", settingsConnect);
    const discBtn = $("settings-disconnect-btn");
    if (discBtn) discBtn.addEventListener("click", settingsDisconnect);
    const rmBtn = $("settings-remove-btn");
    if (rmBtn) rmBtn.addEventListener("click", settingsRemoveNetwork);
    const saveBtn = $("settings-save-btn");
    if (saveBtn) saveBtn.addEventListener("click", settingsSaveToDisk);
    const reloadBtn = $("settings-reload-btn");
    if (reloadBtn) reloadBtn.addEventListener("click", settingsReloadFromDisk);

    document.addEventListener("click", (ev) => {
      const rm = $("settings-remove-btn");
      if (rm && rm.classList.contains("is-armed") && !ev.target.closest("#settings-remove-btn")) {
        settingsClearRemoveArm();
      }
    });

    document.addEventListener("change", (ev) => {
      if (ev.target && ev.target.name === "auth_method" && ev.target.closest("#settings-identity-form")) {
        const method = ev.target.value;
        const stub = { method };
        renderAuthExtra(stub);
      }
    });

    document.addEventListener("submit", (ev) => {
      if (ev.target.id === "settings-identity-form") {
        ev.preventDefault();
        const reconnect = !!(ev.submitter && ev.submitter.dataset.intent === "reconnect");
        settingsApplyIdentity(ev.target, { reconnect });
      }
      else if (ev.target.id === "settings-add-server-form") { ev.preventDefault(); settingsAddServer(ev.target); }
      else if (ev.target.id === "settings-add-channel-form") { ev.preventDefault(); settingsAddChannel(ev.target); }
      else if (ev.target.id === "settings-ratelimit-form") { ev.preventDefault(); settingsApplyRateLimit(ev.target); }
      else if (ev.target.id === "settings-create-form") { ev.preventDefault(); settingsCreateSubmit(ev.target); }
      else if (ev.target.classList && ev.target.classList.contains("server-edit-form")) {
        ev.preventDefault();
        settingsUpdateServer(ev.target);
      }
    });

    document.addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-settings-action]");
      if (!b) return;
      const action = b.dataset.settingsAction;
      if (action === "channel-remove") settingsRemoveChannel(b.dataset.channel);
      else if (action === "server-default" || action === "server-remove") {
        settingsServerAction(action, parseInt(b.dataset.index, 10));
      }
    });
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
    sock.onopen = () => {
      wsState("online");
      // Resync after reconnect: events missed while the socket was down would
      // otherwise leave topology stale (no periodic poll to backstop it).
      if (state.wsEverConnected) loadNetworks();
      state.wsEverConnected = true;
    };
    sock.onmessage = (m) => {
      let payload = null;
      try { payload = JSON.parse(m.data); } catch { return; }
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
    const isStatus = target === "*";
    if (text.startsWith("/") && !text.startsWith("//")) {
      return runSlashCommand(net, target, text.slice(1));
    }
    if (isStatus) {
      errorLine("status buffer accepts slash commands only (try /help)");
      return;
    }
    if (text.startsWith("//")) {
      // escape: send literal slash message
      return sendPlain(net, target, text.slice(1));
    }
    return sendPlain(net, target, text);
  }

  async function sendPlain(net, target, text) {
    recordPendingEcho(net, target, "msg", text);
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
    recordPendingEcho(net, target, "action", body);
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
          recordPendingEcho(net, t, "msg", body);
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
      const closeBtn = ev.target.closest(".ch-close");
      if (closeBtn) {
        ev.stopPropagation();
        const row = closeBtn.closest(".ch-row");
        if (row) closeQuery(row.dataset.net, row.dataset.target);
        return;
      }
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
        if (action === "open-schedules") {
          await openModuleSchedules(b);
          return;
        }
        if (action === "schedule-pause") {
          await api(`/api/schedules/${encodeURIComponent(b.dataset.id)}/pause`, { method: "POST" });
          if (moduleRefForScheduleButton(b)) await loadModuleSchedulesIntoDialog();
          preserveScroll(refreshAdminPanels);
          return;
        }
        if (action === "schedule-resume") {
          await api(`/api/schedules/${encodeURIComponent(b.dataset.id)}/resume`, { method: "POST" });
          if (moduleRefForScheduleButton(b)) await loadModuleSchedulesIntoDialog();
          preserveScroll(refreshAdminPanels);
          return;
        }
        if (action === "schedule-run-now") {
          await api(`/api/schedules/${encodeURIComponent(b.dataset.id)}/run-now`, { method: "POST" });
          if (moduleRefForScheduleButton(b)) await loadModuleSchedulesIntoDialog();
          return;
        }
        if (action === "schedule-cancel") {
          if (!window.confirm("Cancel this schedule? It will be marked cancelled and stop firing.")) return;
          await api(`/api/schedules/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" });
          if (moduleRefForScheduleButton(b)) await loadModuleSchedulesIntoDialog();
          preserveScroll(refreshAdminPanels);
          return;
        }
        if (action === "delete-repo") {
          await api("/api/repos/" + encodeURIComponent(b.dataset.name), { method: "DELETE" });
        } else if (action === "delete-acl") {
          await api("/api/acl/" + encodeURIComponent(b.dataset.id), { method: "DELETE" });
        } else if (["enable", "disable", "reload", "unload"].includes(action)) {
          await api("/api/modules/" + action, { method: "POST", body: { repo: b.dataset.repo, name: b.dataset.name } });
        } else if (action === "open-settings") {
          await openModuleSettings(b);
          return;
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
  async function reloadOpenModule() {
    const els = getDialogEls();
    const repo = moduleSettingsDialog.repo;
    const name = moduleSettingsDialog.name;
    if (!repo || !name || !els.reload) return;
    const btn = els.reload;
    btn.disabled = true;
    btn.classList.remove("is-ok", "is-err");
    btn.classList.add("is-busy");
    btn.textContent = "reloading…";
    els.feedback.textContent = `reloading ${repo}/${name}…`;
    els.feedback.classList.remove("is-err", "is-ok");
    try {
      await api("/api/modules/reload", { method: "POST", body: { repo, name } });
      btn.classList.remove("is-busy");
      btn.classList.add("is-ok");
      btn.textContent = "✓ reloaded";
      els.feedback.textContent = `${repo}/${name} reloaded.`;
      els.feedback.classList.add("is-ok");
      preserveScroll(refreshAdminPanels);
      setTimeout(() => {
        btn.classList.remove("is-ok");
        btn.textContent = "Reload";
        btn.disabled = false;
      }, 1800);
    } catch (err) {
      btn.classList.remove("is-busy");
      btn.classList.add("is-err");
      btn.textContent = "✗ failed";
      els.feedback.textContent = "Reload failed: " + err.message;
      els.feedback.classList.add("is-err");
      setTimeout(() => {
        btn.classList.remove("is-err");
        btn.textContent = "Reload";
        btn.disabled = false;
      }, 2400);
    }
  }

  function wireModuleSettingsDialog() {
    const saveBtn = document.getElementById("module-settings-save");
    const reloadBtn = document.getElementById("module-settings-reload");
    const dlg = document.getElementById("module-settings-dialog");
    if (saveBtn) saveBtn.addEventListener("click", saveOpenModuleSettings);
    if (reloadBtn) reloadBtn.addEventListener("click", reloadOpenModule);
    if (!dlg) return;
    dlg.addEventListener("close", () => {
      moduleSettingsDialog.repo = null;
      moduleSettingsDialog.name = null;
      moduleSettingsDialog.declared = false;
    });
    dlg.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-settings-close]")) { dlg.close(); return; }
      // click on backdrop (dialog element itself) closes
      if (ev.target === dlg) dlg.close();
    });
    dlg.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      const tag = ev.target && ev.target.tagName;
      if (tag === "INPUT" && ev.target.type !== "checkbox") {
        ev.preventDefault();
        saveOpenModuleSettings();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireUI();
    wireSettingsUI();
    wireModuleSettingsDialog();
    wireModuleSchedulesDialog();
    updateAuthBadge();
    if (!state.token) {
      setStatus("Set an API token to populate panels.", "err");
      openTokenDialog();
    } else {
      loadNetworks();
      reconnectEvents();
      refreshAdminPanels();
    }
  });
})();
