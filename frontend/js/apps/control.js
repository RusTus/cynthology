// Control Panel: shared folders, users, file services, info.
const CP_SECTIONS = {
  shares: { label: "Shared Folders", group: "File Sharing", icon: "cp_shares", desc: "Folders on your storage pools", render: (p) => cpShares(p) },
  services: { label: "File Services", group: "File Sharing", icon: "cp_services", desc: "SMB, NFS, Time Machine, SSH", render: (p) => cpServices(p) },
  users: { label: "Users & Groups", group: "File Sharing", icon: "cp_users", desc: "Accounts and 2-step status", render: (p) => cpUsers(p) },
  domain: { label: "Domain/LDAP", group: "File Sharing", icon: "cp_domain", desc: "Join an Active Directory domain", render: (p) => cpDomain(p) },
  network: { label: "Network", group: "Connectivity", icon: "cp_network", desc: "IP address, DNS, link aggregation", render: (p) => cpNetwork(p) },
  security: { label: "Security", group: "Connectivity", icon: "cp_security", desc: "HTTPS certificate, Let's Encrypt, 2-step policy", render: (p) => cpSecurity(p) },
  firewall: { label: "Firewall", group: "Connectivity", icon: "cp_firewall", desc: "Allow or block services and addresses, auto-block", render: (p) => cpFirewall(p) },
  vpn: { label: "VPN Server", group: "Connectivity", icon: "cp_vpn", desc: "WireGuard: reach the NAS and your home network from anywhere", render: (p) => cpVpn(p) },
  external: { label: "External Access", group: "Connectivity", icon: "cp_external", desc: "DDNS host name for reaching the NAS from the internet", render: (p) => cpExternal(p) },
  general: { label: "General", group: "System", icon: "cp_general", desc: "Server name, time zone", render: (p) => cpGeneral(p) },
  info: { label: "Info Center", group: "System", icon: "cp_info", desc: "Hardware and system details", render: (p) => cpInfo(p) },
  notify: { label: "Notifications", group: "System", icon: "cp_notify", desc: "Email alerts", render: (p) => cpNotify(p) },
  ups: { label: "UPS", group: "System", icon: "cp_ups", desc: "Power failure protection", render: (p) => cpUps(p) },
  usb: { label: "External Devices", group: "System", icon: "cp_usb", desc: "USB drives", render: (p) => cpUsb(p) },
  tasks: { label: "Task Scheduler", group: "System", icon: "cp_tasks", desc: "Scripts, drive tests and clean-ups on a schedule", render: (p) => cpTasks(p) },
  update: { label: "Update", group: "System", icon: "cp_update", desc: "Cynthology and Debian updates", render: (p) => cpUpdate(p) },
};

WM.register("control", {
  name: "Control Panel", icon: "⚙️", color: "#4f6d8a", w: 960, h: 620,
  open(body) {
    const home = () => {
      const search = h("input", { class: "cp-search", placeholder: "Search settings", oninput: () => draw() });
      const grid = h("div");
      const draw = () => {
        const q = search.value.toLowerCase();
        const groups = {};
        for (const [k, s] of Object.entries(CP_SECTIONS))
          if (!q || (s.label + " " + t(s.label) + " " + s.desc).toLowerCase().includes(q)) (groups[s.group] ||= []).push([k, s]);
        grid.replaceChildren(...Object.entries(groups).map(([g, items]) => h("div", { class: "cp-group" },
          h("div", { class: "cp-group-title" }, t(g)),
          h("div", { class: "cp-grid" }, items.map(([k, s]) => h("button", { class: "cp-item", "data-k": k, title: s.desc, onclick: () => open(k) },
            appTile(s.icon, 44), h("span", {}, t(s.label))))))));
        if (!Object.keys(groups).length) grid.append(h("p", { class: "muted", style: "text-align:center" }, "Nothing matches."));
      };
      body.replaceChildren(h("div", { class: "pane cp-home" }, h("div", { style: "text-align:center" }, search), grid));
      draw();
      search.focus();
    };
    const open = (key) => {
      body.replaceChildren();
      const sections = Object.fromEntries(Object.entries(CP_SECTIONS).map(([k, s]) => [k, { label: s.label, render: s.render }]));
      const show = sectioned(sections, body);
      const side = body.querySelector(".side");
      side.prepend(h("button", { class: "cp-back", onclick: home }, "‹ All settings"));
      side.querySelectorAll("button[data-k]").forEach((b) => b.prepend(svgIcon(CP_SECTIONS[b.dataset.k].icon, 16)));
      show(key);
    };
    home();
  },
});

async function cpShares(p) {
  const [shares, pools, usage] = await Promise.all([API.get("/api/shares"), API.get("/api/storage/pools"), API.get("/api/shares/usage/all").catch(() => ({}))]);
  const reload = () => { p.replaceChildren(); cpShares(p); };
  const sizeCell = (s) => {
    const u = usage[s.name];
    if (!u && !s.quota_gb) return h("span", { class: "muted" }, "-");
    const used = u ? fmtBytes(u.used) : "?";
    const lim = s.quota_gb ? `${s.quota_gb} GB` : "no limit";
    const pct = u && s.quota_gb ? Math.min(100, Math.round(u.used / (s.quota_gb * 1e9) * 100)) : null;
    return h("div", {}, h("div", { style: "font-size:13px" }, `${used} / ${lim}`),
      pct != null ? h("div", { class: "bar", style: "margin-top:4px;width:120px" }, h("div", { style: `width:${pct}%;${pct > 90 ? "background:var(--bad)" : ""}` })) : null);
  };
  p.append(h("h2", {}, "Shared Folders"),
    h("div", { class: "toolbar" }, h("button", { class: "btn primary", disabled: !pools.length, onclick: () => shareForm(p, pools, reload) }, "+ Create")),
    !pools.length ? h("div", { class: "note warn" }, "Create a storage pool in Storage Manager first.") : null,
    h("table", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Location"), h("th", {}, "Used / limit"), h("th", {}, "Access"), h("th", {})),
      shares.map((s) => {
        const locked = s.encrypted && s.locked;
        return h("tr", {},
          h("td", {}, h("b", { translate: "no" }, s.name), s.encrypted ? h("span", { class: "pill " + (locked ? "warn" : "ok"), style: "margin-left:6px" }, locked ? "🔒 locked" : "🔓 encrypted") : null,
            s.homes ? h("span", { class: "pill info", style: "margin-left:6px" }, "user homes") : null,
            h("div", { class: "muted" }, s.description)),
          h("td", { class: "mono", style: "font-size:12px" }, s.path),
          h("td", {}, sizeCell(s)),
          h("td", {}, s.homes ? h("span", { class: "pill" }, "SMB: home") : s.smb ? h("span", { class: "pill" }, "SMB") : null, " ", s.nfs ? h("span", { class: "pill" }, "NFS") : null,
            s.recycle_bin ? h("span", { class: "pill" }, "Recycle bin") : null),
          h("td", { style: "white-space:nowrap" },
            s.encrypted ? (locked
              ? h("button", { class: "btn primary", onclick: () => unlockDialog(s, reload) }, "Unlock")
              : h("button", { class: "btn", onclick: async () => {
                  if (!confirm(`Lock ${s.name}? It disappears from the network until it is unlocked again.` + (s.auto_unlock ? "\n\nThe key stored on the NAS is removed too (no automatic unlock at boot)." : ""))) return;
                  await act(() => API.post(`/api/shares/${s.name}/lock`), "Locked"); reload();
                } }, "Lock")) : null, " ",
            s.homes || s.usb ? null : h("button", { class: "btn", onclick: () => permsDialog(s, reload) }, "Permissions"), " ",
            s.usb ? h("span", { class: "pill" }, "USB") : h("button", { class: "btn", onclick: async () => {
              const v = prompt(`Size limit for ${s.name} in GB (0 = no limit):`, String(s.quota_gb || 0)); if (v === null) return;
              await act(() => API.put(`/api/shares/${s.name}/quota`, { quota_gb: +v || 0 }), "Size limit saved"); reload();
            } }, "Limit"), " ",
            !s.usb && !s.encrypted ? h("button", { class: "btn", onclick: () => act(() => API.post(`/api/snapshots/${s.name}`), "Snapshot taken") }, "Snapshot") : null, " ",
            s.homes ? null : h("button", { class: "btn danger", onclick: async () => {
              if (!confirm(`Remove share ${s.name}? (data is kept)`)) return;
              await act(() => API.del(`/api/shares/${s.name}`), "Share removed"); reload();
            } }, "Remove")));
      })));
}

function unlockDialog(s, done) {
  const v = { secret: "", remember: !!s.auto_unlock };
  const err = h("div", { class: "err" });
  const close = modal(`Unlock ${s.name}`, h("div", { class: "form", style: "padding:0" },
    h("label", {}, "Password (or recovery key)", h("input", { type: "password", autocomplete: "off", oninput: (e) => (v.secret = e.target.value),
      onkeydown: (e) => e.key === "Enter" && go() })),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: v.remember, onchange: (e) => (v.remember = e.target.checked) }),
      "Unlock automatically when the NAS starts (stores the key on the NAS - convenient, but anyone who takes the whole NAS can read the folder)"),
    err), [["Cancel", () => close()], ["Unlock", () => go(), "primary"]]);
  async function go() {
    err.textContent = "";
    try { await API.post(`/api/shares/${s.name}/unlock`, v); close(); toast(`${s.name} unlocked`); done(); }
    catch (e) { err.textContent = e.message; }
  }
}

