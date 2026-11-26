// Login, desktop icons, main menu, widgets, clock.
const DESKTOP_APPS = ["files", "storage", "control", "appcenter", "containers", "backup", "monitor"];

async function boot() {
  // session handed over from the HTTP origin by the setup wizard (see setup.js)
  const frag = new URLSearchParams(location.hash.slice(1));
  if (frag.get("token")) {
    API.token = frag.get("token");
    sessionStorage.setItem("cyn-token", API.token);
    history.replaceState(null, "", location.pathname);
    if (frag.get("open") === "create-pool") boot.after = () => openCreatePool();
    if (frag.get("net-confirm")) boot.after = () => act(() => API.post("/api/network/confirm"), "Network settings saved - you are on the new address");
  }
  if (API.token) {
    try { const me = await API.get("/api/auth/me"); return startDesktop(me); } catch { /* fall through to login */ }
  }
  document.getElementById("login-host").textContent = location.hostname;
  let ticket = null;
  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("login-err");
    err.textContent = "";
    try {
      let r;
      if (!ticket) {
        r = await API.post("/api/auth/login", {
          username: document.getElementById("login-user").value,
          password: document.getElementById("login-pass").value,
        });
        if (r.need_otp) {   // password OK - now the 2-step code
          ticket = r.ticket;
          document.getElementById("login-step1").classList.add("hidden");
          document.getElementById("login-step2").classList.remove("hidden");
          document.getElementById("login-code").focus();
          return;
        }
      } else {
        try { r = await API.post("/api/auth/otp", { ticket, code: document.getElementById("login-code").value }); }
        catch (ex) {
          if (/expired|Too many/.test(ex.message)) { ticket = null; document.getElementById("login-step2").classList.add("hidden"); document.getElementById("login-step1").classList.remove("hidden"); }
          document.getElementById("login-code").value = "";
          throw ex;
        }
      }
      API.token = r.token;
      sessionStorage.setItem("cyn-token", r.token);
      startDesktop(r);
    } catch (ex) { err.textContent = ex.message; }
  });
}

function launcher(id) {
  const a = WM.apps[id];
  return h("button", { class: "launch", onclick: () => { document.getElementById("menu").classList.add("hidden"); WM.open(id); } },
    appTile(id, 48), a.name);
}

function startDesktop(me) {
  document.getElementById("login").classList.add("hidden");
  if (me.enroll_required) return requiredTwoFactor(me);
  document.getElementById("desktop").classList.remove("hidden");
  document.getElementById("user-btn").replaceChildren(svgIcon("user", 18), h("span", {}, me.user));
  document.getElementById("menu-btn").replaceChildren(svgIcon("menu", 18));
  document.getElementById("usb-btn").replaceChildren(svgIcon("eject", 18));
  document.getElementById("icons").append(...DESKTOP_APPS.map(launcher));
  document.getElementById("menu").append(...Object.keys(WM.apps).map(launcher));

  const toggle = (id) => (e) => { e.stopPropagation(); document.getElementById(id).classList.toggle("hidden"); };
  document.getElementById("menu-btn").onclick = toggle("menu");
  document.getElementById("user-btn").onclick = toggle("user-menu");
  document.addEventListener("click", () => document.querySelectorAll(".menu").forEach((m) => m.classList.add("hidden")));
  document.getElementById("user-menu").onclick = async (e) => {
    const a = e.target.dataset.act;
    if (a === "personal") WM.open("personal");
    else if (a === "logout") { await API.post("/api/auth/logout"); sessionStorage.removeItem("cyn-token"); location.reload(); }
    else if (a && confirm(`Really ${a === "reboot" ? "restart" : "shut down"} the server?`)) act(() => API.post(`/api/system/power/${a}`), "Command sent");
  };

  const clock = () => (document.getElementById("clock").textContent = new Date().toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }));
  clock(); setInterval(clock, 15000);
  widgets(); setInterval(widgets, 5000);
  usbIndicator(); setInterval(usbIndicator, 5000);
  document.getElementById("usb-btn").onclick = toggle("usb-menu");
  if (boot.after) boot.after();
  else if (me.admin) API.get("/api/setup/status").then((s) => s.needs_setup && runSetupWizard());
}

const NET_HIST = { last: null, rx: [], tx: [] };
const UPDATE_INFO = { available: false, checked: 0 };

function openControl(section) {
  WM.open("control");
  setTimeout(() => document.querySelector(`.cp-item[data-k="${section}"]`)?.click(), 50);
}

