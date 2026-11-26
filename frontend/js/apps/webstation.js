// Web Station: websites from the "web" shared folder with nginx + PHP, virtual hosts, HTTPS, reverse proxy.
WM.register("webstation", {
  name: "Web Station", icon: "🌐", color: "#0ea5e9", w: 980, h: 640,
  open(body) {
    let show = null;
    show = sectioned({
      overview: { label: "Overview", render: (p) => wsOverview(p, (k) => show(k)) },
      sites: { label: "Websites", render: wsSites },
      php: { label: "PHP", render: wsPhp },
    }, body);
  },
});

async function wsStatus(p) {
  try { return await API.get("/api/webstation"); } catch (e) { p.replaceChildren(h("div", { class: "note warn" }, e.message)); return null; }
}

function wsNotInstalled(p, after) {
  const box = h("div");
  p.replaceChildren(h("h2", {}, "Web Station"),
    h("p", {}, "Host websites on this NAS: static pages, PHP apps such as WordPress, Joomla or Nextcloud, and reverse proxies to apps running in containers."),
    h("p", { class: "muted" }, "Installs Debian's nginx web server and PHP-FPM with common extensions (MySQL/MariaDB, PostgreSQL, SQLite, GD, Imagick, cURL, intl, zip, APCu, Redis…). Websites live in a new shared folder called “web”."),
    h("button", { class: "btn primary", onclick: async (e) => {
      e.target.disabled = true;
      try {
        const r = await API.post("/api/webstation/install");
        if (r.job && !(await ctJob(box, r.job))) { e.target.disabled = false; return; }
        box.append(h("p", { class: "muted" }, "Setting up the web folder and default website…"));
        await act(() => API.post("/api/webstation/setup"), "Web Station is ready");
        after();
      } catch (ex) { toast("Error: " + ex.message); e.target.disabled = false; }
    } }, "Install Web Station"), box);
}

function wsUrl(s) {
  const https = s.https_port && (!s.http_port || s.redirect_https);
  const port = https ? s.https_port : s.http_port;
  const host = s.type === "name" ? s.hostname.split(" ")[0] : location.hostname;
  const dflt = (https && port === 443) || (!https && port === 80);
  return `${https ? "https" : "http"}://${host}${dflt ? "" : ":" + port}/`;
}

async function wsOverview(p, show) {
  const s = await wsStatus(p); if (!s) return;
  if (!s.installed) return wsNotInstalled(p, () => wsOverview(p, show));
  const svc = (name, on) => h("div", { class: "card" }, h("div", { class: "muted" }, name),
    h("div", {}, h("span", { class: "pill " + (on ? "ok" : "") }, on ? "Running" : "Stopped")));
  p.replaceChildren(h("h2", {}, "Web Station"),
    h("div", { class: "cards" }, svc("Web server (nginx)", s.running), svc(`PHP ${s.php_version || ""}`, s.php_running),
      h("div", { class: "card" }, h("div", { class: "muted" }, "Websites"), h("div", { style: "font-size:24px;font-weight:600" }, String(s.sites.filter((x) => x.enabled).length)))),
    h("label", { class: "check", style: "margin:16px 0" }, h("input", { type: "checkbox", checked: s.enabled, onchange: async (e) => {
      await act(() => API.put("/api/webstation/enabled", { enabled: e.target.checked }), e.target.checked ? "Web Station turned on" : "Web Station turned off");
      wsOverview(p, show);
    } }), "Turn on Web Station"),
    h("p", {}, "Web folder: ", h("span", { class: "mono" }, s.web_root || "(created with the first website)"),
      h("span", { class: "muted" }, " — reach it from your computer as the shared folder “web” to upload websites.")),
    h("h3", { class: "pc-group" }, "Websites"),
    s.sites.length ? h("ul", {}, s.sites.map((x) => h("li", {}, h("b", {}, x.name), " — ",
      x.enabled ? h("a", { href: wsUrl(x), target: "_blank" }, wsUrl(x)) : h("span", { class: "muted" }, "off")))) : h("p", { class: "muted" }, "None yet."),
    h("button", { class: "btn", onclick: () => show("sites") }, "Manage websites"));
}

async function wsSites(p) {
  const s = await wsStatus(p); if (!s) return;
  if (!s.installed) return wsNotInstalled(p, () => wsSites(p));
  const kinds = { static: "Static website", php: "PHP website", proxy: "Reverse proxy" };
  p.replaceChildren(h("div", { class: "toolbar" }, h("h2", { style: "margin:0;flex:1" }, "Websites"),
      h("button", { class: "btn primary", onclick: () => wsEdit(p, null, s) }, "Create")),
    s.sites.length ? h("table", {},
      h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", {}, "Address"), h("th", {}, "Serves"), h("th", {}, "Status"), h("th", {})),
      s.sites.map((x) => h("tr", {},
        h("td", {}, h("b", {}, x.name)), h("td", {}, kinds[x.kind]),
        h("td", {}, h("a", { href: wsUrl(x), target: "_blank" }, wsUrl(x)),
          h("div", { class: "muted", style: "font-size:12px" }, [x.http_port && `HTTP ${x.http_port}`, x.https_port && `HTTPS ${x.https_port}`].filter(Boolean).join(" · "))),
        h("td", { class: "mono", style: "font-size:12px" }, x.kind === "proxy" ? x.proxy : "web/" + x.folder),
        h("td", {}, h("span", { class: "pill " + (x.enabled ? "ok" : "") }, x.enabled ? "On" : "Off")),
        h("td", { style: "white-space:nowrap" }, h("button", { class: "btn", onclick: () => wsEdit(p, x, s) }, "Edit"), " ",
          h("button", { class: "btn danger", onclick: async () => {
            if (!confirm(`Delete website "${x.name}"? Its files in the web folder are kept.`)) return;
            await act(() => API.del(`/api/webstation/sites/${x.id}`), "Website deleted"); wsSites(p);
          } }, "Delete")))))
      : h("p", { class: "muted" }, "No websites yet."));
}