function recoveryKeyDialog(name, key) {
  const text = `Cynthology encrypted shared folder "${name}" on ${location.hostname}\n\nRecovery key:\n${key}\n\nUse it instead of the password (Unlock → Password or recovery key) if the password is lost.\n`;
  modal(`Recovery key for ${name}`, h("div", { class: "form", style: "padding:0" },
    h("div", { class: "note warn" }, "Save this key somewhere safe. Without the password or this key the data can never be read - not even by an administrator."),
    h("pre", { class: "mono", style: "white-space:pre-wrap;background:var(--panel-2);padding:12px;border-radius:8px;margin:0;font-size:14px", translate: "no" }, key),
    h("div", { class: "toolbar" },
      h("button", { class: "btn", onclick: () => navigator.clipboard?.writeText(key).then(() => toast("Copied")) }, "Copy"),
      h("button", { class: "btn", onclick: () => {
        const a = h("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: `${name}-recovery-key.txt` });
        document.body.append(a); a.click(); a.remove();
      } }, "Download"))), [["I saved it", null, "primary"]]);
}

function shareForm(p, pools, done) {
  const f = { name: "", description: "", pool_id: pools[0].id, smb: true, nfs: false, recycle_bin: true, quota_gb: 0, encrypted: false, password: "", auto_unlock: false };
  const pw2 = { v: "" };
  const enc = h("div", { class: "form", style: "padding:0" });
  const drawEnc = () => enc.replaceChildren(
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.encrypted, onchange: (e) => { f.encrypted = e.target.checked; drawEnc(); } }), "Encrypt this shared folder"),
    f.encrypted ? [
      h("div", { class: "muted", style: "font-size:12px" }, "Files are stored encrypted. The folder is only readable while unlocked; snapshots and Previous Versions aren't available for encrypted folders. You'll get a recovery key."),
      h("label", {}, "Encryption password (8+ characters)", h("input", { type: "password", autocomplete: "new-password", oninput: (e) => (f.password = e.target.value) })),
      h("label", {}, "Repeat password", h("input", { type: "password", autocomplete: "new-password", oninput: (e) => (pw2.v = e.target.value) })),
      h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => (f.auto_unlock = e.target.checked) }), "Unlock automatically when the NAS starts (key stored on the NAS)")] : null);
  drawEnc();
  p.replaceChildren(h("h2", {}, "Create shared folder"), h("div", { class: "form" },
    h("label", {}, "Name", h("input", { oninput: (e) => (f.name = e.target.value) })),
    h("label", {}, "Description", h("input", { oninput: (e) => (f.description = e.target.value) })),
    h("label", {}, "Location", h("select", { onchange: (e) => (f.pool_id = +e.target.value) },
      pools.map((x) => h("option", { value: x.id }, `${x.label} (${x.mount})`)))),
    h("label", {}, "Size limit in GB (0 = no limit)", h("input", { type: "number", min: 0, value: 0, style: "width:140px", oninput: (e) => (f.quota_gb = +e.target.value || 0) })),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: true, onchange: (e) => (f.smb = e.target.checked) }), "Share over SMB (Windows / macOS)"),
    h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => (f.nfs = e.target.checked) }), "Share over NFS (Linux)"),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: true, onchange: (e) => (f.recycle_bin = e.target.checked) }), "Enable recycle bin"),
    enc,
    h("div", { class: "toolbar" },
      h("button", { class: "btn", onclick: done }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => {
        if (f.encrypted && f.password !== pw2.v) return toast("The passwords don't match");
        const r = await act(() => API.post("/api/shares", f), "Shared folder created");
        if (r?.recovery_key) recoveryKeyDialog(r.name, r.recovery_key);
        done();
      } }, "Create"))));
}

async function cpUsers(p) {
  const [users, tfa] = await Promise.all([API.get("/api/users"), API.get("/api/2fa/users")]);
  const reload = () => { p.replaceChildren(); cpUsers(p); };
  const nu = { name: "", full_name: "", password: "", admin: false };
  p.append(h("h2", {}, "Users & Groups"),
    h("table", {}, h("tr", {}, h("th", {}, "User"), h("th", {}, "Full name"), h("th", {}, "Groups"), h("th", {}, "2-step"), h("th", {})),
      users.map((u) => h("tr", {}, h("td", {}, h("b", {}, u.name)), h("td", {}, u.full_name), h("td", {}, u.groups.join(", ")),
        h("td", {}, tfa[u.name] ? h("span", { class: "pill ok" }, "On") : h("span", { class: "muted" }, "Off")),
        h("td", {}, tfa[u.name] ? h("button", { class: "btn", title: "For a lost phone: the user signs in with just the password and can set it up again", onclick: async () => {
          if (!confirm(`Reset 2-step verification for ${u.name}? They can then sign in with only their password.`)) return;
          await act(() => API.del(`/api/2fa/users/${u.name}`), "2-step verification reset"); reload();
        } }, "Reset 2-step") : null, " ", h("button", { class: "btn danger", onclick: async () => {
          if (!confirm(`Delete user ${u.name}?`)) return;
          await act(() => API.del(`/api/users/${u.name}`), "User deleted"); reload();
        } }, "Delete"))))),
    h("h2", { style: "margin-top:24px" }, "User home"),
    homesBox(reload),
    h("h2", { style: "margin-top:24px" }, "Add user"),
    h("div", { class: "form" },
      h("label", {}, "Username", h("input", { oninput: (e) => (nu.name = e.target.value) })),
      h("label", {}, "Full name", h("input", { oninput: (e) => (nu.full_name = e.target.value) })),
      h("label", {}, "Password (8+ characters)", h("input", { type: "password", oninput: (e) => (nu.password = e.target.value) })),
      h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => (nu.admin = e.target.checked) }), "Administrator"),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => { await act(() => API.post("/api/users", nu), "User created"); reload(); } }, "Create user"))));
}

async function cpServices(p) {
  const [svcs, tm, shares] = await Promise.all([API.get("/api/services"), API.get("/api/shares/timemachine/status"), API.get("/api/shares")]);
  const f = { enabled: tm.enabled, share: tm.share || "TimeMachine", max_gb: tm.max_gb || 0 };
  const names = shares.filter((s) => !s.usb).map((s) => s.name);
  const tmCard = h("div", { class: "card", style: "max-width:640px;margin-bottom:18px" },
    h("div", {}, h("b", {}, "Time Machine (Mac backups)"), " ", h("span", { class: "pill " + (tm.enabled ? "ok" : "") }, tm.enabled ? `on - ${tm.share}` : "off")),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.enabled, onchange: (e) => (f.enabled = e.target.checked) }),
      "Let Macs back up to this NAS with Time Machine"),
    h("label", {}, "Shared folder", h("input", { list: "tm-shares", value: f.share, oninput: (e) => (f.share = e.target.value.trim()) }),
      h("datalist", { id: "tm-shares" }, names.map((n) => h("option", { value: n })))),
    h("label", {}, "Size limit in GB (0 = no limit; with several Macs the limit is shared)",
      h("input", { type: "number", min: 0, value: f.max_gb, style: "width:140px", oninput: (e) => (f.max_gb = +e.target.value) })),
    h("div", { class: "muted", style: "font-size:12px" }, "On the Mac: System Settings → General → Time Machine → Add Backup Disk, then pick this NAS. ",
      "Sign in with your Cynthology user. A new folder name creates that shared folder."),
    h("div", {}, h("button", { class: "btn primary", onclick: async () => {
      await act(() => API.put("/api/shares/timemachine/config", f), f.enabled ? "Time Machine is on" : "Time Machine is off");
      p.replaceChildren(); cpServices(p);
    } }, "Apply")));
  p.append(h("h2", {}, "File Services"), tmCard, h("table", {},
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
  const [s, tfa] = await Promise.all([API.get("/api/security"), API.get("/api/2fa/policy")]);
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
    h("h3", { class: "pc-group", style: "margin-top:20px" }, "2-step verification"),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: tfa.enforce_admins, onchange: async (e) => {
      try { await act(() => API.put("/api/2fa/policy", { enforce_admins: e.target.checked }), e.target.checked ? "Administrators now need 2-step verification" : "Policy off"); }
      catch { e.target.checked = !e.target.checked; }
    } }), "Require 2-step verification for all administrators"),
    h("div", { class: "muted" }, "Each user turns it on under their name (top right) → Personal settings. ",
      "Failed sign-ins are limited: 5 wrong attempts in 15 minutes block that user and address for 15 minutes."),
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
    h("h3", { class: "pc-group", style: "margin-top:20px" }, "Free certificate from Let's Encrypt"),
    leBox(reload),
    h("h3", { class: "pc-group", style: "margin-top:20px" }, "Use your own certificate"),
    h("div", { class: "form", style: "max-width:720px" },
      h("div", { class: "muted" }, "Paste PEM text, e.g. from Let's Encrypt (fullchain.pem + privkey.pem)."),
      h("label", {}, "Certificate (+ intermediates)", certPem), h("label", {}, "Private key", keyPem),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => {
        await act(() => API.post("/api/security/cert/upload", { cert: certPem.value, key: keyPem.value }), "Certificate installed - reconnecting…");
        await waitForServer(); reload();
      } }, "Install certificate"))));
}

async function cpNotify(p) {
  const [cfg, alerts] = await Promise.all([API.get("/api/health/smtp"), API.get("/api/health/alerts")]);
  const f = { ...cfg, password: null };
  const inp = (label, key, attrs = {}) => h("label", {}, label, h("input", { value: f[key] ?? "", oninput: (e) => (f[key] = attrs.type === "number" ? +e.target.value : e.target.value), ...attrs }));
  const save = () => API.put("/api/health/smtp", f);
  p.append(h("h2", {}, "Notifications"),
    h("div", { class: "note" }, "Get an email when a drive fails, a pool is degraded or a volume fills up. ",
      "For Gmail or Outlook use an ", h("b", {}, "app password"), " (smtp.gmail.com / smtp.office365.com, port 587, STARTTLS)."),
    h("div", { class: "form", style: "margin-top:12px" },
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.enabled, onchange: (e) => (f.enabled = e.target.checked) }), "Send email notifications"),
      inp("Send to (comma-separated)", "to", { placeholder: "you@example.com" }),
      inp("SMTP server", "host", { placeholder: "smtp.gmail.com" }),
      h("div", { style: "display:flex;gap:10px" },
        inp("Port", "port", { type: "number", style: "width:100px" }),
        h("label", {}, "Security", h("select", { onchange: (e) => (f.security = e.target.value) },
          [["starttls", "STARTTLS"], ["ssl", "SSL/TLS"], ["none", "None"]].map(([v, l]) => h("option", { value: v, selected: f.security === v }, l))))),
      inp("Username", "username", { autocomplete: "off" }),
      h("label", {}, "Password", h("input", { type: "password", autocomplete: "new-password", placeholder: cfg.has_password ? "(unchanged)" : "", oninput: (e) => (f.password = e.target.value) })),
      inp("From address (optional)", "sender"),
      h("div", { class: "toolbar" },
        h("button", { class: "btn primary", onclick: () => act(save, "Saved") }, "Save"),
        h("button", { class: "btn", onclick: async () => { await save(); await act(() => API.post("/api/health/smtp/test"), "Test email sent"); } }, "Save & send test email"))),
    h("h3", { class: "pc-group", style: "margin-top:22px" }, "Recent alerts"),
    alerts.length ? h("table", {}, alerts.slice(0, 30).map((a) => h("tr", {},
      h("td", { class: "muted", style: "white-space:nowrap" }, new Date(a.time * 1000).toLocaleString()),
      h("td", {}, h("span", { class: "pill " + (a.level === "critical" ? "bad" : a.level === "warning" ? "warn" : "ok") }, a.event === "resolved" ? "resolved" : a.level)),
      h("td", {}, h("b", {}, a.title), h("div", { class: "muted" }, a.detail || "")))))
      : h("p", { class: "muted" }, "No alerts so far."));
}

