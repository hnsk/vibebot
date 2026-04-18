(function () {
  const tokenKey = "vibebot_token";
  function token() {
    const t = localStorage.getItem(tokenKey);
    if (t) return t;
    const prompted = window.prompt("Enter vibebot API token");
    if (prompted) localStorage.setItem(tokenKey, prompted);
    return prompted || "";
  }

  async function api(path, init) {
    init = init || {};
    init.headers = Object.assign({ Authorization: "Bearer " + token() }, init.headers || {});
    if (init.body && typeof init.body !== "string") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(init.body);
    }
    const r = await fetch(path, init);
    if (!r.ok) throw new Error(path + " → " + r.status);
    const ct = r.headers.get("Content-Type") || "";
    return ct.includes("json") ? r.json() : r.text();
  }

  const renderers = {
    network(n) {
      return `<div class="card">
        <h3>${n.name}${n.connected ? " ✓" : ""}</h3>
        <div class="muted">${n.host}:${n.port}${n.tls ? " TLS" : ""}</div>
        <div class="muted">Channels: ${n.channels.join(", ") || "—"}</div>
      </div>`;
    },
    module(m) {
      return `<div class="card">
        <h3>${m.repo}/${m.name}</h3>
        <div class="muted">${m.description || "(no description)"} — ${m.enabled ? "enabled" : "disabled"}</div>
        <div class="actions">
          <button data-action="${m.enabled ? "disable" : "enable"}" data-repo="${m.repo}" data-name="${m.name}">${m.enabled ? "Disable" : "Enable"}</button>
          <button data-action="reload" data-repo="${m.repo}" data-name="${m.name}">Reload</button>
          <button data-action="unload" data-repo="${m.repo}" data-name="${m.name}">Unload</button>
        </div>
      </div>`;
    },
    repo(r) {
      return `<div class="card">
        <h3>${r.name}</h3>
        <div class="muted">${r.url} @ ${r.branch}${r.enabled ? "" : " (disabled)"}</div>
        <div class="actions">
          <button data-action="pull-repo" data-name="${r.name}">Pull</button>
          <button data-action="delete-repo" data-name="${r.name}">Delete</button>
        </div>
      </div>`;
    },
  };

  async function loadPanel(el) {
    const tpl = renderers[el.dataset.template];
    if (!tpl) return;
    try {
      const data = await api(el.dataset.api);
      el.innerHTML = `<div class="row">${data.map(tpl).join("")}</div>`;
    } catch (err) {
      el.innerHTML = `<div class="muted">Error: ${err.message}</div>`;
    }
  }

  async function refreshAll() {
    document.querySelectorAll("[data-api]").forEach(loadPanel);
  }

  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("button[data-action]");
    if (!b) return;
    const repo = b.dataset.repo;
    const name = b.dataset.name;
    const action = b.dataset.action;
    try {
      if (action === "delete-repo") await api("/api/repos/" + encodeURIComponent(name), { method: "DELETE" });
      else if (action === "pull-repo") await api("/api/repos/" + encodeURIComponent(name) + "/pull", { method: "POST" });
      else await api("/api/modules/" + action, { method: "POST", body: { repo, name } });
      await refreshAll();
    } catch (err) {
      alert(err.message);
    }
  });

  document.addEventListener("submit", async (ev) => {
    if (ev.target.id === "send-form") {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      try {
        await api("/api/networks/" + encodeURIComponent(fd.get("network")) + "/send", {
          method: "POST",
          body: { target: fd.get("target"), message: fd.get("message") },
        });
        ev.target.reset();
      } catch (err) {
        alert(err.message);
      }
    } else if (ev.target.id === "repo-form") {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      try {
        await api("/api/repos", {
          method: "POST",
          body: {
            name: fd.get("name"),
            url: fd.get("url"),
            branch: fd.get("branch") || "main",
            enabled: true,
          },
        });
        ev.target.reset();
        await refreshAll();
      } catch (err) {
        alert(err.message);
      }
    }
  });

  function connectEvents() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + "/ws/events?token=" + encodeURIComponent(token());
    const log = document.getElementById("events-log");
    const ws = new WebSocket(url);
    ws.onmessage = (m) => {
      log.textContent = (m.data + "\n" + log.textContent).slice(0, 8000);
    };
    ws.onclose = () => setTimeout(connectEvents, 3000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    refreshAll();
    connectEvents();
    setInterval(refreshAll, 5000);
  });
})();
