// Container Manager: containers, images, Compose projects, registry search, settings.
WM.register("containers", {
  name: "Container Manager", icon: "🐳", color: "#2f7fd6", w: 1040, h: 660,
  open(body, win) {
    const timers = [];
    win.cleanup.push(() => timers.forEach(clearInterval));
    sectioned({
      overview: { label: "Overview", render: ctOverview },
      containers: { label: "Containers", render: (p) => ctContainers(p, timers) },
      projects: { label: "Projects", render: ctProjects },
      images: { label: "Images", render: ctImages },
      registry: { label: "Registry", render: ctRegistry },
      settings: { label: "Settings", render: ctSettings },
    }, body);
  },
});

function ctError(p, e) {
  p.replaceChildren(h("h2", {}, "Container Manager"), h("div", { class: "note warn" }, e.message));
}

// Poll a background job, showing its output in `box`; resolves when finished.
async function ctJob(box, jobId) {
  const pre = h("pre", { class: "mono", style: "white-space:pre-wrap;background:var(--panel-2);padding:10px;border-radius:8px;max-height:260px;overflow:auto" });
  const head = h("div", { class: "muted" }, "Working…");
  box.replaceChildren(head, pre);
  for (;;) {
    const j = await API.get(`/api/containers/jobs/${jobId}`);
    pre.textContent = j.log.join("\n");
    pre.scrollTop = pre.scrollHeight;
    if (j.state !== "running") {
      head.replaceChildren(h("span", { class: "pill " + (j.state === "done" ? "ok" : "bad") }, j.state === "done" ? "Done" : "Failed"), " ", j.title);
      return j.state === "done";
    }
    await new Promise((r) => setTimeout(r, 1200));
  }
}

async function ctOverview(p) {
  let o, cs;
  try { [o, cs] = await Promise.all([API.get("/api/containers/overview"), API.get("/api/containers")]); } catch (e) { return ctError(p, e); }
  p.append(h("h2", {}, "Overview"),
    h("div", { class: "cards" },
      h("div", { class: "card" }, h("div", { class: "muted" }, "Running"), h("div", { style: "font-size:28px;font-weight:600;color:var(--ok)" }, String(o.running))),
      h("div", { class: "card" }, h("div", { class: "muted" }, "Stopped"), h("div", { style: "font-size:28px;font-weight:600" }, String(o.stopped))),
      h("div", { class: "card" }, h("div", { class: "muted" }, "Images"), h("div", { style: "font-size:28px;font-weight:600" }, String(o.images)))),
    h("h3", { class: "pc-group", style: "margin-top:18px" }, "Running containers"),
    h("table", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "CPU"), h("th", {}, "Memory"), h("th", {}, "Open")),
      cs.filter((c) => c.state === "running").map((c) => h("tr", {}, h("td", {}, h("b", {}, c.name)), h("td", {}, c.cpu || "-"), h("td", {}, c.mem || "-"),
        h("td", {}, c.ports.filter((x) => x.proto === "tcp").map((x) => h("a", { href: `http://${location.hostname}:${x.host}`, target: "_blank", style: "margin-right:8px" }, `:${x.host}`)))))),
    h("p", { class: "muted", style: "margin-top:16px" }, `Docker ${o.version} · data in ${o.data_root}`));
}