async function cpUps(p) {
  const { config: cfg, status: st } = await API.get("/api/ups");
  const f = { ...cfg };
  const reload = () => { p.replaceChildren(); cpUps(p); };
  const box = h("div", { class: "form" });
  const mins = (s) => (s == null ? "?" : `${Math.round(s / 60)} min`);

  const statusCard = !st.enabled ? null : st.connected
    ? h("div", { class: "card", style: "max-width:560px;margin-bottom:16px" },
        h("div", {}, h("b", {}, st.model || "UPS"), " ", h("span", { class: "pill " + (st.on_battery || st.low_battery ? "bad" : st.replace_battery ? "warn" : "ok") }, st.status)),
        st.charge != null ? h("div", {}, h("div", { class: "bar" }, h("div", { style: `width:${st.charge}%;background:${st.charge < 30 ? "var(--bad)" : "var(--ok)"}` })),
          h("div", { class: "muted" }, `Battery ${st.charge}% · runtime ${mins(st.runtime)}` + (st.load != null ? ` · load ${st.load}%` : ""))) : null,
        st.input_voltage != null ? h("div", { class: "muted" }, `Input ${st.input_voltage} V` + (st.output_voltage != null ? ` · output ${st.output_voltage} V` : "")) : null,
        h("div", { class: "muted mono" }, st.target + (st.serial ? ` · serial ${st.serial}` : "")))
    : h("div", { class: "note warn", style: "max-width:560px;margin-bottom:16px" }, "Can't reach the UPS: " + [].concat(st.error).join(" "));

  async function draw() {
    const parts = [
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.enabled, onchange: (e) => { f.enabled = e.target.checked; draw(); } }), "Enable UPS support"),
    ];
    if (f.enabled) {
      parts.push(h("label", {}, "UPS type", h("select", { onchange: (e) => { f.mode = e.target.value; draw(); } },
        [["usb", "USB UPS (connected to this NAS)"], ["snmp", "SNMP UPS (network card)"], ["network", "UPS server on the network (another Cynthology or Synology NAS)"]]
          .map(([v, l]) => h("option", { value: v, selected: f.mode === v }, l)))));
      if (f.mode === "usb") {
        const found = h("div", { class: "muted" });
        parts.push(h("div", {}, h("button", { class: "btn", onclick: async () => {
          found.textContent = "Looking for USB UPS devices…";
          const r = await API.get("/api/ups/detect");
          found.textContent = r.length ? "Found: " + r.map((u) => `${u.vendor || ""} ${u.product || ""} (${u.driver})`.trim()).join("; ") : "No USB UPS found. Check the cable (a USB data cable, not just power).";
        } }, "Detect USB UPS"), found));
      } else if (f.mode === "snmp") {
        parts.push(h("label", {}, "UPS IP address", h("input", { value: f.snmp_host, oninput: (e) => (f.snmp_host = e.target.value.trim()) })),
          h("label", {}, "SNMP community", h("input", { value: f.snmp_community, oninput: (e) => (f.snmp_community = e.target.value.trim()) })));
      } else {
        parts.push(h("label", {}, "UPS server IP address", h("input", { value: f.server_host, placeholder: "192.168.1.10", oninput: (e) => (f.server_host = e.target.value.trim()) })),
          h("details", {}, h("summary", { class: "muted" }, "Advanced (UPS name, login)"),
            h("div", { class: "form", style: "margin-top:8px" },
              h("label", {}, "UPS name", h("input", { value: f.server_ups, oninput: (e) => (f.server_ups = e.target.value.trim()) })),
              h("label", {}, "User", h("input", { value: f.server_user, oninput: (e) => (f.server_user = e.target.value.trim()) })),
              h("label", {}, "Password", h("input", { value: f.server_password, oninput: (e) => (f.server_password = e.target.value.trim()) })),
              h("div", { class: "muted" }, "Synology NAS servers use ups / monuser / secret (the defaults)."))));
      }
      parts.push(h("h3", { class: "pc-group" }, "When the power fails"),
        h("label", { class: "check" }, h("input", { type: "radio", name: "ups-sd", checked: f.shutdown === "timer", onchange: () => { f.shutdown = "timer"; draw(); } }),
          "Shut down safely after ", h("input", { type: "number", min: 1, max: 240, value: f.shutdown_minutes, style: "width:70px", disabled: f.shutdown !== "timer",
            oninput: (e) => (f.shutdown_minutes = +e.target.value) }), " minutes on battery"),
        h("label", { class: "check" }, h("input", { type: "radio", name: "ups-sd", checked: f.shutdown === "lowbatt", onchange: () => { f.shutdown = "lowbatt"; draw(); } }),
          "Run on battery until the UPS reports low battery, then shut down"));
      if (f.mode !== "network") {
        parts.push(h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.ups_poweroff, onchange: (e) => (f.ups_poweroff = e.target.checked) }),
            "Turn the UPS off after the NAS has shut down (so everything restarts cleanly when power returns)"),
          h("h3", { class: "pc-group" }, "UPS server"),
          h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.serve, onchange: (e) => (f.serve = e.target.checked) }),
            "Let other devices on the network use this UPS (e.g. another NAS or a Synology - they connect to this NAS's IP)"));
      }
    }
    parts.push(h("div", { class: "toolbar" },
      h("button", { class: "btn primary", onclick: async () => { await act(() => API.put("/api/ups", f), "UPS settings applied"); reload(); } }, "Apply"),
      st.enabled ? h("button", { class: "btn", onclick: reload }, "Refresh status") : null));
    box.replaceChildren(...parts);
  }
  p.append(h("h2", {}, "UPS"), statusCard, box);
  draw();
}

async function cpUsb(p) {
  const disks = await API.get("/api/usb");
  const reload = () => { p.replaceChildren(); cpUsb(p); };
  const fsName = { vfat: "FAT32", exfat: "exFAT", ntfs: "NTFS", ntfs3: "NTFS", ext4: "ext4", btrfs: "Btrfs", xfs: "XFS", hfsplus: "HFS+ (read-only)" };
  p.append(h("h2", {}, "External Devices"),
    h("p", { class: "muted" }, "USB drives are mounted automatically when plugged in and appear as shared folders usbshare1, usbshare2… in File Station and on the network."),
    disks.length ? h("div", { class: "cards" }, disks.map((d) => h("div", { class: "card" },
      h("div", {}, h("b", {}, d.model || d.name), " ", h("span", { class: "muted" }, `${fmtBytes(d.size)} · /dev/${d.name}`)),
      d.volumes.length ? d.volumes.map((v) => h("div", { style: "border-top:1px solid var(--line);padding-top:8px" },
          h("div", {}, h("b", {}, v.label || "(no label)"), " ", h("span", { class: "pill" }, fsName[v.fstype] || v.fstype), " ", h("span", { class: "muted" }, fmtBytes(v.size))),
          v.share ? h("div", {}, "Shared folder ", h("b", {}, v.share), h("span", { class: "muted mono" }, "  " + v.mountpoint))
            : v.supported ? h("div", { class: "muted" }, "Not mounted") : h("div", { class: "err" }, `Unsupported filesystem (${v.fstype})`)))
        : h("div", { class: "muted" }, "No filesystem - format it to use it."),
      h("div", { class: "toolbar", style: "margin:4px 0 0" },
        d.volumes.some((v) => v.share)
          ? h("button", { class: "btn primary", onclick: async () => { await act(() => API.post(`/api/usb/${d.name}/eject`), "Ejected - you can unplug the drive now"); reload(); } }, "⏏ Eject")
          : d.volumes.some((v) => v.supported) ? h("button", { class: "btn", onclick: async () => { await act(() => API.post(`/api/usb/${d.name}/mount`), "Mounted"); reload(); } }, "Mount") : null,
        h("button", { class: "btn danger", onclick: () => usbFormat(p, d, reload) }, "Format…")))))
      : h("p", { class: "muted" }, "No external drives connected."),
    h("div", { class: "toolbar", style: "margin-top:12px" }, h("button", { class: "btn", onclick: reload }, "⟳ Refresh")));
}

function usbFormat(p, d, done) {
  const f = { fs: "exfat", label: "USB" };
  p.replaceChildren(h("h2", {}, `Format ${d.model || d.name}`),
    h("div", { class: "form" },
      h("div", { class: "note warn" }, `Everything on this ${fmtBytes(d.size)} drive will be erased.`),
      h("label", {}, "Format as", h("select", { onchange: (e) => (f.fs = e.target.value) },
        [["exfat", "exFAT - works on Windows, macOS and Linux (recommended)"], ["ntfs", "NTFS - Windows"], ["ext4", "ext4 - Linux only, fastest on the NAS"]].map(([v, l]) => h("option", { value: v }, l)))),
      h("label", {}, "Label (max 11 characters)", h("input", { value: f.label, maxlength: 11, oninput: (e) => (f.label = e.target.value) })),
      h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: done }, "Cancel"),
        h("button", { class: "btn danger", onclick: async () => {
          if (!confirm(`Erase ${d.model || d.name} and format it as ${f.fs}?`)) return;
          p.replaceChildren(h("p", { class: "muted" }, "Formatting…"));
          try { await act(() => API.post(`/api/usb/${d.name}/format`, f), "Formatted and mounted"); } finally { done(); }
        } }, "Erase and format"))));
}

