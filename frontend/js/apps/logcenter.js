// Log Center: system journal, sign-ins, SMB file access (per shared folder) and alerts.
WM.register("logs", {
  name: "Log Center", icon: "📜", color: "#475569", w: 1060, h: 640,
  open(body) {
    sectioned({
      system: { label: "System", render: logSystem },
      signins: { label: "Sign-ins", render: logSignins },
      files: { label: "File access", render: logFiles },
      alerts: { label: "Alerts", render: logAlerts },
    }, body);
  },
});

const logWhen = (t) => new Date(t * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" });

async function logExport(kind) {
  const r = await fetch(`/api/logs/export?kind=${kind}`, { headers: { Authorization: `Bearer ${API.token}` } });
  if (!r.ok) return toast("Export failed");
  const a = h("a", { href: URL.createObjectURL(await r.blob()), download: `cynthology-${kind}-log.csv` });
  document.body.append(a); a.click(); a.remove();
}

function logBar(fields, onChange, kind) {
  return h("div", { class: "toolbar", style: "flex-wrap:wrap" }, ...fields,
    h("button", { class: "btn", onclick: onChange }, "⟳ Refresh"),
    kind ? h("button", { class: "btn", onclick: () => logExport(kind) }, "Export CSV") : null);
}

async function logSystem(p) {
  const f = { priority: "6", unit: "", q: "", since_hours: 24 };
  const units = await API.get("/api/logs/units").catch(() => []);
  const list = h("div");
  const load = async () => {
    list.replaceChildren(h("p", { class: "muted" }, "Loading…"));
    const qs = new URLSearchParams({ ...f, limit: 500 });
    let rows;
    try { rows = await API.get(`/api/logs/system?${qs}`); } catch (e) { return list.replaceChildren(h("div", { class: "note warn" }, e.message)); }
    list.replaceChildren(rows.length ? h("table", { class: "fs-table" },
      h("tr", {}, h("th", { style: "width:160px" }, "Time"), h("th", { style: "width:90px" }, "Level"), h("th", { style: "width:180px" }, "Service"), h("th", {}, "Message")),
      rows.map((r) => h("tr", {}, h("td", { class: "muted" }, logWhen(r.time)),
        h("td", {}, h("span", { class: "pill " + (["emergency", "alert", "critical", "error"].includes(r.priority) ? "bad" : r.priority === "warning" ? "warn" : "") }, r.priority)),
        h("td", { class: "muted", translate: "no" }, r.unit), h("td", { class: "mono", style: "font-size:12px;white-space:pre-wrap;word-break:break-word", translate: "no" }, r.message))))
      : h("p", { class: "muted" }, "Nothing matches."));
  };
  p.append(h("h2", {}, "System log"), logBar([
    h("select", { onchange: (e) => { f.priority = e.target.value; load(); } }, [["3", "Errors"], ["4", "Warnings and errors"], ["5", "Notices and above"], ["6", "Everything (info)"]].map(([v, l]) => h("option", { value: v, selected: f.priority === v }, l))),
    h("select", { onchange: (e) => { f.unit = e.target.value; load(); } }, h("option", { value: "" }, "All services"), units.map((u) => h("option", { value: u, translate: "no" }, u))),
    h("select", { onchange: (e) => { f.since_hours = +e.target.value; load(); } }, [[1, "Last hour"], [24, "Last 24 hours"], [168, "Last 7 days"], [720, "Last 30 days"]].map(([v, l]) => h("option", { value: v, selected: f.since_hours === v }, l))),
    h("input", { type: "search", placeholder: "Search", onkeydown: (e) => { if (e.key === "Enter") { f.q = e.target.value; load(); } } })], load, "system"), list);
  load();
}

async function logSignins(p) {
  const f = { q: "", event: "" };
  const list = h("div");
  const labels = { login: ["ok", "Signed in"], failed: ["bad", "Failed"], "2fa-failed": ["bad", "Wrong 2-step code"], locked: ["warn", "Locked out"], blocked: ["bad", "Address blocked"] };
  const load = async () => {
    const rows = await API.get(`/api/logs/signins?${new URLSearchParams(f)}`);
    list.replaceChildren(rows.length ? h("table", { class: "fs-table" },
      h("tr", {}, h("th", { style: "width:170px" }, "Time"), h("th", { style: "width:160px" }, "Event"), h("th", {}, "User"), h("th", {}, "From"), h("th", {}, "Details")),
      rows.map((r) => { const [cls, txt] = labels[r.event] || ["", r.event];
        return h("tr", {}, h("td", { class: "muted" }, logWhen(r.time)), h("td", {}, h("span", { class: "pill " + cls }, txt)),
          h("td", { translate: "no" }, r.user || "-"), h("td", { class: "mono", translate: "no" }, r.ip), h("td", { class: "muted" }, r.detail || "")); }))
      : h("p", { class: "muted" }, "No sign-ins recorded yet."));
  };
  p.append(h("h2", {}, "Sign-ins"), logBar([
    h("select", { onchange: (e) => { f.event = e.target.value; load(); } }, h("option", { value: "" }, "All events"),
      Object.entries(labels).map(([k, [, l]]) => h("option", { value: k }, l))),
    h("input", { type: "search", placeholder: "User or address", onkeydown: (e) => { if (e.key === "Enter") { f.q = e.target.value; load(); } } })], load, "signins"), list);
  load();
}

async function logFiles(p) {
  const shares = await API.get("/api/shares");
  const f = { q: "", share: "", since_hours: 24 };
  const list = h("div");
  const load = async () => {
    const rows = await API.get(`/api/logs/files?${new URLSearchParams(f)}`);
    list.replaceChildren(rows.length ? h("table", { class: "fs-table" },
      h("tr", {}, h("th", { style: "width:170px" }, "Time"), h("th", { style: "width:110px" }, "User"), h("th", { style: "width:130px" }, "From"), h("th", { style: "width:110px" }, "Action"), h("th", {}, "File")),
      rows.map((r) => h("tr", {}, h("td", { class: "muted" }, logWhen(r.time)), h("td", { translate: "no" }, r.user), h("td", { class: "mono", translate: "no" }, r.ip),
        h("td", {}, h("span", { class: "pill " + (r.action === "delete" ? "bad" : r.action === "write" || r.action === "rename" ? "warn" : "") }, r.action)),
        h("td", { class: "mono", style: "font-size:12px", translate: "no" }, `${r.share}/${r.path}`))))
      : h("p", { class: "muted" }, "No file access recorded (turn it on for a shared folder below)."));
  };
  const toggles = h("div", { style: "display:flex;flex-wrap:wrap;gap:4px 16px;margin:6px 0 14px" }, shares.filter((s) => !s.usb).map((s) =>
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!s.audit, onchange: (e) =>
      act(() => API.put(`/api/shares/${s.name}/audit`, { enabled: e.target.checked }), e.target.checked ? `Logging file access in ${s.name}` : "Off") }),
      h("span", { translate: "no" }, s.name))));
  p.append(h("h2", {}, "File access"),
    h("div", { class: "muted", style: "font-size:13px" }, "Record who opens, changes, renames and deletes files over SMB, per shared folder (it logs every opened file, so turn it on only where you need it):"),
    toggles,
    logBar([
      h("select", { onchange: (e) => { f.share = e.target.value; load(); } }, h("option", { value: "" }, "All shared folders"), shares.map((s) => h("option", { value: s.name, translate: "no" }, s.name))),
      h("select", { onchange: (e) => { f.since_hours = +e.target.value; load(); } }, [[1, "Last hour"], [24, "Last 24 hours"], [168, "Last 7 days"], [720, "Last 30 days"]].map(([v, l]) => h("option", { value: v, selected: f.since_hours === v }, l))),
      h("input", { type: "search", placeholder: "User, file or address", onkeydown: (e) => { if (e.key === "Enter") { f.q = e.target.value; load(); } } })], load, "files"), list);
  load();
}

async function logAlerts(p) {
  const alerts = await API.get("/api/health/alerts").catch(() => []);
  p.append(h("h2", {}, "Alerts"), alerts.length ? h("table", { class: "fs-table" },
    h("tr", {}, h("th", { style: "width:170px" }, "Time"), h("th", { style: "width:110px" }, "Level"), h("th", {}, "Alert")),
    alerts.map((a) => h("tr", {}, h("td", { class: "muted" }, logWhen(a.time)),
      h("td", {}, h("span", { class: "pill " + (a.level === "critical" ? "bad" : a.level === "warning" ? "warn" : "ok") }, a.event === "resolved" ? "resolved" : a.level)),
      h("td", {}, h("b", {}, a.title), a.detail ? h("div", { class: "muted", style: "font-size:12px" }, a.detail) : null))))
    : h("p", { class: "muted" }, "No alerts."));
}
