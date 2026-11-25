// Control Panel: shared folders, users, file services, info.
WM.register("control", {
  name: "Control Panel", icon: "⚙️", color: "#4f6d8a",
  open(body) {
    sectioned({
      general: { label: "General", render: cpGeneral },
      security: { label: "Security", render: cpSecurity },
      shares: { label: "Shared Folders", render: cpShares },
      users: { label: "Users & Groups", render: cpUsers },
      services: { label: "File Services", render: cpServices },
      info: { label: "Info Center", render: cpInfo },
    }, body);
  },
});

async function cpShares(p) {
  const [shares, pools] = await Promise.all([API.get("/api/shares"), API.get("/api/storage/pools")]);
  const reload = () => { p.replaceChildren(); cpShares(p); };
  p.append(h("h2", {}, "Shared Folders"),
    h("div", { class: "toolbar" }, h("button", { class: "btn primary", disabled: !pools.length, onclick: () => shareForm(p, pools, reload) }, "+ Create")),
    !pools.length ? h("div", { class: "note warn" }, "Create a storage pool in Storage Manager first.") : null,
    h("table", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Location"), h("th", {}, "Access"), h("th", {})),
      shares.map((s) => h("tr", {},
        h("td", {}, h("b", {}, s.name), h("div", { class: "muted" }, s.description)),
        h("td", {}, s.path),
        h("td", {}, s.smb ? h("span", { class: "pill" }, "SMB") : null, " ", s.nfs ? h("span", { class: "pill" }, "NFS") : null,
          s.recycle_bin ? h("span", { class: "pill" }, "Recycle bin") : null),
        h("td", {},
          h("button", { class: "btn", onclick: () => act(() => API.post(`/api/shares/${s.name}/snapshots`), "Snapshot taken") }, "Snapshot"), " ",
          h("button", { class: "btn danger", onclick: async () => {
            if (!confirm(`Remove share ${s.name}? (data is kept)`)) return;
            await act(() => API.del(`/api/shares/${s.name}`), "Share removed"); reload();
          } }, "Remove"))))));
}

function shareForm(p, pools, done) {
  const f = { name: "", description: "", pool_id: pools[0].id, smb: true, nfs: false, recycle_bin: true };
  p.replaceChildren(h("h2", {}, "Create shared folder"), h("div", { class: "form" },
    h("label", {}, "Name", h("input", { oninput: (e) => (f.name = e.target.value) })),
    h("label", {}, "Description", h("input", { oninput: (e) => (f.description = e.target.value) })),
    h("label", {}, "Location", h("select", { onchange: (e) => (f.pool_id = +e.target.value) },
      pools.map((x) => h("option", { value: x.id }, `${x.label} (${x.mount})`)))),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: true, onchange: (e) => (f.smb = e.target.checked) }), "Share over SMB (Windows / macOS)"),
    h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => (f.nfs = e.target.checked) }), "Share over NFS (Linux)"),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: true, onchange: (e) => (f.recycle_bin = e.target.checked) }), "Enable recycle bin"),
    h("div", { class: "toolbar" },
      h("button", { class: "btn", onclick: done }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => { await act(() => API.post("/api/shares", f), "Shared folder created"); done(); } }, "Create"))));
}

async function cpUsers(p) {
  const users = await API.get("/api/users");
  const reload = () => { p.replaceChildren(); cpUsers(p); };
  const nu = { name: "", full_name: "", password: "", admin: false };
  p.append(h("h2", {}, "Users & Groups"),
    h("table", {}, h("tr", {}, h("th", {}, "User"), h("th", {}, "Full name"), h("th", {}, "Groups"), h("th", {})),
      users.map((u) => h("tr", {}, h("td", {}, h("b", {}, u.name)), h("td", {}, u.full_name), h("td", {}, u.groups.join(", ")),
        h("td", {}, h("button", { class: "btn danger", onclick: async () => {
          if (!confirm(`Delete user ${u.name}?`)) return;
          await act(() => API.del(`/api/users/${u.name}`), "User deleted"); reload();
        } }, "Delete"))))),
    h("h2", { style: "margin-top:24px" }, "Add user"),
    h("div", { class: "form" },
      h("label", {}, "Username", h("input", { oninput: (e) => (nu.name = e.target.value) })),
      h("label", {}, "Full name", h("input", { oninput: (e) => (nu.full_name = e.target.value) })),
      h("label", {}, "Password (8+ characters)", h("input", { type: "password", oninput: (e) => (nu.password = e.target.value) })),
      h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => (nu.admin = e.target.checked) }), "Administrator"),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => { await act(() => API.post("/api/users", nu), "User created"); reload(); } }, "Create user"))));
}