async function cpNetwork(p) {
  const data = await API.get("/api/network");
  const st = data.status;
  const cfg = JSON.parse(JSON.stringify(data.config));
  const reload = () => { p.replaceChildren(); cpNetwork(p); };
  const box = h("div");
  const speed = (s) => (s ? (s >= 1000 ? `${s / 1000} Gbps` : `${s} Mbps`) : "");

  const ipEditor = (ip, label) => h("div", { class: "form", style: "gap:8px" },
    h("label", {}, label, h("select", { onchange: (e) => { ip.mode = e.target.value; draw(); } },
      [["dhcp", "Get an address automatically (DHCP)"], ["static", "Use a fixed address"], ["off", "No IPv4 address"]].map(([v, l]) => h("option", { value: v, selected: ip.mode === v }, l)))),
    ip.mode === "static" ? h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" },
      h("label", {}, "Address / subnet", h("input", { value: ip.address, placeholder: "192.168.1.50/24", oninput: (e) => (ip.address = e.target.value.trim()) })),
      h("label", {}, "Gateway (router)", h("input", { value: ip.gateway, placeholder: "192.168.1.1", oninput: (e) => (ip.gateway = e.target.value.trim()) }))) : null);
  const mtuSel = (o) => h("label", {}, "MTU", h("select", { onchange: (e) => (o.mtu = +e.target.value) },
    [[1500, "1500 (standard)"], [9000, "9000 (jumbo frames - every device on the path must support it)"]].concat(o.mtu !== 1500 && o.mtu !== 9000 ? [[o.mtu, String(o.mtu)]] : [])
      .map(([v, l]) => h("option", { value: v, selected: o.mtu === v }, l))));

  function draw() {
    const inBond = new Set(cfg.bonds.flatMap((b) => b.members));
    box.replaceChildren(
      ...cfg.bonds.map((b, bi) => h("div", { class: "card", style: "margin-bottom:12px" },
        h("div", {}, h("b", {}, `${b.name} - link aggregation`), " ", h("span", { class: "muted" }, b.members.join(" + ")),
          h("button", { class: "btn danger", style: "float:right", onclick: () => { cfg.bonds.splice(bi, 1); draw(); } }, "Remove bond")),
        h("label", {}, "Mode", h("select", { onchange: (e) => (b.mode = e.target.value) },
          Object.entries(data.bond_modes).map(([v, l]) => h("option", { value: v, selected: b.mode === v }, l)))),
        ipEditor(b.ipv4, "IPv4"), mtuSel(b))),
      ...cfg.nics.filter((n) => !inBond.has(n.name)).map((n) => {
        const live = st.interfaces.find((i) => i.name === n.name) || {};
        return h("div", { class: "card", style: "margin-bottom:12px" },
          h("div", {}, h("b", {}, n.name), " ",
            h("span", { class: "pill " + (live.carrier ? "ok" : "") }, live.carrier ? `connected ${speed(live.speed)}` : "no cable"),
            h("span", { class: "muted mono", style: "margin-left:8px;font-size:12px" }, live.mac || ""),
            h("span", { class: "muted", style: "margin-left:8px" }, (live.ipv4 || []).join(", "))),
          h("label", { class: "check" }, h("input", { type: "checkbox", checked: n.enabled, onchange: (e) => { n.enabled = e.target.checked; draw(); } }), "Enabled"),
          n.enabled ? [ipEditor(n.ipv4, "IPv4"), mtuSel(n)] : null);
      }),
      cfg.nics.length - inBond.size >= 2 ? h("button", { class: "btn", onclick: () => {
        const free = cfg.nics.filter((n) => !inBond.has(n.name)).map((n) => n.name);
        const pick = prompt(`Ports to combine (comma-separated, at least two):\n${free.join(", ")}`, free.slice(0, 2).join(", "));
        if (!pick) return;
        const members = pick.split(",").map((x) => x.trim()).filter(Boolean);
        const first = cfg.nics.find((n) => n.name === members[0]);
        cfg.bonds.push({ name: `bond${cfg.bonds.length}`, mode: "balance-alb", members, mtu: 1500, ipv4: first ? { ...first.ipv4 } : { mode: "dhcp", address: "", gateway: "" } });
        draw();
      } }, "+ Create link aggregation (bond)") : null,
      h("h3", { class: "pc-group", style: "margin-top:16px" }, "VLANs"),
      h("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Tagged 802.1Q networks on a port or bond (the switch port must carry these VLANs). The port itself can stay on the untagged network, or be set to “No IPv4 address”."),
      ...(cfg.vlans || []).map((v, vi) => {
        const live = st.interfaces.find((i) => i.name === `${v.parent}.${v.id}`) || {};
        return h("div", { class: "card", style: "margin-bottom:12px" },
          h("div", {}, h("b", {}, `VLAN ${v.id}`), h("span", { class: "muted" }, ` on ${v.parent}`),
            live.ipv4?.length ? h("span", { class: "muted", style: "margin-left:8px" }, live.ipv4.join(", ")) : null,
            h("button", { class: "btn danger", style: "float:right", onclick: () => { cfg.vlans.splice(vi, 1); draw(); } }, "Remove")),
          ipEditor(v.ipv4, "IPv4"), mtuSel(v));
      }),
      h("button", { class: "btn", onclick: () => {
        const parents = [...cfg.nics.filter((n) => n.enabled && !inBond.has(n.name)).map((n) => n.name), ...cfg.bonds.map((b) => b.name)];
        const id = parseInt(prompt("VLAN ID (1-4094)", "10"), 10);
        if (!id) return;
        const parent = parents.length === 1 ? parents[0] : prompt(`On which port or bond?\n${parents.join(", ")}`, parents[0]);
        if (!parent) return;
        (cfg.vlans ||= []).push({ parent: parent.trim(), id, mtu: 1500, ipv4: { mode: "dhcp", address: "", gateway: "" } });
        draw();
      } }, "+ Add VLAN"),
      h("h3", { class: "pc-group", style: "margin-top:16px" }, "DNS servers"),
      h("input", { value: cfg.dns.join(", "), placeholder: "empty = from DHCP, e.g. 1.1.1.1, 9.9.9.9", style: "width:100%;max-width:520px",
        oninput: (e) => (cfg.dns = e.target.value.split(/[ ,]+/).filter(Boolean)) }),
      h("div", { class: "toolbar", style: "margin-top:16px" }, h("button", { class: "btn primary", onclick: applyNet }, "Apply"),
        h("button", { class: "btn", onclick: reload }, "Cancel")));
  }

  async function applyNet() {
    const statics = [...cfg.nics.filter((n) => n.enabled), ...cfg.bonds, ...(cfg.vlans || [])].filter((x) => x.ipv4.mode === "static").map((x) => x.ipv4.address.split("/")[0]);
    const host = location.hostname;
    const hostIsIp = /^\d+\.\d+\.\d+\.\d+$/.test(host);
    const currentIps = st.interfaces.flatMap((i) => i.ipv4.map((a) => a.split("/")[0]));
    // where will the browser find the NAS afterwards?
    let target = host;
    if (hostIsIp && currentIps.includes(host) && !statics.includes(host)) {
      const stays = cfg.nics.some((n) => n.enabled && n.ipv4.mode === "dhcp" && (st.interfaces.find((i) => i.name === n.name)?.ipv4 || []).some((a) => a.startsWith(host + "/")));
      if (!stays) target = statics[0] || host;
    }
    const msg = target !== host
      ? `The NAS will move to ${target}. Your browser follows automatically.`
      : "The network restarts briefly.";
    if (!confirm(`${msg}\n\nIf the NAS can't be reached from this browser within ${data.rollback_seconds} seconds, the previous settings are restored automatically.\n\nApply?`)) return;
    try { await API.put("/api/network", cfg); } catch (e) { return toast("Error: " + e.message); }
    const overlay = h("div", { class: "wiz" }, h("div", { class: "wiz-card" }, h("h2", {}, "Applying network settings…"),
      h("p", { class: "muted" }, target !== host ? `Moving to ${target}…` : "Waiting for the NAS to come back…")));
    document.body.append(overlay);
    await new Promise((r) => setTimeout(r, 8000));
    if (target !== host) {
      location.href = `${location.protocol}//${target}:${location.port || (location.protocol === "https:" ? 443 : 80)}/#token=${encodeURIComponent(API.token)}&net-confirm=1`;
      return;
    }
    for (let i = 0; i < 30; i++) {
      try { await API.post("/api/network/confirm"); overlay.remove(); toast("Network settings saved"); return reload(); }
      catch (e) { if (/nothing to confirm/.test(e.message)) { overlay.remove(); toast("The change was rolled back"); return reload(); } }
      await new Promise((r) => setTimeout(r, 2000));
    }
    overlay.remove(); toast("Could not reach the NAS - it will restore the previous settings by itself"); 
  }

  p.append(h("h2", {}, "Network"),
    st.manager ? h("div", { class: "note warn" }, `Networking on this system is managed by ${st.manager}. Change it with ${st.manager}'s tools; the settings below are read-only.`) : null,
    st.pending ? h("div", { class: "note warn" }, "A network change is waiting for confirmation. ",
      h("button", { class: "btn primary", onclick: async () => { await act(() => API.post("/api/network/confirm"), "Confirmed"); reload(); } }, "Keep these settings")) : null,
    h("div", { class: "muted", style: "margin-bottom:12px" }, `Default gateway ${st.gateway || "-"} · DNS ${st.dns.join(", ") || "-"}`),
    box);
  draw();
}

