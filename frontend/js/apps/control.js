// Control Panel: shared folders, users, file services, info.
WM.register("control", {
  name: "Control Panel", icon: "⚙️", color: "#4f6d8a",
  open(body) {
    sectioned({
      general: { label: "General", render: cpGeneral },
      network: { label: "Network", render: cpNetwork },
      security: { label: "Security", render: cpSecurity },
      notify: { label: "Notifications", render: cpNotify },
      ups: { label: "UPS", render: cpUps },
      usb: { label: "External Devices", render: cpUsb },
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
          s.usb ? h("span", { class: "pill" }, "USB") : h("button", { class: "btn", onclick: () => act(() => API.post(`/api/snapshots/${s.name}`), "Snapshot taken") }, "Snapshot"), " ",
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
      h("h3", { class: "pc-group", style: "margin-top:16px" }, "DNS servers"),
      h("input", { value: cfg.dns.join(", "), placeholder: "empty = from DHCP, e.g. 1.1.1.1, 9.9.9.9", style: "width:100%;max-width:520px",
        oninput: (e) => (cfg.dns = e.target.value.split(/[ ,]+/).filter(Boolean)) }),
      h("div", { class: "toolbar", style: "margin-top:16px" }, h("button", { class: "btn primary", onclick: applyNet }, "Apply"),
        h("button", { class: "btn", onclick: reload }, "Cancel")));
  }

  async function applyNet() {
    const statics = [...cfg.nics.filter((n) => n.enabled), ...cfg.bonds].filter((x) => x.ipv4.mode === "static").map((x) => x.ipv4.address.split("/")[0]);
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