async function ctContainers(p, timers) {
  const tbl = h("table");
  const draw = async () => {
    let cs;
    try { cs = await API.get("/api/containers"); } catch (e) { return ctError(p, e); }
    tbl.replaceChildren(h("tr", {}, h("th", {}, "Name"), h("th", {}, "Image"), h("th", {}, "Status"), h("th", {}, "Ports"), h("th", {}, "CPU / memory"), h("th", {})),
      ...cs.map((c) => h("tr", {},
        h("td", {}, h("b", {}, c.name), c.project ? h("div", { class: "muted" }, `project ${c.project}`) : null),
        h("td", { class: "mono", style: "font-size:12px" }, c.image),
        h("td", {}, h("span", { class: "pill " + (c.state === "running" ? "ok" : c.state === "restarting" ? "warn" : "") }, c.state), h("div", { class: "muted", style: "font-size:12px" }, c.status)),
        h("td", {}, c.ports.map((x) => h("div", {}, x.proto === "tcp" ? h("a", { href: `http://${location.hostname}:${x.host}`, target: "_blank" }, `${x.host}`) : `${x.host}`, ` → ${x.container}/${x.proto}`))),
        h("td", { style: "font-size:12px" }, c.cpu || "", h("div", { class: "muted" }, c.mem || "")),
        h("td", { style: "white-space:nowrap" },
          c.state === "running"
            ? [h("button", { class: "btn", onclick: () => act(() => API.post(`/api/containers/c/${c.name}/stop`), `${c.name} stopped`).then(draw) }, "Stop"), " ",
               h("button", { class: "btn", onclick: () => act(() => API.post(`/api/containers/c/${c.name}/restart`), `${c.name} restarted`).then(draw) }, "Restart")]
            : h("button", { class: "btn primary", onclick: () => act(() => API.post(`/api/containers/c/${c.name}/start`), `${c.name} started`).then(draw) }, "Start"), " ",
          h("button", { class: "btn", onclick: () => ctDetails(p, c.name) }, "Details"), " ",
          c.project ? null : h("button", { class: "btn danger", onclick: async () => {
            if (!confirm(`Delete container ${c.name}? Data in mapped shared folders is kept.`)) return;
            await act(() => API.del(`/api/containers/c/${c.name}`), "Deleted"); draw();
          } }, "Delete")))));
    if (!cs.length) tbl.replaceChildren(h("tr", {}, h("td", { class: "muted" }, "No containers. Create one from Images (Run), Projects or the Registry.")));
  };
  p.append(h("h2", {}, "Containers"), tbl);
  await draw();
  const t = setInterval(() => p.isConnected && draw(), 8000);
  timers.push(t);
}

async function ctDetails(p, name) {
  const back = () => { p.replaceChildren(); ctContainers(p, []); };
  const [d, l] = await Promise.all([API.get(`/api/containers/c/${name}/details`), API.get(`/api/containers/c/${name}/logs`)]);
  const row = (k, v) => h("tr", {}, h("th", {}, k), h("td", {}, v));
  p.replaceChildren(h("h2", {}, name), h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: back }, "← Back"),
      h("button", { class: "btn", onclick: () => ctDetails(p, name) }, "⟳ Refresh")),
    h("table", {}, row("Image", d.image), row("State", d.state), row("Restart policy", d.restart), row("Network", d.network),
      row("Ports", d.ports.map((x) => `${x.host.join(", ") || "-"} → ${x.container}`).join("; ") || "-"),
      row("Folders", h("div", {}, d.mounts.map((m) => h("div", { class: "mono", style: "font-size:12px" }, `${m.source} → ${m.target}${m.rw ? "" : " (read-only)"}`)))),
      row("Environment", h("div", {}, d.env.map((e) => h("div", { class: "mono", style: "font-size:12px" }, e)))),
      d.command ? row("Command", h("span", { class: "mono" }, d.command)) : null),
    h("h3", { class: "pc-group", style: "margin-top:16px" }, "Log (last 300 lines)"),
    h("pre", { class: "mono", style: "white-space:pre-wrap;background:var(--panel-2);padding:10px;border-radius:8px;max-height:360px;overflow:auto" }, l.log || "(empty)"));
}

async function ctImages(p) {
  let imgs;
  try { imgs = await API.get("/api/containers/images/list"); } catch (e) { return ctError(p, e); }
  const pullName = h("input", { placeholder: "e.g. nginx:latest or ghcr.io/owner/app:1.0", style: "flex:1" });
  const jobBox = h("div");
  const redraw = () => { p.replaceChildren(); ctImages(p); };
  p.append(h("h2", {}, "Images"),
    h("div", { class: "toolbar" }, pullName, h("button", { class: "btn primary", onclick: async () => {
      if (!pullName.value.trim()) return;
      const r = await act(() => API.post("/api/containers/images/pull", { image: pullName.value.trim() }));
      if (await ctJob(jobBox, r.job)) setTimeout(redraw, 800);
    } }, "Pull")), jobBox,
    h("table", {}, h("tr", {}, h("th", {}, "Image"), h("th", {}, "Size"), h("th", {}, "Created"), h("th", {})),
      imgs.map((i) => h("tr", {}, h("td", { class: "mono", style: "font-size:13px" }, i.ref, i.in_use ? h("span", { class: "pill ok", style: "margin-left:6px" }, "in use") : null),
        h("td", {}, i.size), h("td", { class: "muted" }, i.created),
        h("td", { style: "white-space:nowrap" }, h("button", { class: "btn primary", onclick: () => ctRunWizard(p, i.ref) }, "Run"), " ",
          h("button", { class: "btn danger", disabled: i.in_use, onclick: async () => {
            if (!confirm(`Delete image ${i.ref}?`)) return;
            await act(() => API.del(`/api/containers/images/${encodeURIComponent(i.ref)}`), "Image deleted"); redraw();
          } }, "Delete"))))));
}

