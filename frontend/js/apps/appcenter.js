// Package Center: SPK packages from configurable sources (SynoCommunity etc.), manual .spk
// install, install wizards, plus one-click Docker containers.
WM.register("appcenter", {
  name: "Package Center", icon: "📦", color: "#d9902a", w: 960, h: 620,
  open(body) {
    const st = { view: "all", q: "", catalog: null };
    const side = h("nav", { class: "side" });
    const pane = h("div", { class: "pane" });
    const search = h("input", { placeholder: "Search", style: "flex:1", oninput: (e) => { st.q = e.target.value.toLowerCase(); render(); } });
    const top = h("div", { class: "pc-top" },
      h("button", { class: "btn", title: "Refresh", onclick: () => { st.catalog = null; render(); } }, "⟳"), search,
      h("button", { class: "btn", onclick: manualInstall }, "Manual Install"),
      h("button", { class: "btn", onclick: settings }, "Settings"));
    const views = { installed: "Installed", all: "All Packages", community: "Community", containers: "Containers" };
    const nav = () => side.replaceChildren(...Object.entries(views).map(([k, v]) =>
      h("button", { class: st.view === k ? "on" : "", onclick: () => { st.view = k; nav(); render(); } }, v)));
    body.append(side, h("div", { style: "flex:1;display:flex;flex-direction:column;min-width:0" }, top, pane));
    nav();

    async function render() {
      pane.replaceChildren(h("p", { class: "muted" }, "Loading…"));
      if (st.view === "containers") return renderContainers();
      if (st.view === "installed") return renderInstalled();
      if (!st.catalog) st.catalog = await API.get("/api/packages/catalog");
      const { packages, errors } = st.catalog;
      const match = (p) => !st.q || (p.dname + " " + p.desc + " " + p.package).toLowerCase().includes(st.q);
      const groups = {};
      for (const p of packages.filter(match)) {
        if (st.view === "community" && !p.compatible) continue;
        (groups[p.source] ||= []).push(p);
      }
      pane.replaceChildren(
        ...errors.map((e) => h("div", { class: "note warn" }, `Source "${e.source}" unavailable: ${e.error}`)),
        ...Object.entries(groups).map(([src, list]) => h("div", {},
          h("h3", { class: "pc-group" }, src, h("span", { class: "muted" }, ` · ${list.length}`)),
          h("div", { class: "pc-grid" }, list.map(pkgTile)))));
      if (!Object.keys(groups).length) pane.append(h("p", { class: "muted" }, "No packages match."));
    }

    function pkgTile(p) {
      let action;
      if (p.installed_version) action = h("span", { class: "pill ok" }, "Installed");
      else if (!p.compatible) action = h("span", { class: "pill", title: p.why_not }, "Not compatible");
      else action = h("button", { class: "pc-install", onclick: () => installFlow(() => API.post("/api/packages/prepare", { source: p.source, package: p.package })) }, "Install");
      return h("div", { class: "pc-tile", title: p.desc },
        p.thumbnail ? h("img", { src: p.thumbnail, loading: "lazy", alt: "" }) : h("div", { class: "pc-noimg" }, "📦"),
        h("div", { style: "min-width:0" }, h("b", {}, p.dname), h("div", { class: "muted pc-desc" }, p.why_not || p.desc),
          h("div", { class: "muted", style: "font-size:11px" }, `v${p.version}`), action));
    }

    async function renderInstalled() {
      const list = await API.get("/api/packages/installed");
      pane.replaceChildren(h("h3", { class: "pc-group" }, "Installed"),
        list.length ? h("div", { class: "pc-grid" }, list.map((p) => h("div", { class: "pc-tile" },
          h("img", { src: `/api/packages/installed/${p.package}/icon`, alt: "" }),
          h("div", {}, h("b", {}, p.dname), h("div", { class: "muted" }, `v${p.version} · `,
            h("span", { class: "pill " + (p.running ? "ok" : "") }, p.startable ? (p.running ? "Running" : "Stopped") : "Tool")),
            h("div", { class: "toolbar", style: "margin:6px 0 0" },
              p.url && p.running ? h("a", { class: "btn primary", target: "_blank", href: `${p.url.protocol}://${location.hostname}:${p.url.port}${p.url.path}` }, "Open") : null,
              p.startable ? h("button", { class: "btn", onclick: async () => { await act(() => API.post(`/api/packages/installed/${p.package}/${p.running ? "stop" : "start"}`)); renderInstalled(); } }, p.running ? "Stop" : "Run") : null,
              h("button", { class: "btn danger", onclick: async () => {
                const keep = confirm(`Uninstall ${p.dname}?\n\nOK = keep its settings/data for a later reinstall\nCancel = choose again`);
                if (!keep && !confirm(`Erase ALL data of ${p.dname}? This cannot be undone.`)) return;
                await act(() => API.del(`/api/packages/installed/${p.package}?keep_data=${keep}`), `${p.dname} uninstalled`);
                st.catalog = null; renderInstalled();
              } }, "Uninstall"))))))
          : h("p", { class: "muted" }, "Nothing installed yet."));
    }

    async function installFlow(prepareFn) {
      pane.replaceChildren(h("p", { class: "muted" }, "Downloading and checking package…"));
      let s;
      try { s = await prepareFn(); } catch (e) { toast("Error: " + e.message); return render(); }
      const answers = {};
      const form = h("div", { class: "form" },
        h("h2", {}, `Install ${s.info.displayname || s.info.package} ${s.info.version}`),
        h("div", { class: "muted" }, s.info.description || ""),
        ...s.problems.map((p) => h("div", { class: "note warn" }, p)));
      // DSM-style wizard steps from WIZARD_UIFILES/install_uifile
      for (const step of s.wizard || []) {
        form.append(h("h3", { class: "pc-group" }, step.step_title || ""));
        for (const item of step.items || []) {
          if (item.desc) form.append(h("div", { class: "muted", html: item.desc }));
          for (const sub of item.subitems || []) {
            if (item.type === "textfield" || item.type === "password" || item.type === "combobox") {
              answers[sub.key] = sub.defaultValue ?? "";
              form.append(h("label", {}, h("span", { html: sub.desc || sub.key }),
                h("input", { type: item.type === "password" ? "password" : "text", value: sub.defaultValue ?? "", oninput: (e) => (answers[sub.key] = e.target.value) })));
            } else if (item.type === "singleselect" || item.type === "multiselect") {
              answers[sub.key] = !!sub.defaultValue;
              form.append(h("label", { class: "check" }, h("input", {
                type: item.type === "singleselect" ? "radio" : "checkbox", name: step.step_title + item.desc, checked: !!sub.defaultValue,
                onchange: (e) => { if (item.type === "singleselect") item.subitems.forEach((x) => (answers[x.key] = false)); answers[sub.key] = e.target.checked; } }),
                h("span", { html: sub.desc || sub.key })));
            }
          }
        }
      }
      form.append(h("div", { class: "toolbar" },
        h("button", { class: "btn", onclick: render }, "Cancel"),
        h("button", { class: "btn primary", disabled: s.problems.length > 0, onclick: async () => {
          pane.replaceChildren(h("p", { class: "muted" }, "Installing…"));
          try { await act(() => API.post("/api/packages/install", { staging: s.staging, wizard: answers }), "Installed"); st.catalog = null; st.view = "installed"; nav(); }
          finally { render(); }
        } }, "Install")));
      pane.replaceChildren(form);
    }

    function manualInstall() {
      const file = h("input", { type: "file", accept: ".spk" });
      pane.replaceChildren(h("div", { class: "form" }, h("h2", {}, "Manual Install"),
        h("div", { class: "muted" }, "Upload a Synology-format .spk package built for x86-64 (or noarch)."), file,
        h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: render }, "Cancel"),
          h("button", { class: "btn primary", onclick: () => {
            if (!file.files[0]) return toast("Choose a .spk file first");
            installFlow(async () => {
              const r = await fetch("/api/packages/upload", { method: "POST", headers: { Authorization: `Bearer ${API.token}` }, body: file.files[0] });
              const d = await r.json(); if (!r.ok) throw new Error(d.detail); return d;
            });
          } }, "Next"))));
    }

    async function settings() {
      let sources = await API.get("/api/packages/sources");
      let sel = -1;
      const tbl = h("table", { class: "pc-src" });
      const draw = () => tbl.replaceChildren(h("tr", {}, h("th", {}, "Name"), h("th", {}, "Location")),
        ...sources.map((s, i) => h("tr", { class: i === sel ? "sel" : "", onclick: () => { sel = i; draw(); } }, h("td", {}, s.name), h("td", {}, s.url))));
      const edit = (i) => {
        const cur = sources[i] || { name: "", url: "https://" };
        const name = prompt("Name", cur.name); if (!name) return;
        const url = prompt("Location", cur.url); if (!url) return;
        if (i >= 0) sources[i] = { name, url }; else sources.push({ name, url });
        draw();
      };
      draw();
      pane.replaceChildren(h("div", { class: "form", style: "max-width:720px" }, h("h2", {}, "Settings · Package Sources"),
        h("div", { class: "toolbar" },
          h("button", { class: "btn", onclick: () => edit(-1) }, "Add"),
          h("button", { class: "btn", onclick: () => sel >= 0 && edit(sel) }, "Edit"),
          h("button", { class: "btn", onclick: () => { if (sel >= 0) { sources.splice(sel, 1); sel = -1; draw(); } } }, "Delete")),
        tbl,
        h("div", { class: "toolbar", style: "justify-content:flex-end" },
          h("button", { class: "btn", onclick: render }, "Cancel"),
          h("button", { class: "btn primary", onclick: async () => { await act(() => API.put("/api/packages/sources", sources), "Sources saved"); st.catalog = null; render(); } }, "OK"))));
    }

    async function renderContainers() {
      const apps = await API.get("/api/apps");
      pane.replaceChildren(h("h3", { class: "pc-group" }, "Containers (Docker)"), h("div", { class: "pc-grid" }, apps.map((a) => h("div", { class: "pc-tile" },
        h("div", { class: "pc-noimg" }, a.icon),
        h("div", {}, h("b", {}, a.name), h("div", { class: "muted pc-desc" }, a.description),
          a.installed
            ? h("div", { class: "toolbar", style: "margin:6px 0 0" },
                h("a", { class: "btn primary", href: `${location.protocol}//${location.hostname}:${a.port}`, target: "_blank" }, "Open"),
                h("button", { class: "btn danger", onclick: async () => { await act(() => API.post(`/api/apps/${a.id}/uninstall`), `${a.name} removed`); renderContainers(); } }, "Uninstall"))
            : h("button", { class: "pc-install", onclick: async () => { await act(() => API.post(`/api/apps/${a.id}/install`), `${a.name} installed`); renderContainers(); } }, "Install"))))));
    }

    render();
  },
});