async function cpUpdate(p) {
  const s = await API.get("/api/update?refresh=1");
  const logBox = h("pre", { class: "mono update-log hidden" });
  const reload = () => { p.replaceChildren(); cpUpdate(p); };
  const follow = async (logUrl, done) => {
    logBox.classList.remove("hidden");
    for (;;) {
      let r = null;
      try { r = await API.get(logUrl); }
      catch (e) {
        // the web UI restarts during an update; once it answers but has no log page, the new version is running
        if (/Not Found/.test(e.message)) return done({ current: "the new version" });
      }
      if (r) {
        logBox.textContent = r.log || "Starting…";
        logBox.scrollTop = logBox.scrollHeight;
        if (!r.running) return done(r);
      } else logBox.textContent += "\n… restarting the web interface …";
      await new Promise((res) => setTimeout(res, 2500));
    }
  };
  const lat = s.latest;
  p.append(h("h2", {}, "Update"),
    h("div", { class: "card", style: "max-width:720px" },
      h("div", { style: "display:flex;align-items:center;gap:14px" }, appTile("cp_update", 44),
        h("div", { style: "flex:1" }, h("div", { style: "font-size:16px;font-weight:600" }, `Cynthology ${s.current}`),
          s.error ? h("div", { class: "err" }, s.error)
            : s.update_available ? h("div", {}, h("span", { class: "pill info" }, `Version ${lat.version} is available`))
            : h("div", { class: "muted" }, "You are up to date."))),
      s.update_available && lat ? h("details", { open: true }, h("summary", { class: "muted" }, `What's new in ${lat.version}`),
        h("div", { class: "release-notes" }, lat.notes || "(no release notes)")) : null,
      h("div", { class: "toolbar", style: "margin:4px 0 0" },
        s.update_available ? h("button", { class: "btn primary", disabled: s.running, onclick: async () => {
          if (!confirm(`Update Cynthology to ${lat.version}? Settings and data are kept. The web interface restarts for a moment.`)) return;
          await act(() => API.post("/api/update/start"), "Update started");
          follow("/api/update/log", (r) => {
            toast(`Cynthology ${r.current} installed - reloading`);
            setTimeout(() => location.reload(), 2500);
          });
        } }, s.running ? "Updating…" : "Update now") : null,
        h("button", { class: "btn", onclick: reload }, "Check again")),
      h("div", { class: "muted", style: "font-size:12px" }, "Command line on the NAS: ", h("span", { class: "mono" }, "sudo cynthology update"))),
    logBox,
    h("h3", { class: "section-title", style: "max-width:720px" }, "Debian system updates"),
    h("div", { id: "deb-box", class: "muted" }, "Checking Debian packages…"));
  if (s.running) follow("/api/update/log", () => setTimeout(() => location.reload(), 2500));
  const deb = await API.get("/api/update/debian").catch((e) => ({ error: e.message }));
  const box = p.querySelector("#deb-box");
  if (!box) return;
  const debLog = h("pre", { class: "mono update-log hidden" });
  box.replaceChildren(deb.error ? h("div", { class: "err" }, deb.error) : deb.count
      ? h("div", {}, h("div", { style: "color:var(--text)" }, `${deb.count} package update(s) available`,
          deb.security?.length ? h("span", { class: "pill warn", style: "margin-left:8px" }, `${deb.security.length} security`) : null),
        h("div", { style: "font-size:12px;margin:4px 0 8px" }, deb.packages.slice(0, 30).join(", ") + (deb.count > 30 ? " …" : "")),
        h("button", { class: "btn", onclick: async () => {
          await act(() => API.post("/api/update/debian/start"), "Installing Debian updates");
          debLog.classList.remove("hidden");
          for (;;) {
            const r = await API.get("/api/update/debian/log");
            debLog.textContent = r.log; debLog.scrollTop = debLog.scrollHeight;
            if (!r.running) { toast(r.reboot_required ? "Updates installed - restart the NAS to finish" : "Debian updates installed"); break; }
            await new Promise((res) => setTimeout(res, 2500));
          }
        } }, "Install Debian updates"), debLog)
      : h("div", {}, "✓ All Debian packages are up to date."));
}

// Let's Encrypt: a trusted certificate for a public name (renews by itself).
function leBox(reload) {
  const f = { domains: "", email: "" };
  const out = h("div");
  API.get("/api/ddns").then((d) => { if (!f.domains && d.entries[0]) { f.domains = d.entries[0].hostname; dom.value = f.domains; } }).catch(() => {});
  const dom = h("input", { placeholder: "mynas.duckdns.org  nas.example.com", oninput: (e) => (f.domains = e.target.value) });
  return h("div", { class: "form", style: "max-width:720px" },
    h("div", { class: "muted" }, "Browsers trust it without warnings. Needs a public name pointing at your internet address (see External Access → DDNS) ",
      "and your router forwarding ", h("b", {}, "port 80"), " to this NAS while the certificate is issued or renewed (automatic, every ~60 days)."),
    h("label", {}, "Name(s), separated by spaces", dom),
    h("label", {}, "Email for expiry warnings (optional)", h("input", { type: "email", oninput: (e) => (f.email = e.target.value.trim()) })),
    h("div", {}, h("button", { class: "btn primary", onclick: async (e) => {
      e.target.disabled = true;
      try {
        const r = await API.post("/api/security/cert/letsencrypt", { domains: f.domains.split(/[\s,]+/).filter(Boolean), email: f.email });
        if (r.job && await ctJob(out, r.job)) {
          out.append(h("div", { class: "note" }, "✓ Certificate installed. The web interface restarts in a few seconds - reconnecting…"));
          await new Promise((res) => setTimeout(res, 4000)); await waitForServer(); reload();
        }
      } catch (ex) { toast("Error: " + ex.message); }
      e.target.disabled = false;
    } }, "Get certificate")), out);
}

// ---------- Firewall ----------
async function cpFirewall(p) {
  const d = await API.get("/api/firewall");
  const cfg = { enabled: d.enabled, default: d.default, rules: d.rules.map((r) => ({ ...r })), autoblock: { ...d.autoblock, trusted: [...d.autoblock.trusted] } };
  const reload = () => { p.replaceChildren(); cpFirewall(p); };
  const box = h("div");
  const myNet = /^\d+\.\d+\.\d+\.\d+$/.test(d.your_ip) ? d.your_ip.split(".").slice(0, 3).join(".") + ".0/24" : d.your_ip;
  const svcPick = (r) => h("div", { style: "display:flex;flex-wrap:wrap;gap:4px 12px" },
    Object.entries(d.services).map(([k, label]) => h("label", { class: "check", style: "font-size:13px" },
      h("input", { type: "checkbox", checked: r.services.includes(k), onchange: (e) => { r.services = e.target.checked ? [...r.services, k] : r.services.filter((x) => x !== k); } }), label)));
  function draw() {
    box.replaceChildren(
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: cfg.enabled, onchange: (e) => { cfg.enabled = e.target.checked; draw(); } }), h("b", {}, "Turn on the firewall")),
      cfg.enabled ? h("div", { class: "form", style: "margin-top:10px" },
        h("label", {}, "Traffic that no rule matches", h("select", { onchange: (e) => { cfg.default = e.target.value; draw(); } },
          h("option", { value: "allow", selected: cfg.default === "allow" }, "Allow (only the deny rules block anything)"),
          h("option", { value: "deny", selected: cfg.default === "deny" }, "Deny (only what the rules allow gets through - most secure)"))),
        h("h3", { class: "pc-group" }, "Rules (checked from top to bottom; the first match decides)"),
        ...cfg.rules.map((r, i) => h("div", { class: "card", style: "margin-bottom:10px" },
          h("div", { class: "toolbar", style: "margin:0" },
            h("label", { class: "check" }, h("input", { type: "checkbox", checked: r.enabled, onchange: (e) => (r.enabled = e.target.checked) }), "On"),
            h("select", { onchange: (e) => (r.action = e.target.value) }, h("option", { value: "allow", selected: r.action === "allow" }, "Allow"), h("option", { value: "deny", selected: r.action === "deny" }, "Deny")),
            h("input", { value: r.source, placeholder: "from: anyone (or IP / subnet, e.g. 192.168.1.0/24)", style: "flex:1;min-width:220px", oninput: (e) => (r.source = e.target.value.trim()) }),
            h("button", { class: "btn", disabled: i === 0, title: "Move up", onclick: () => { cfg.rules.splice(i - 1, 0, cfg.rules.splice(i, 1)[0]); draw(); } }, "↑"),
            h("button", { class: "btn danger", onclick: () => { cfg.rules.splice(i, 1); draw(); } }, "Remove")),
          svcPick(r),
          h("div", { class: "toolbar", style: "margin:6px 0 0" },
            h("input", { value: r.ports, placeholder: "other ports, e.g. 8080, 9000-9100", style: "flex:1", oninput: (e) => (r.ports = e.target.value) }),
            h("select", { onchange: (e) => (r.protocol = e.target.value) }, [["tcp", "TCP"], ["udp", "UDP"], ["both", "TCP + UDP"]].map(([v, l]) => h("option", { value: v, selected: r.protocol === v }, l))),
            h("input", { value: r.note || "", placeholder: "note", style: "width:160px", oninput: (e) => (r.note = e.target.value) })),
          !r.services.length && !r.ports ? h("div", { class: "muted", style: "font-size:12px" }, "No service or port selected: this rule matches all traffic from the source.") : null)),
        h("div", { class: "toolbar" },
          h("button", { class: "btn", onclick: () => { cfg.rules.push({ enabled: true, action: "allow", services: [], ports: "", protocol: "tcp", source: "", note: "" }); draw(); } }, "+ Add rule"),
          h("button", { class: "btn", onclick: () => { cfg.rules.unshift({ enabled: true, action: "allow", services: [], ports: "", protocol: "tcp", source: myNet, note: "my local network" }); draw(); } }, `+ Allow everything from my network (${myNet})`))) : null,
      h("h3", { class: "pc-group", style: "margin-top:18px" }, "Auto-block"),
      h("div", { class: "form" },
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: cfg.autoblock.enabled, onchange: (e) => (cfg.autoblock.enabled = e.target.checked) }),
          "Block an address completely after repeated failed sign-ins (works even with the firewall off)"),
        h("div", { class: "toolbar", style: "margin:0" },
          h("label", {}, "Failed sign-ins ", h("input", { type: "number", min: 2, value: cfg.autoblock.attempts, style: "width:70px", oninput: (e) => (cfg.autoblock.attempts = +e.target.value) })),
          h("label", {}, "within (minutes) ", h("input", { type: "number", min: 1, value: cfg.autoblock.within_minutes, style: "width:80px", oninput: (e) => (cfg.autoblock.within_minutes = +e.target.value) })),
          h("label", {}, "block for (minutes, 0 = until removed) ", h("input", { type: "number", min: 0, value: cfg.autoblock.block_minutes, style: "width:90px", oninput: (e) => (cfg.autoblock.block_minutes = +e.target.value) }))),
        h("label", {}, "Never block (addresses or subnets, separated by spaces)", h("input", { value: cfg.autoblock.trusted.join(" "), placeholder: myNet,
          oninput: (e) => (cfg.autoblock.trusted = e.target.value.split(/[\s,]+/).filter(Boolean)) })),
        d.blocked.length ? h("div", {}, h("div", { class: "muted" }, "Blocked now:"),
          h("table", {}, d.blocked.map((b) => h("tr", {}, h("td", { class: "mono" }, b.ip), h("td", { class: "muted" }, b.expires_in ? `${Math.round(b.expires_in / 60)} min left` : "until removed"),
            h("td", {}, h("button", { class: "btn", onclick: async () => { await act(() => API.post("/api/firewall/unblock", { ip: b.ip }), "Unblocked"); reload(); } }, "Unblock")))))) : null),
      h("div", { class: "toolbar", style: "margin-top:16px" }, h("button", { class: "btn primary", onclick: applyFw }, "Apply"), h("button", { class: "btn", onclick: reload }, "Cancel")));
  }
  async function applyFw() {
    try { await API.put("/api/firewall", cfg); } catch (e) { return toast("Error: " + e.message); }
    const overlay = h("div", { class: "wiz" }, h("div", { class: "wiz-card" }, h("h2", {}, "Applying firewall rules…"),
      h("p", { class: "muted" }, "Checking that this browser can still reach the NAS. If it can't, the previous rules come back in 90 seconds by themselves.")));
    document.body.append(overlay);
    await new Promise((r) => setTimeout(r, 3000));
    for (let i = 0; i < 20; i++) {
      try { await API.post("/api/firewall/confirm"); overlay.remove(); toast("Firewall rules saved"); return reload(); }
      catch (e) { if (/nothing to confirm/.test(e.message)) { overlay.remove(); toast("The change was rolled back"); return reload(); } }
      await new Promise((r) => setTimeout(r, 2000));
    }
    overlay.remove(); toast("Could not reach the NAS - it will restore the previous rules by itself");
  }
  p.append(h("h2", {}, "Firewall"),
    !d.available ? h("div", { class: "note warn" }, "nftables isn't installed yet - update Cynthology (Control Panel → Update).") : null,
    d.pending ? h("div", { class: "note warn" }, "A firewall change is waiting for confirmation. ",
      h("button", { class: "btn primary", onclick: async () => { await act(() => API.post("/api/firewall/confirm"), "Confirmed"); reload(); } }, "Keep these rules")) : null,
    h("div", { class: "muted", style: "margin-bottom:12px" }, `You are connecting from ${d.your_ip}. Rules that would block you from this page are refused. `,
      "Container ports published by Docker are not covered by these rules."),
    box);
  draw();
}