async function widgets() {
  const [info, stats, pools, health] = await Promise.all([API.get("/api/system/info"), API.get("/api/system/stats"), API.get("/api/storage/pools"), API.get("/api/health")]);
  const bad = health.status !== "healthy";
  // network rate history for the sparkline
  if (NET_HIST.last) {
    const dt = Math.max(1, stats.t - NET_HIST.last.t);
    NET_HIST.rx.push((stats.net.recv - NET_HIST.last.net.recv) / dt); NET_HIST.tx.push((stats.net.sent - NET_HIST.last.net.sent) / dt);
    if (NET_HIST.rx.length > 36) { NET_HIST.rx.shift(); NET_HIST.tx.shift(); }
  }
  NET_HIST.last = stats;
  if (Date.now() - UPDATE_INFO.checked > 3600e3) {     // check hourly (the server caches GitHub too)
    UPDATE_INFO.checked = Date.now();
    API.get("/api/update").then((u) => { UPDATE_INFO.available = !!u.update_available; UPDATE_INFO.version = u.latest?.version; }).catch(() => {});
  }
  const meter = (label, pct) => h("div", { class: "w-meter" }, h("span", {}, label), h("div", { class: "bar" }, h("div", { style: `width:${pct}%` })), h("span", {}, `${pct.toFixed(0)}%`));
  const spark = h("canvas", { class: "w-spark", width: 248, height: 54 });
  const ctx = spark.getContext("2d"), max = Math.max(1024, ...NET_HIST.rx, ...NET_HIST.tx);
  for (const [series, color] of [[NET_HIST.rx, "#0ea5b7"], [NET_HIST.tx, "#3b82f6"]]) {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 1.6;
    series.forEach((v, i) => { const x = (i / 35) * 248, y = 52 - (v / max) * 48; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  }
  const kv = (k, v) => h("div", { class: "w-kv" }, h("span", { class: "muted" }, k), h("span", {}, v));
  document.getElementById("widgets").replaceChildren(
    h("div", { class: "widget" }, h("h4", {}, "System Health"),
      h("div", { class: "w-health " + (health.status === "critical" ? "bad" : bad ? "warn" : "ok") },
        h("div", { class: "w-health-icon" }, bad ? "!" : "✓"),
        h("div", {}, h("div", { class: "w-health-title" }, bad ? "Attention needed" : "Healthy"),
          h("div", { class: "muted", style: "font-size:12px" }, bad ? `${health.issues.length} issue(s)` : "Your NAS is working well."))),
      ...health.issues.slice(0, 3).map((i) => h("div", { class: "w-issue", title: i.detail }, (i.level === "critical" ? "⛔ " : "⚠️ ") + i.title)),
      kv("Server name", info.hostname), kv("LAN", (info.ips || [])[0] || "-"), kv("Uptime", fmtUptime(info.uptime)),
      UPDATE_INFO.available ? h("a", { href: "#", class: "w-update", onclick: (e) => { e.preventDefault(); openControl("update"); } },
        `⬆ Update ${UPDATE_INFO.version} available`) : null),
    h("div", { class: "widget" }, h("h4", {}, "Resource Monitor"), meter("CPU", stats.cpu), meter("RAM", stats.mem.percent),
      h("div", { class: "w-kv" }, h("span", { style: "color:#3b82f6" }, `↑ ${fmtBytes(NET_HIST.tx.at(-1) || 0)}/s`), h("span", { style: "color:#0ea5b7" }, `↓ ${fmtBytes(NET_HIST.rx.at(-1) || 0)}/s`)),
      spark),
    h("div", { class: "widget" }, h("h4", {}, "Storage"),
      pools.length ? pools.map((p) => meter(p.label, p.total ? (100 * p.used) / p.total : 0))
        : h("div", { class: "muted", style: "font-size:12px" }, "No storage pools yet")));
}

boot();

// Taskbar eject button: visible while external drives are connected.
async function usbIndicator() {
  let disks = [];
  try { disks = await API.get("/api/usb"); } catch { return; }
  const btn = document.getElementById("usb-btn"), menu = document.getElementById("usb-menu");
  btn.classList.toggle("hidden", !disks.length);
  if (!disks.length) menu.classList.add("hidden");
  menu.replaceChildren(h("div", { class: "muted", style: "padding:6px 12px;font-size:12px" }, "External devices"),
    ...disks.map((dk) => h("div", { style: "display:flex;align-items:center;gap:8px;padding:6px 12px" },
      h("div", { style: "flex:1" }, h("div", {}, dk.model || dk.name), h("div", { class: "muted", style: "font-size:12px" },
        dk.volumes.filter((v) => v.share).map((v) => v.share).join(", ") || "not mounted")),
      dk.volumes.some((v) => v.share) ? h("button", { class: "btn", onclick: async (e) => {
        e.stopPropagation();
        await act(() => API.post(`/api/usb/${dk.name}/eject`), "Ejected - safe to unplug"); usbIndicator();
      } }, "Eject") : null)));
}