// Create a container from an image: name, ports, shared-folder mappings, environment, restart policy.
async function ctRunWizard(p, image) {
  const shares = await API.get("/api/shares");
  const base = image.split("/").pop().split(":")[0].replace(/[^A-Za-z0-9_.-]/g, "-");
  const f = { image, name: base, ports: [], volumes: [], env: {}, restart: "unless-stopped", network: "bridge", command: "" };
  const envRows = [];
  const portsBox = h("div", { class: "form" }), volBox = h("div", { class: "form" }), envBox = h("div", { class: "form" });
  const drawPorts = () => portsBox.replaceChildren(...f.ports.map((x, i) => h("div", { style: "display:flex;gap:8px;align-items:center" },
      "NAS port", h("input", { type: "number", value: x.host || "", style: "width:100px", oninput: (e) => (x.host = +e.target.value) }),
      "→ container port", h("input", { type: "number", value: x.container || "", style: "width:100px", oninput: (e) => (x.container = +e.target.value) }),
      h("select", { onchange: (e) => (x.proto = e.target.value) }, ["tcp", "udp"].map((v) => h("option", { value: v, selected: x.proto === v }, v))),
      h("button", { class: "btn", onclick: () => { f.ports.splice(i, 1); drawPorts(); } }, "✕"))),
    h("div", {}, h("button", { class: "btn", onclick: () => { f.ports.push({ host: 0, container: 0, proto: "tcp" }); drawPorts(); } }, "+ Port")));
  const drawVols = () => volBox.replaceChildren(...f.volumes.map((v, i) => h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" },
      h("select", { onchange: (e) => (v.share = e.target.value) }, shares.map((s) => h("option", { value: s.name, selected: v.share === s.name }, s.name))),
      "/", h("input", { value: v.sub, placeholder: "subfolder (optional)", style: "width:150px", oninput: (e) => (v.sub = e.target.value) }),
      "→", h("input", { value: v.container, placeholder: "/config", style: "width:160px", oninput: (e) => (v.container = e.target.value) }),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: v.readonly, onchange: (e) => (v.readonly = e.target.checked) }), "read-only"),
      h("button", { class: "btn", onclick: () => { f.volumes.splice(i, 1); drawVols(); } }, "✕"))),
    shares.length ? h("div", {}, h("button", { class: "btn", onclick: () => { f.volumes.push({ share: shares[0].name, sub: base, container: "", readonly: false }); drawVols(); } }, "+ Folder"))
      : h("div", { class: "muted" }, "Create a shared folder first to give the container storage."));
  const drawEnv = () => envBox.replaceChildren(...envRows.map((r, i) => h("div", { style: "display:flex;gap:8px" },
      h("input", { value: r.k, placeholder: "NAME", style: "width:200px", oninput: (e) => (r.k = e.target.value) }), "=",
      h("input", { value: r.v, placeholder: "value", style: "flex:1", oninput: (e) => (r.v = e.target.value) }),
      h("button", { class: "btn", onclick: () => { envRows.splice(i, 1); drawEnv(); } }, "✕"))),
    h("div", {}, h("button", { class: "btn", onclick: () => { envRows.push({ k: "", v: "" }); drawEnv(); } }, "+ Variable")));
  drawPorts(); drawVols(); drawEnv();
  p.replaceChildren(h("h2", {}, "Run container"), h("div", { class: "muted mono" }, image),
    h("div", { class: "form", style: "max-width:760px;margin-top:10px" },
      h("label", {}, "Container name", h("input", { value: f.name, oninput: (e) => (f.name = e.target.value) })),
      h("h3", { class: "pc-group" }, "Ports"), portsBox,
      h("h3", { class: "pc-group" }, "Folders (inside shared folders - kept when the container is deleted)"), volBox,
      h("h3", { class: "pc-group" }, "Environment variables"), envBox,
      h("details", {}, h("summary", { class: "muted" }, "Advanced"), h("div", { class: "form", style: "margin-top:8px" },
        h("label", {}, "Restart", h("select", { onchange: (e) => (f.restart = e.target.value) },
          [["unless-stopped", "Always, unless I stop it (recommended)"], ["always", "Always"], ["on-failure", "Only if it crashes"], ["no", "Never"]].map(([v, l]) => h("option", { value: v, selected: f.restart === v }, l)))),
        h("label", {}, "Network", h("select", { onchange: (e) => (f.network = e.target.value) },
          [["bridge", "Bridge (own network, ports mapped)"], ["host", "Host (shares the NAS's network; ports not mapped)"]].map(([v, l]) => h("option", { value: v }, l)))),
        h("label", {}, "Command (optional)", h("input", { oninput: (e) => (f.command = e.target.value) })))),
      h("div", { class: "toolbar" },
        h("button", { class: "btn", onclick: () => { p.replaceChildren(); ctImages(p); } }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => {
          f.env = Object.fromEntries(envRows.filter((r) => r.k).map((r) => [r.k, r.v]));
          const body = { ...f, volumes: f.volumes.map((v) => ({ share_path: [v.share, v.sub].filter(Boolean).join("/"), container: v.container, readonly: v.readonly })) };
          await act(() => API.post("/api/containers/run", body), `${f.name} is running`);
          p.replaceChildren(); ctContainers(p, []);
        } }, "Run"))));
}