// ---------- External Access: DDNS ----------
async function cpExternal(p) {
  const d = await API.get("/api/ddns");
  const reload = () => { p.replaceChildren(); cpExternal(p); };
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "-");
  p.append(h("h2", {}, "External Access"),
    h("div", { class: "note" }, "DDNS gives this NAS a fixed name (e.g. mynas.duckdns.org) that follows your internet connection's changing address. ",
      "To reach the NAS from outside, also forward ports on your router - only what you need (e.g. 5001 for this interface, 80 for Let's Encrypt) - and use the Firewall. ",
      "A VPN (e.g. WireGuard on your router) is safer than opening ports."),
    h("h3", { class: "pc-group", style: "margin-top:16px" }, "DDNS"),
    d.entries.length ? h("table", {},
      h("tr", {}, h("th", {}, "Host name"), h("th", {}, "Provider"), h("th", {}, "Address"), h("th", {}, "Status"), h("th", {})),
      d.entries.map((e) => h("tr", {}, h("td", {}, h("b", { translate: "no" }, e.hostname)), h("td", {}, d.providers[e.provider]),
        h("td", { class: "mono" }, e.ip || "-"),
        h("td", {}, e.last_error ? h("span", { class: "err" }, e.last_error) : h("span", { class: "pill ok" }, "Normal"),
          h("div", { class: "muted", style: "font-size:12px" }, `checked ${when(e.last_check)}`)),
        h("td", { style: "white-space:nowrap" }, h("button", { class: "btn", onclick: () => ddnsDialog(d, e, reload) }, "Edit"), " ",
          h("button", { class: "btn danger", onclick: async () => { if (!confirm(`Remove ${e.hostname}?`)) return; await act(() => API.del(`/api/ddns/${e.id}`), "Removed"); reload(); } }, "Remove")))))
      : h("p", { class: "muted" }, "No DDNS host name yet."),
    h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: () => ddnsDialog(d, null, reload) }, "+ Add"),
      d.entries.length ? h("button", { class: "btn", onclick: async () => { await act(() => API.post("/api/ddns/refresh"), "Updated"); reload(); } }, "Update now") : null));
}

function ddnsDialog(d, e, done) {
  const f = e ? { ...e, token: "", password: "" } : { provider: "duckdns", hostname: "", username: "", password: "", token: "", server: "", enabled: true };
  const err = h("div", { class: "err" });
  const fields = h("div", { class: "form", style: "padding:0" });
  const inp = (label, k, attrs = {}) => h("label", {}, label, h("input", { value: f[k] || "", oninput: (ev) => (f[k] = ev.target.value.trim()), ...attrs }));
  const draw = () => fields.replaceChildren(
    inp("Host name", "hostname", { placeholder: f.provider === "duckdns" ? "mynas.duckdns.org" : "nas.example.com" }),
    f.provider === "custom" ? inp("Update server", "server", { placeholder: "members.dyndns.org" }) : null,
    ["duckdns", "cloudflare"].includes(f.provider)
      ? inp(f.provider === "duckdns" ? "Token (from duckdns.org)" : "API token (Cloudflare: My Profile → API Tokens → Edit zone DNS)", "token",
          { type: "password", autocomplete: "off", placeholder: e?.has_token ? "(unchanged)" : "" })
      : [inp("Username", "username", { autocomplete: "off" }), inp("Password", "password", { type: "password", autocomplete: "new-password", placeholder: e?.has_password ? "(unchanged)" : "" })]);
  draw();
  const close = modal(e ? `Edit ${e.hostname}` : "Add DDNS host name", h("div", { class: "form", style: "padding:0" },
    h("label", {}, "Provider", h("select", { onchange: (ev) => { f.provider = ev.target.value; draw(); } },
      Object.entries(d.providers).map(([k, l]) => h("option", { value: k, selected: f.provider === k }, l)))),
    fields, err), [
    ["Cancel", () => close()],
    ["Save", async (ev) => {
      err.textContent = ""; ev.target.disabled = true;
      try {
        const r = await API.post("/api/ddns", f);
        if (r.last_error) { err.textContent = r.last_error; ev.target.disabled = false; done(); return; }
        close(); toast(`${r.hostname} → ${r.ip}`); done();
      } catch (ex) { err.textContent = ex.message; ev.target.disabled = false; }
    }, "primary"]]);
}

// User home service: a private "home" folder for every user (with optional size limits).
function homesBox(reload) {
  const box = h("div", { class: "muted" }, "Loading…");
  Promise.all([API.get("/api/shares/homes/status"), API.get("/api/storage/pools")]).then(([st, pools]) => {
    const f = { enabled: st.enabled, pool_id: st.pool_id || pools[0]?.id, default_user_quota_gb: st.default_user_quota_gb || 0 };
    box.className = "";
    box.replaceChildren(h("div", { class: "form" },
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: st.enabled, onchange: (e) => (f.enabled = e.target.checked) }),
        "Give every user a private home folder (\\\\NAS\\home over SMB, “home” in File Station)"),
      !st.enabled ? h("label", {}, "Location", h("select", { onchange: (e) => (f.pool_id = +e.target.value) }, pools.map((x) => h("option", { value: x.id }, x.label)))) : null,
      h("label", {}, "Default size limit per user in GB (0 = none; for new home folders)", h("input", { type: "number", min: 0, value: f.default_user_quota_gb, style: "width:140px",
        oninput: (e) => (f.default_user_quota_gb = +e.target.value || 0) })),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => { await act(() => API.put("/api/shares/homes", f), "Saved"); reload(); } }, "Save")),
      st.enabled ? h("table", {}, h("tr", {}, h("th", {}, "User"), h("th", {}, "Home folder used"), h("th", {}, "Limit"), h("th", {})),
        Object.entries(st.users).map(([u, x]) => h("tr", {}, h("td", { translate: "no" }, u), h("td", {}, x.usage ? fmtBytes(x.usage.used) : "-"),
          h("td", {}, x.quota_gb ? `${x.quota_gb} GB` : "none"),
          h("td", {}, h("button", { class: "btn", onclick: async () => {
            const v = prompt(`Home folder size limit for ${u} in GB (0 = none):`, String(x.quota_gb || 0)); if (v === null) return;
            await act(() => API.put(`/api/shares/homes/quota/${u}`, { quota_gb: +v || 0 }), "Limit saved"); reload();
          } }, "Set limit"))))) : null,
      h("div", { class: "muted", style: "font-size:12px" }, "Size limits apply per folder: a user's home folder, or a whole shared folder (Shared Folders → Limit). ",
        "Btrfs has no limit across all folders for one user.")));
  }).catch((e) => { box.textContent = e.message; });
  return box;
}

// ---------- Task Scheduler ----------
async function cpTasks(p) {
  const [d, disks, shares, users] = await Promise.all([API.get("/api/tasks"), API.get("/api/storage/disks"), API.get("/api/shares"), API.get("/api/users").catch(() => [])]);
  const reload = () => { p.replaceChildren(); cpTasks(p); };
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "-");
  p.append(h("div", { class: "toolbar" }, h("h2", { style: "margin:0;flex:1" }, "Task Scheduler"),
      h("button", { class: "btn primary", onclick: () => taskEditor(p, null, { disks, shares, users, kinds: d.kinds }, reload) }, "+ Create")),
    d.tasks.length ? h("table", {},
      h("tr", {}, h("th", {}, "Task"), h("th", {}, "Type"), h("th", {}, "Schedule"), h("th", {}, "Last run"), h("th", {})),
      d.tasks.map((t) => h("tr", {},
        h("td", {}, h("b", {}, t.name), t.enabled ? null : h("span", { class: "pill", style: "margin-left:6px" }, "off")),
        h("td", {}, d.kinds[t.kind]),
        h("td", {}, t.when, t.next ? h("div", { class: "muted", style: "font-size:12px" }, `next: ${t.next}`) : null),
        h("td", {}, t.running ? h("span", { class: "pill info" }, "running…") : t.last
          ? [h("span", { class: "pill " + (t.last.exit_code === 0 ? "ok" : "bad") }, t.last.exit_code === 0 ? "OK" : `failed (${t.last.exit_code})`),
             h("div", { class: "muted", style: "font-size:12px" }, when(t.last.started))] : h("span", { class: "muted" }, "never")),
        h("td", { style: "white-space:nowrap" },
          h("button", { class: "btn", onclick: async () => { const r = await act(() => API.post(`/api/tasks/${t.id}/run`), "Task started"); setTimeout(reload, 2500); } }, "Run now"), " ",
          h("button", { class: "btn", onclick: () => taskHistory(t) }, "Results"), " ",
          h("button", { class: "btn", onclick: () => taskEditor(p, t, { disks, shares, users, kinds: d.kinds }, reload) }, "Edit"), " ",
          h("button", { class: "btn danger", onclick: async () => { if (!confirm(`Delete task ${t.name}?`)) return; await act(() => API.del(`/api/tasks/${t.id}`), "Deleted"); reload(); } }, "Delete")))))
      : h("p", { class: "muted" }, "No scheduled tasks yet. Tip: a monthly extended S.M.A.R.T. test catches failing drives early."),
    h("p", { class: "muted", style: "font-size:12px" }, "Failed runs send an alert (Control Panel → Notifications). Missed runs (NAS switched off) run at the next start."));
}

