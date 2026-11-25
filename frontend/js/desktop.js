// Login, desktop icons, main menu, widgets, clock.
const DESKTOP_APPS = ["files", "storage", "control", "appcenter", "monitor"];

async function boot() {
  // session handed over from the HTTP origin by the setup wizard (see setup.js)
  const frag = new URLSearchParams(location.hash.slice(1));
  if (frag.get("token")) {
    API.token = frag.get("token");
    sessionStorage.setItem("cyn-token", API.token);
    history.replaceState(null, "", location.pathname);
    if (frag.get("open") === "create-pool") boot.after = () => openCreatePool();
  }
  if (API.token) {
    try { const me = await API.get("/api/auth/me"); return startDesktop(me); } catch { /* fall through to login */ }
  }
  document.getElementById("login-host").textContent = location.hostname;
  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("login-err");
    err.textContent = "";
    try {
      const r = await API.post("/api/auth/login", {
        username: document.getElementById("login-user").value,
        password: document.getElementById("login-pass").value,
      });
      API.token = r.token;
      sessionStorage.setItem("cyn-token", r.token);
      startDesktop(r);
    } catch (ex) { err.textContent = ex.message; }
  });
}

function launcher(id) {
  const a = WM.apps[id];
  return h("button", { class: "launch", onclick: () => { document.getElementById("menu").classList.add("hidden"); WM.open(id); } },
    h("div", { class: "app-ico", style: `background:${a.color}` }, a.icon), a.name);
}

function startDesktop(me) {
  document.getElementById("login").classList.add("hidden");
  document.getElementById("desktop").classList.remove("hidden");
  document.getElementById("user-btn").textContent = "👤 " + me.user;
  document.getElementById("icons").append(...DESKTOP_APPS.map(launcher));
  document.getElementById("menu").append(...Object.keys(WM.apps).map(launcher));

  const toggle = (id) => (e) => { e.stopPropagation(); document.getElementById(id).classList.toggle("hidden"); };
  document.getElementById("menu-btn").onclick = toggle("menu");
  document.getElementById("user-btn").onclick = toggle("user-menu");
  document.addEventListener("click", () => document.querySelectorAll(".menu").forEach((m) => m.classList.add("hidden")));
  document.getElementById("user-menu").onclick = async (e) => {
    const a = e.target.dataset.act;
    if (a === "logout") { await API.post("/api/auth/logout"); sessionStorage.removeItem("cyn-token"); location.reload(); }
    else if (a && confirm(`Really ${a === "reboot" ? "restart" : "shut down"} the server?`)) act(() => API.post(`/api/system/power/${a}`), "Command sent");
  };

  const clock = () => (document.getElementById("clock").textContent = new Date().toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }));
  clock(); setInterval(clock, 15000);
  widgets(); setInterval(widgets, 5000);
  if (boot.after) boot.after();
  else if (me.admin) API.get("/api/setup/status").then((s) => s.needs_setup && runSetupWizard());
}

async function widgets() {
  const [info, stats, pools, health] = await Promise.all([API.get("/api/system/info"), API.get("/api/system/stats"), API.get("/api/storage/pools"), API.get("/api/health")]);
  const bad = health.status !== "healthy";
  const meter = (label, pct) => h("div", { style: "margin:6px 0" }, h("div", { style: "display:flex;justify-content:space-between;font-size:12px" }, h("span", {}, label), h("span", {}, `${pct.toFixed(0)}%`)), h("div", { class: "bar" }, h("div", { style: `width:${pct}%` })));
  document.getElementById("widgets").replaceChildren(
    h("div", { class: "widget" }, h("h4", {}, "System Health"),
      h("div", {}, h("span", { class: "pill " + (health.status === "critical" ? "bad" : bad ? "warn" : "ok") }, bad ? "Attention needed" : "Healthy")),
      ...health.issues.map((i) => h("div", { style: "font-size:12px;margin-top:6px", title: i.detail }, (i.level === "critical" ? "⛔ " : "⚠️ ") + i.title)),
      h("div", { class: "muted", style: "font-size:12px;margin-top:6px" }, `${info.hostname} · up ${fmtUptime(info.uptime)}`)),
    h("div", { class: "widget" }, h("h4", {}, "Resources"), meter("CPU", stats.cpu), meter("Memory", stats.mem.percent)),
    h("div", { class: "widget" }, h("h4", {}, "Storage"),
      pools.length ? pools.map((p) => meter(`${p.label} (${p.raid.toUpperCase()})`, p.total ? (100 * p.used) / p.total : 0))
        : h("div", { class: "muted", style: "font-size:12px" }, "No storage pools yet")));
}

boot();