function wsEdit(p, site, s) {
  const v = site ? { ...site } : { name: "", enabled: true, type: "name", hostname: "", http_port: 80, https_port: 443, redirect_https: false, kind: "php", folder: "", proxy: "http://127.0.0.1:" };
  const err = h("div", { class: "err" });
  const field = (label, el, hint) => h("label", {}, h("span", {}, label), el, hint ? h("div", { class: "muted", style: "font-size:12px" }, hint) : null);
  const inp = (k, attrs = {}) => h("input", { value: v[k] ?? "", oninput: (e) => (v[k] = e.target.value), ...attrs });
  const port = (k) => h("input", { type: "number", min: 1, max: 65535, value: v[k] ?? "", placeholder: "off", style: "width:110px",
    oninput: (e) => (v[k] = e.target.value ? Number(e.target.value) : null) });
  const dyn = h("div", { class: "form", style: "padding:0" });
  const draw = () => dyn.replaceChildren(
    v.type === "name" ? field("Host name(s)", inp("hostname", { placeholder: "www.example.com example.com" }),
      "Separate several names with spaces. Point them at this NAS in DNS (or your router / hosts file).")
      : h("div", { class: "muted", style: "font-size:12px" }, "Port-based: the website answers on its own port(s), at any address of the NAS."),
    h("div", { class: "toolbar" }, field("HTTP port", port("http_port")), field("HTTPS port", port("https_port"))),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: v.redirect_https, onchange: (e) => (v.redirect_https = e.target.checked) }), "Send HTTP visitors to HTTPS"),
    v.kind === "proxy"
      ? field("Forward to", inp("proxy", { placeholder: "http://127.0.0.1:8096" }), "Address of the app, e.g. a container's port on this NAS.")
      : field("Folder", h("div", { class: "toolbar", style: "margin:0" }, h("span", { class: "mono" }, "web/"), inp("folder", { placeholder: "(the whole web folder)", style: "flex:1" })),
          "Inside the “web” shared folder; created if it doesn't exist."));
  const sel = (k, opts) => h("select", { onchange: (e) => { v[k] = e.target.value; draw(); } },
    Object.entries(opts).map(([val, l]) => h("option", { value: val, selected: v[k] === val }, l)));
  draw();
  p.replaceChildren(h("div", { class: "form", style: "max-width:640px" },
    h("h2", {}, site ? `Edit ${site.name}` : "Create website"),
    field("Name", inp("name", { placeholder: "My blog" })),
    field("Serves", sel("kind", { php: "PHP website (also serves static files)", static: "Static website (HTML only)", proxy: "Reverse proxy to another address" })),
    field("Answers by", sel("type", { name: "Host name (several websites can share ports 80/443)", port: "Port (website gets its own ports)" })),
    dyn,
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: v.enabled, onchange: (e) => (v.enabled = e.target.checked) }), "Enabled"),
    err,
    h("div", { class: "toolbar", style: "justify-content:flex-end" },
      h("button", { class: "btn", onclick: () => wsSites(p) }, "Cancel"),
      h("button", { class: "btn primary", onclick: async (e) => {
        err.textContent = ""; e.target.disabled = true;
        try { await API.put("/api/webstation/sites", v); toast("Website saved"); wsSites(p); }
        catch (ex) { err.textContent = ex.message; e.target.disabled = false; }
      } }, "Save"))));
}

async function wsPhp(p) {
  const s = await wsStatus(p); if (!s) return;
  if (!s.installed) return wsNotInstalled(p, () => wsPhp(p));
  const v = { ...s.php };
  const row = (k, label, hint) => h("label", {}, h("span", {}, label),
    h("input", { value: v[k], style: "width:140px", oninput: (e) => (v[k] = e.target.value) }), h("div", { class: "muted", style: "font-size:12px" }, hint));
  p.replaceChildren(h("div", { class: "form", style: "max-width:640px" },
    h("h2", {}, `PHP ${s.php_version || ""}`),
    h("div", { class: "muted" }, "Applies to every PHP website. For large uploads raise both the upload size and the request size."),
    row("memory_limit", "Memory limit", "e.g. 256M, 1G"),
    row("upload_max_filesize", "Largest file upload", "e.g. 512M, 2G"),
    row("post_max_size", "Largest request (form or upload)", "at least the upload size"),
    row("max_execution_time", "Script time limit (seconds)", "e.g. 120"),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: v.display_errors, onchange: (e) => (v.display_errors = e.target.checked) }), "Show PHP errors in the browser (for testing only)"),
    h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: () => act(() => API.put("/api/webstation/php", v), "PHP settings saved") }, "Save")),
    h("h3", { class: "pc-group" }, "Extensions"),
    s.extensions.length ? h("div", { style: "display:flex;flex-wrap:wrap;gap:6px" }, s.extensions.map((x) => h("span", { class: "pill" }, x)))
      : h("p", { class: "muted" }, "—"),
    h("p", { class: "muted", style: "font-size:12px" }, "Need a database? Install MariaDB and phpMyAdmin from Package Center → Synology alternatives; PHP connects to 127.0.0.1:3306.")));
}