async function taskHistory(t) {
  const hs = await API.get(`/api/tasks/${t.id}/history`);
  modal(`Results: ${t.name}`, hs.length ? h("div", { style: "max-height:60vh;overflow:auto;display:grid;gap:10px" }, hs.map((e) => h("div", {},
    h("div", {}, h("span", { class: "pill " + (e.exit_code === 0 ? "ok" : "bad") }, e.exit_code === 0 ? "OK" : `exit ${e.exit_code}`), " ",
      new Date(e.started * 1000).toLocaleString(), h("span", { class: "muted" }, ` · ${e.duration}s`)),
    e.output ? h("pre", { class: "mono", style: "white-space:pre-wrap;background:var(--panel-2);padding:8px;border-radius:6px;margin:4px 0 0;font-size:12px;max-height:200px;overflow:auto" }, e.output) : null)))
    : h("p", { class: "muted" }, "Not run yet."));
}

function taskEditor(p, t, ctx, done) {
  const f = t ? JSON.parse(JSON.stringify(t)) : { name: "", kind: "smart", enabled: true, notify_on_failure: true,
    schedule: { every: "monthly", time: "03:00", days: ["sun"], day_of_month: 1 }, script: "#!/bin/bash\n", user: "root",
    test: "long", disks: [], shares: [], older_than_days: 30 };
  const err = h("div", { class: "err" });
  const body = h("div", { class: "form", style: "padding:0" });
  const sch = h("div", { class: "toolbar", style: "margin:0;flex-wrap:wrap" });
  const drawSch = () => {
    const s = f.schedule;
    sch.replaceChildren(
      h("select", { onchange: (e) => { s.every = e.target.value; drawSch(); } },
        [["hourly", "Every hour"], ["daily", "Every day"], ["weekly", "Every week"], ["monthly", "Every month"], ["boot", "At startup"]].map(([v, l]) => h("option", { value: v, selected: s.every === v }, l))),
      s.every === "weekly" ? h("span", { style: "display:flex;gap:6px" }, ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((dd) => h("label", { class: "check", style: "font-size:13px" },
        h("input", { type: "checkbox", checked: s.days.includes(dd), onchange: (e) => { s.days = e.target.checked ? [...s.days, dd] : s.days.filter((x) => x !== dd); } }), dd.charAt(0).toUpperCase() + dd.slice(1)))) : null,
      s.every === "monthly" ? h("label", {}, "on day ", h("input", { type: "number", min: 1, max: 28, value: s.day_of_month, style: "width:70px", oninput: (e) => (s.day_of_month = +e.target.value) })) : null,
      s.every !== "boot" ? h("label", {}, s.every === "hourly" ? "at minute " : "at ", h("input", { type: "time", value: s.time, oninput: (e) => (s.time = e.target.value) })) : null);
  };
  const draw = () => {
    drawSch();
    body.replaceChildren(
      h("label", {}, "Name", h("input", { value: f.name, placeholder: f.kind === "smart" ? "Monthly drive check" : "", oninput: (e) => (f.name = e.target.value) })),
      t ? null : h("label", {}, "Type", h("select", { onchange: (e) => { f.kind = e.target.value; draw(); } },
        Object.entries(ctx.kinds).map(([k, l]) => h("option", { value: k, selected: f.kind === k }, l)))),
      h("div", {}, h("div", { class: "muted", style: "font-size:13px;margin-bottom:4px" }, "When"), sch),
      f.kind === "script" ? [
        h("label", {}, "Run as", h("select", { onchange: (e) => (f.user = e.target.value) },
          [h("option", { value: "root", selected: f.user === "root" }, "root (administrator)"), ...ctx.users.map((u) => h("option", { value: u.name, selected: f.user === u.name, translate: "no" }, u.name))])),
        h("label", {}, "Script (bash)", h("textarea", { class: "mono", rows: 8, spellcheck: "false", style: "font-size:13px", oninput: (e) => (f.script = e.target.value) }, f.script))] : null,
      f.kind === "smart" ? [
        h("label", {}, "Test", h("select", { onchange: (e) => (f.test = e.target.value) },
          h("option", { value: "short", selected: f.test === "short" }, "Short (about 2 minutes)"),
          h("option", { value: "long", selected: f.test === "long" }, "Extended (reads the whole drive - hours; the NAS stays usable)"))),
        h("div", {}, h("div", { class: "muted", style: "font-size:13px" }, "Drives (none ticked = all data drives)"),
          ctx.disks.filter((dk) => dk.serial).map((dk) => h("label", { class: "check" },
            h("input", { type: "checkbox", checked: f.disks.includes(dk.serial), onchange: (e) => { f.disks = e.target.checked ? [...f.disks, dk.serial] : f.disks.filter((x) => x !== dk.serial); } }),
            `${dk.name} - ${dk.model || "?"} (${dk.serial})`)))] : null,
      f.kind === "recycle" ? [
        h("label", {}, "Delete items that have been in the recycle bin for at least (days; 0 = everything)",
          h("input", { type: "number", min: 0, value: f.older_than_days, style: "width:100px", oninput: (e) => (f.older_than_days = +e.target.value) })),
        h("div", {}, h("div", { class: "muted", style: "font-size:13px" }, "Shared folders (none ticked = all)"),
          ctx.shares.filter((s) => s.recycle_bin).map((s) => h("label", { class: "check" },
            h("input", { type: "checkbox", checked: f.shares.includes(s.name), onchange: (e) => { f.shares = e.target.checked ? [...f.shares, s.name] : f.shares.filter((x) => x !== s.name); } }), s.name)))] : null,
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.enabled, onchange: (e) => (f.enabled = e.target.checked) }), "Enabled"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.notify_on_failure, onchange: (e) => (f.notify_on_failure = e.target.checked) }), "Alert me when it fails"),
      err);
  };
  draw();
  const close = modal(t ? `Edit ${t.name}` : "Create task", body, [["Cancel", () => close()], ["Save", async (e) => {
    err.textContent = ""; e.target.disabled = true;
    try { await API.post("/api/tasks", f); close(); toast("Task saved"); done(); }
    catch (ex) { err.textContent = ex.message; e.target.disabled = false; }
  }, "primary"]]);
}

// Who may use a shared folder: read/write and read-only lists of users and @groups (local or domain).
async function permsDialog(s, done) {
  const [users, groups] = await Promise.all([API.get("/api/users").catch(() => []), API.get("/api/users/groups").catch(() => [])]);
  const v = { write: s.write_users.join("\n"), read: s.read_users.join("\n") };
  const err = h("div", { class: "err" });
  const wTa = h("textarea", { rows: 4, class: "mono", style: "font-size:13px", oninput: (e) => (v.write = e.target.value) }, v.write);
  const rTa = h("textarea", { rows: 3, class: "mono", style: "font-size:13px", oninput: (e) => (v.read = e.target.value) }, v.read);
  const add = (ta, key, name) => { const lines = ta.value.split("\n").map((x) => x.trim()).filter(Boolean); if (!lines.includes(name)) lines.push(name); ta.value = v[key] = lines.join("\n"); };
  const names = [...groups.filter((g) => ["users", "cynthology-admin"].includes(g.name) || g.members.length).map((g) => "@" + g.name), ...users.map((u) => u.name)];
  const chips = (ta, key) => h("div", { style: "display:flex;gap:4px;flex-wrap:wrap" },
    names.map((n) => h("button", { class: "btn", type: "button", translate: "no", style: "padding:1px 8px;font-size:12px", onclick: () => add(ta, key, n) }, "+ " + n)));
  const close = modal(`Permissions: ${s.name}`, h("div", { class: "form", style: "padding:0" },
    h("div", { class: "muted", style: "font-size:12px" }, "One per line. A name starting with @ is a group (e.g. @users = all local users). ",
      "On a domain: CORP\\alice or @CORP\\Domain Users."),
    domainSearch((n) => add(wTa, "write", n)),
    h("label", {}, "Read/write", wTa), chips(wTa, "write"),
    h("label", {}, "Read-only", rTa), chips(rTa, "read"),
    err), [["Cancel", () => close()], ["Save", async (e) => {
      err.textContent = ""; e.target.disabled = true;
      const split = (x) => x.split("\n").map((y) => y.trim()).filter(Boolean);
      try { await API.put(`/api/shares/${s.name}/permissions`, { write_users: split(v.write), read_users: split(v.read) }); close(); toast("Permissions saved"); done(); }
      catch (ex) { err.textContent = ex.message; e.target.disabled = false; }
    }, "primary"]]);
}

// Search domain users/groups (only shown when the NAS is joined to a domain); click adds to read/write.
function domainSearch(onPick) {
  const box = h("div");
  API.get("/api/directory").then((d) => {
    if (!d.joined) return;
    const res = h("div", { style: "display:flex;gap:4px;flex-wrap:wrap" });
    let timer;
    box.append(h("label", {}, `Find in domain ${d.domain}`, h("input", { placeholder: "name of a user or group", oninput: (e) => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const r = await API.get(`/api/directory/lookup?q=${encodeURIComponent(e.target.value)}`).catch(() => ({ users: [], groups: [] }));
        res.replaceChildren(...r.groups.map((g) => h("button", { class: "btn", type: "button", translate: "no", style: "padding:1px 8px;font-size:12px", onclick: () => onPick("@" + g) }, "+ @" + g)),
          ...r.users.map((u) => h("button", { class: "btn", type: "button", translate: "no", style: "padding:1px 8px;font-size:12px", onclick: () => onPick(u) }, "+ " + u)));
      }, 300);
    } })), res);
  }).catch(() => {});
  return box;
}