const CT_TEMPLATE = `services:
  app:
    image: nginx:alpine
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - ./html:/usr/share/nginx/html:ro
`;

async function ctProjects(p) {
  let ps;
  try { ps = await API.get("/api/containers/projects/list"); } catch (e) { return ctError(p, e); }
  const jobBox = h("div");
  const redraw = () => { p.replaceChildren(); ctProjects(p); };
  const doAction = async (name, action) => {
    const r = await act(() => API.post(`/api/containers/projects/${name}/${action}`));
    await ctJob(jobBox, r.job); setTimeout(redraw, 1500);
  };
  p.append(h("h2", {}, "Projects (Docker Compose)"),
    h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: () => ctProjectEditor(p, null) }, "+ Create project")), jobBox,
    ps.length ? h("table", {}, h("tr", {}, h("th", {}, "Project"), h("th", {}, "Status"), h("th", {}, "Folder"), h("th", {})),
      ps.map((x) => h("tr", {}, h("td", {}, h("b", {}, x.name)),
        h("td", {}, h("span", { class: "pill " + (x.status.startsWith("running") ? "ok" : "") }, x.status)),
        h("td", { class: "mono muted", style: "font-size:12px" }, "docker/" + x.name),
        h("td", { style: "white-space:nowrap" },
          x.status.startsWith("running")
            ? h("button", { class: "btn", onclick: () => doAction(x.name, "down") }, "Stop")
            : h("button", { class: "btn primary", onclick: () => doAction(x.name, "up") }, "Start"), " ",
          h("button", { class: "btn", title: "Pull newer images and recreate changed containers", onclick: () => doAction(x.name, "update") }, "Update"), " ",
          h("button", { class: "btn", onclick: () => ctProjectEditor(p, x.name) }, "Edit"), " ",
          h("button", { class: "btn", onclick: async () => {
            const l = await API.get(`/api/containers/projects/${x.name}/logs`);
            jobBox.replaceChildren(h("pre", { class: "mono", style: "white-space:pre-wrap;background:var(--panel-2);padding:10px;border-radius:8px;max-height:300px;overflow:auto" }, l.log || "(no output)"));
          } }, "Logs"), " ",
          h("button", { class: "btn danger", onclick: async () => {
            if (!confirm(`Delete project ${x.name}? Its containers are removed.`)) return;
            const files = confirm(`Also delete the folder docker/${x.name} with its data?\n\nOK = delete the folder too\nCancel = keep the folder`);
            await act(() => API.del(`/api/containers/projects/${x.name}?delete_files=${files}`), "Project deleted"); redraw();
          } }, "Delete")))))
      : h("p", { class: "muted" }, "No projects yet. A project is a compose.yaml describing one or more containers; it is stored in the shared folder “docker”."));
}