async function cpServices(p) {
  const svcs = await API.get("/api/services");
  p.append(h("h2", {}, "Services"), h("table", {},
    svcs.map((s) => h("tr", {}, h("td", {}, h("b", {}, s.label), h("div", { class: "muted" }, s.unit)),
      h("td", {}, h("span", { class: "pill " + (s.running ? "ok" : "") }, s.running ? "Running" : "Stopped")),
      h("td", {}, h("button", { class: "btn", onclick: async () => {
        await act(() => API.post(`/api/services/${s.id}/${s.running ? "disable" : "enable"}`), `${s.label} ${s.running ? "stopped" : "started"}`);
        p.replaceChildren(); cpServices(p);
      } }, s.running ? "Disable" : "Enable"))))));
}

async function cpInfo(p) {
  const i = await API.get("/api/system/info");
  const row = (k, v) => h("tr", {}, h("th", {}, k), h("td", {}, v));
  p.append(h("h2", {}, "Info Center"), h("table", {},
    row("Server name", i.hostname), row("OS", i.os), row("Kernel", i.kernel), row("CPU", `${i.cpu} (${i.cores} threads)`),
    row("Memory", fmtBytes(i.memory_total)), row("Uptime", fmtUptime(i.uptime))));
}

async function cpGeneral(p) {
  const g = await API.get("/api/setup/general");
  const f = { hostname: g.hostname, timezone: g.timezone, ntp: g.ntp };
  p.append(h("h2", {}, "General"), h("div", { class: "form" },
    h("label", {}, "Server name", h("input", { value: f.hostname, oninput: (e) => (f.hostname = e.target.value.trim()) })),
    h("label", {}, "Time zone", h("input", { list: "cp-tz", value: f.timezone, oninput: (e) => (f.timezone = e.target.value.trim()) }),
      h("datalist", { id: "cp-tz" }, g.timezones.map((z) => h("option", { value: z })))),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.ntp, onchange: (e) => (f.ntp = e.target.checked) }), "Sync time with internet time servers (NTP)"),
    h("div", { class: "muted" }, "IP addresses: ", g.ips.filter((i) => i !== "127.0.0.1" && i !== "::1").join(", ")),
    h("div", { class: "toolbar" },
      h("button", { class: "btn primary", onclick: async () => {
        const r = await act(() => API.put("/api/setup/general", f), "Saved");
        if (r.restarting) { toast("Server name changed - reconnecting…"); await waitForServer(); }
      } }, "Apply"),
      h("button", { class: "btn", onclick: runSetupWizard }, "Run setup wizard again"))));
}

async function cpSecurity(p) {
  const s = await API.get("/api/security");
  const c = s.cert;
  const reload = () => { p.replaceChildren(); cpSecurity(p); };
  const row = (k, v) => h("tr", {}, h("th", {}, k), h("td", {}, v));
  const certPem = h("textarea", { placeholder: "-----BEGIN CERTIFICATE-----\n… (certificate, then any intermediate certificates)" });
  const keyPem = h("textarea", { placeholder: "-----BEGIN PRIVATE KEY-----\n…" });
  p.append(h("h2", {}, "Security"),
    h("h3", { class: "pc-group" }, "HTTPS"),
    h("div", { class: "form" },
      h("div", {}, `HTTP on port ${s.http_port}, HTTPS on port ${s.https_port}.`),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: s.https_redirect, onchange: async (e) => {
        await act(() => API.put("/api/security", { https_redirect: e.target.checked }), e.target.checked ? "HTTP now redirects to HTTPS" : "Redirect off");
        if (e.target.checked && location.protocol === "http:") switchToHttps(false);
      } }), "Redirect HTTP connections to HTTPS")),
    h("h3", { class: "pc-group", style: "margin-top:20px" }, "Certificate"),
    c.present ? h("table", {},
      row("Type", h("span", { class: "pill " + (c.kind === "uploaded" ? "ok" : "") }, c.kind)),
      row("Issued to", c.subject), row("Issued by", c.issuer),
      row("Expires", h("span", { class: c.days_left < 30 ? "err" : "" }, `${c.expires} (${c.days_left} days)`)),
      row("Valid for", h("span", { class: "mono" }, c.san.join(", "))))
      : h("p", { class: "muted" }, "No certificate yet."),
    h("div", { class: "toolbar", style: "margin-top:10px" },
      h("button", { class: "btn", onclick: async () => {
        if (!confirm("Create a new self-signed certificate? The web server restarts and browsers will warn once more.")) return;
        await act(() => API.post("/api/security/cert/self-signed"), "New certificate - reconnecting…");
        await waitForServer(); reload();
      } }, "Create new self-signed certificate")),
    h("h3", { class: "pc-group", style: "margin-top:20px" }, "Use your own certificate"),
    h("div", { class: "form", style: "max-width:720px" },
      h("div", { class: "muted" }, "Paste PEM text, e.g. from Let's Encrypt (fullchain.pem + privkey.pem)."),
      h("label", {}, "Certificate (+ intermediates)", certPem), h("label", {}, "Private key", keyPem),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => {
        await act(() => API.post("/api/security/cert/upload", { cert: certPem.value, key: keyPem.value }), "Certificate installed - reconnecting…");
        await waitForServer(); reload();
      } }, "Install certificate"))));
}