// ---------- Domain/LDAP ----------
async function cpDomain(p) {
  const d = await API.get("/api/directory");
  const reload = () => { p.replaceChildren(); cpDomain(p); };
  p.append(h("h2", {}, "Domain/LDAP"));
  if (!d.available) p.append(h("div", { class: "note warn" }, "The domain software isn't installed yet - update Cynthology (Control Panel → Update)."));
  if (d.joined) {
    const row = (k, v) => h("tr", {}, h("th", { style: "width:200px" }, k), h("td", {}, v));
    p.append(h("table", { class: "kv" },
        row("Domain", h("b", { translate: "no" }, d.domain)), row("NetBIOS name", h("span", { translate: "no" }, d.workgroup)),
        row("Status", h("span", { class: "pill " + (d.healthy ? "ok" : "bad") }, d.healthy ? "Connected" : "Problem")),
        d.detail && !d.healthy ? row("", h("span", { class: "err" }, d.detail)) : null,
        d.dc ? row("Domain controller", d.dc) : null,
        row("NAS administrators", h("span", { translate: "no" }, `${d.workgroup}\\${d.admin_group}`))),
      h("div", { class: "note", style: "margin-top:12px" }, "Domain users sign in here as ", h("b", { translate: "no" }, `${d.workgroup}\\name`),
        " and on Windows with their usual domain login. Give them access in Shared Folders → Permissions (e.g. ",
        h("span", { class: "mono", translate: "no" }, `@${d.workgroup}\\Domain Users`), ")."),
      h("div", { class: "toolbar", style: "margin-top:14px" }, h("button", { class: "btn danger", onclick: async () => {
        const user = prompt("Leave the domain.\n\nTo also remove this NAS's computer account from the domain, enter a domain admin user name (or leave empty):", "");
        if (user === null) return;
        const pw = user ? prompt(`Password for ${user}:`) : "";
        if (!confirm("Domain users lose access to this NAS. Continue?")) return;
        await act(() => API.post("/api/directory/leave", { username: user, password: pw || "" }), "Left the domain"); reload();
      } }, "Leave domain")));
    return;
  }
  const f = { domain: "", workgroup: "", username: "Administrator", password: "", admin_group: "Domain Admins", dns: "" };
  const err = h("div", { class: "err" });
  const inp = (label, k, attrs = {}) => h("label", {}, label, h("input", { value: f[k], oninput: (e) => (f[k] = e.target.value.trim()), ...attrs }));
  p.append(h("div", { class: "note" }, "Join an Active Directory domain (Windows Server or Samba AD) so domain users can use this NAS with their domain accounts. ",
      "This NAS's clock must be within 5 minutes of the domain controller's."),
    h("div", { class: "form", style: "max-width:600px;margin-top:12px" },
      inp("Domain (DNS name)", "domain", { placeholder: "corp.example.com" }),
      inp("NetBIOS domain name (optional)", "workgroup", { placeholder: "CORP" }),
      inp("Domain controller's IP address as DNS server (optional)", "dns", { placeholder: "192.168.1.10" }),
      inp("Domain account allowed to join computers", "username"),
      h("label", {}, "Password", h("input", { type: "password", autocomplete: "off", oninput: (e) => (f.password = e.target.value) })),
      inp("Domain group whose members administer this NAS", "admin_group"),
      err,
      h("div", {}, h("button", { class: "btn primary", onclick: async (e) => {
        err.textContent = ""; e.target.disabled = true; e.target.textContent = "Joining…";
        try { await API.post("/api/directory/join", f); toast(`Joined ${f.domain}`); reload(); }
        catch (ex) { err.textContent = ex.message; e.target.disabled = false; e.target.textContent = "Join"; }
      } }, "Join"))),
    h("p", { class: "muted", style: "font-size:12px" }, "Plain LDAP servers (without Samba support) can't provide Windows file sharing logins, so only Active Directory is offered."));
}

// ---------- VPN Server (WireGuard) ----------
async function cpVpn(p) {
  const d = await API.get("/api/vpn");
  const reload = () => { p.replaceChildren(); cpVpn(p); };
  const f = { enabled: d.enabled, subnet: d.subnet, port: d.port, endpoint: d.endpoint || d.suggested_endpoint || "", dns: d.dns || "" };
  const ago = (t) => { if (!t) return "never"; const s = Math.round(Date.now() / 1000 - t); return s < 120 ? `${s} s ago` : s < 7200 ? `${Math.round(s / 60)} min ago` : new Date(t * 1000).toLocaleString(); };
  const inp = (label, k, attrs = {}) => h("label", {}, label, h("input", { value: f[k], oninput: (e) => (f[k] = attrs.type === "number" ? +e.target.value : e.target.value.trim()), ...attrs }));
  p.append(h("h2", {}, "VPN Server"),
    !d.available ? h("div", { class: "note warn" }, "WireGuard isn't installed yet - update Cynthology (Control Panel → Update).") : null,
    h("div", { class: "note" }, "WireGuard lets your phone and laptop reach this NAS and your home network from anywhere, encrypted - ",
      "safer than opening ports for each service. On your router, forward ", h("b", {}, `UDP port ${d.port}`), " to this NAS; that is the only port needed."),
    d.firewall_warning ? h("div", { class: "note warn", style: "margin-top:8px" }, d.firewall_warning) : null,
    h("div", { class: "form", style: "max-width:640px;margin-top:12px" },
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.enabled, onchange: (e) => (f.enabled = e.target.checked) }), h("b", {}, "Turn on the VPN server"),
        d.enabled ? h("span", { class: "pill " + (d.running ? "ok" : "bad"), style: "margin-left:8px" }, d.running ? "running" : "not running") : null),
      inp("Public address devices connect to (your DDNS name or public IP)", "endpoint", { placeholder: "mynas.duckdns.org" }),
      h("div", { class: "toolbar", style: "margin:0" }, inp("UDP port", "port", { type: "number", style: "width:110px" }), inp("VPN subnet", "subnet", { style: "width:160px" })),
      inp("DNS server for devices (optional; default: the one this NAS uses)", "dns", { placeholder: "192.168.1.1" }),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => { await act(() => API.put("/api/vpn", f), "VPN settings saved"); reload(); } }, "Save"))),
    h("h3", { class: "pc-group", style: "margin-top:20px" }, "Devices"),
    d.peers.length ? h("table", {},
      h("tr", {}, h("th", {}, "Device"), h("th", {}, "Address"), h("th", {}, "Mode"), h("th", {}, "Last seen"), h("th", {}, "Traffic"), h("th", {})),
      d.peers.map((x) => h("tr", {},
        h("td", {}, h("b", { translate: "no" }, x.name), x.enabled ? null : h("span", { class: "pill", style: "margin-left:6px" }, "off")),
        h("td", { class: "mono" }, x.address),
        h("td", {}, x.full_tunnel ? "All traffic" : "Home network"),
        h("td", {}, x.online ? h("span", { class: "pill ok" }, "connected") : h("span", { class: "muted" }, ago(x.handshake)),
          x.endpoint && x.online ? h("div", { class: "muted mono", style: "font-size:11px" }, x.endpoint) : null),
        h("td", { class: "muted", style: "font-size:12px" }, `↓ ${fmtBytes(x.tx)} ↑ ${fmtBytes(x.rx)}`),
        h("td", { style: "white-space:nowrap" },
          h("button", { class: "btn", onclick: () => vpnConfigDialog(x.id) }, "Show config"), " ",
          h("button", { class: "btn", onclick: async () => { await act(() => API.put(`/api/vpn/peers/${x.id}`, { enabled: !x.enabled }), x.enabled ? "Device turned off" : "Device turned on"); reload(); } }, x.enabled ? "Turn off" : "Turn on"), " ",
          h("button", { class: "btn danger", onclick: async () => { if (!confirm(`Remove ${x.name}? It can't connect any more.`)) return; await act(() => API.del(`/api/vpn/peers/${x.id}`), "Device removed"); reload(); } }, "Remove")))))
      : h("p", { class: "muted" }, "No devices yet."),
    h("div", { class: "toolbar" }, h("button", { class: "btn primary", disabled: !d.enabled, onclick: () => {
      const v = { name: "", full_tunnel: false };
      const err = h("div", { class: "err" });
      const close = modal("Add a device", h("div", { class: "form", style: "padding:0" },
        h("label", {}, "Device name", h("input", { placeholder: "Alice's phone", oninput: (e) => (v.name = e.target.value) })),
        h("label", { class: "check" }, h("input", { type: "radio", name: "tun", checked: true, onchange: () => (v.full_tunnel = false) }),
          h("div", {}, h("b", {}, "Home network only"), h("div", { class: "muted" }, "Reach the NAS and devices at home; everything else uses the device's own internet."))),
        h("label", { class: "check" }, h("input", { type: "radio", name: "tun", onchange: () => (v.full_tunnel = true) }),
          h("div", {}, h("b", {}, "All traffic through home"), h("div", { class: "muted" }, "Safer on public Wi-Fi: the device browses as if it were at home."))),
        err), [["Cancel", () => close()], ["Add", async (e) => {
          err.textContent = ""; e.target.disabled = true;
          try { const r = await API.post("/api/vpn/peers", v); close(); reload(); vpnConfigDialog(r.id); }
          catch (ex) { err.textContent = ex.message; e.target.disabled = false; }
        }, "primary"]]);
    } }, "+ Add device")));
}

async function vpnConfigDialog(id) {
  const c = await API.get(`/api/vpn/peers/${id}/config`);
  modal(`VPN for ${c.name}`, h("div", { class: "form", style: "padding:0" },
    h("ol", { class: "wiz-list" },
      h("li", {}, "Install the WireGuard app (iPhone/Android: app store; Windows/Mac/Linux: wireguard.com)."),
      h("li", {}, "Phone: tap + → Scan from QR code. Computer: import the downloaded file.")),
    c.qr_svg ? h("div", { class: "qr", html: c.qr_svg, style: "background:#fff;padding:10px;border-radius:8px;width:max-content;margin:auto" }) : null,
    h("details", {}, h("summary", { class: "muted" }, "Show the configuration text"),
      h("pre", { class: "mono", style: "white-space:pre-wrap;font-size:12px;background:var(--panel-2);padding:10px;border-radius:8px", translate: "no" }, c.config)),
    h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: () => {
      const a = h("a", { href: URL.createObjectURL(new Blob([c.config], { type: "text/plain" })), download: `${c.name.replace(/[^A-Za-z0-9._-]/g, "_")}.conf` });
      document.body.append(a); a.click(); a.remove();
    } }, "Download .conf")),
    h("div", { class: "note warn" }, "This contains the device's private key - anyone with it can connect. Don't share it.")), [["Done", null, "primary"]]);
}