async function ctProjectEditor(p, name) {
  const cur = name ? await API.get(`/api/containers/projects/${name}`) : { name: "", yaml: CT_TEMPLATE };
  const nameIn = h("input", { value: cur.name, disabled: !!name, placeholder: "my-app" });
  const yaml = h("textarea", { style: "min-height:320px;font-size:13px", spellcheck: false }, cur.yaml);
  const err = h("div", { class: "err" }), jobBox = h("div");
  p.replaceChildren(h("h2", {}, name ? `Edit ${name}` : "Create project"),
    h("div", { class: "form", style: "max-width:820px" },
      h("label", {}, "Project name (lowercase, used as the folder name)", nameIn),
      h("label", {}, "compose.yaml", yaml),
      h("div", { class: "muted" }, "Relative paths like ./data are created inside docker/<project> on your volume."),
      err,
      h("div", { class: "toolbar" },
        h("button", { class: "btn", onclick: () => { p.replaceChildren(); ctProjects(p); } }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => {
          err.textContent = "";
          try {
            const r = await API.put("/api/containers/projects/save", { name: nameIn.value.trim(), yaml: yaml.value, start: true });
            if (await ctJob(jobBox, r.job)) toast("Project is running");
          } catch (e) { err.textContent = e.message; }
        } }, "Save & start")), jobBox));
}

async function ctRegistry(p) {
  const q = h("input", { placeholder: "Search Docker Hub (e.g. jellyfin, home-assistant, pihole)", style: "flex:1" });
  const results = h("div"), jobBox = h("div");
  const search = async () => {
    results.replaceChildren(h("p", { class: "muted" }, "Searching…"));
    try {
      const rs = await API.get(`/api/containers/registry/search?q=${encodeURIComponent(q.value)}`);
      const tag = (r) => h("input", { value: "latest", style: "width:90px", "data-r": r.name });
      results.replaceChildren(h("table", {}, rs.map((r) => {
        const t = tag(r);
        return h("tr", {}, h("td", {}, h("b", {}, r.name), r.official ? h("span", { class: "pill ok", style: "margin-left:6px" }, "official") : null,
            h("div", { class: "muted", style: "font-size:12px" }, r.description)),
          h("td", { class: "muted", style: "white-space:nowrap" }, `★ ${r.stars}`),
          h("td", { style: "white-space:nowrap" }, t, " ", h("button", { class: "btn primary", onclick: async () => {
            const r2 = await act(() => API.post("/api/containers/images/pull", { image: `${r.name}:${t.value || "latest"}` }));
            if (await ctJob(jobBox, r2.job)) toast("Downloaded - find it under Images → Run");
          } }, "Download")));
      })));
    } catch (e) { results.replaceChildren(h("div", { class: "note warn" }, e.message)); }
  };
  q.addEventListener("keydown", (e) => e.key === "Enter" && search());
  p.append(h("h2", {}, "Registry"), h("div", { class: "toolbar" }, q, h("button", { class: "btn primary", onclick: search }, "Search")), jobBox, results);
}

async function ctSettings(p) {
  let o, pools;
  try { [o, pools] = await Promise.all([API.get("/api/containers/overview"), API.get("/api/storage/pools")]); } catch (e) { return ctError(p, e); }
  const onPool = pools.some((x) => o.data_root.startsWith(x.mount + "/"));
  const sel = h("select", {}, pools.map((x) => h("option", { value: x.id }, `${x.label} (${x.mount})`)));
  p.append(h("h2", {}, "Settings"),
    h("table", {}, h("tr", {}, h("th", {}, "Docker version"), h("td", {}, o.version)), h("tr", {}, h("th", {}, "Data stored in"), h("td", { class: "mono" }, o.data_root))),
    onPool ? h("div", { class: "note", style: "margin-top:12px" }, "✓ Images and container data are on a storage pool.")
      : h("div", { class: "form", style: "margin-top:12px" },
          h("div", { class: "note warn" }, "Images and container data are on the boot disk, which is usually small and has no RAID. Move them to a storage pool - this is possible while Docker has no containers or images yet."),
          pools.length ? h("div", { class: "toolbar" }, sel, h("button", { class: "btn primary", onclick: async () => {
            await act(() => API.post("/api/containers/settings/data-root", { pool_id: +sel.value }), "Docker now stores its data on the pool");
            p.replaceChildren(); ctSettings(p);
          } }, "Move Docker data")) : h("div", { class: "muted" }, "Create a storage pool first.")));
}
