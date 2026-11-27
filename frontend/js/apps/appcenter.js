const appInitial = (name) => (name || "?").replace(/[^A-Za-z0-9]/g, "").slice(0, 2) || "•";

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
    const views = { installed: "Installed", all: "All Packages", alternatives: "Synology alternatives", community: "Community", containers: "Containers" };
    const nav = () => side.replaceChildren(...Object.entries(views).map(([k, v]) =>
      h("button", { class: st.view === k ? "on" : "", onclick: () => { st.view = k; nav(); render(); } }, v,
        k === "installed" && st.updates?.length ? h("span", { class: "pc-badge" }, String(st.updates.length)) : null)));
    const refreshUpdates = () => API.get("/api/packages/updates").then((u) => { st.updates = u; nav(); }).catch(() => {});
    refreshUpdates();
    body.append(side, h("div", { style: "flex:1;display:flex;flex-direction:column;min-width:0" }, top, pane));
    nav();

    async function render() {
      pane.replaceChildren(h("p", { class: "muted" }, "Loading…"));
      if (st.view === "containers") return renderContainers();
      if (st.view === "alternatives") return renderAlternatives();
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
      if (p.update_available) action = h("button", { class: "pc-install", onclick: () => installFlow(() => API.post("/api/packages/prepare", { source: p.source, package: p.package })) }, `Update (${p.installed_version} → ${p.version})`);
      else if (p.installed_version) action = h("span", { class: "pill ok" }, "Installed");
      else if (!p.compatible) action = h("span", { class: "pill", title: p.why_not }, "Not compatible");
      else action = h("button", { class: "pc-install", onclick: () => installFlow(() => API.post("/api/packages/prepare", { source: p.source, package: p.package })) }, "Install");
      return h("div", { class: "pc-tile", title: p.desc },
        p.thumbnail ? h("img", { src: p.thumbnail, loading: "lazy", alt: "" }) : h("div", { class: "pc-noimg" }, "📦"),
        h("div", { style: "min-width:0" }, h("b", {}, p.dname), h("div", { class: "muted pc-desc" }, p.why_not || p.desc),
          !p.why_not && p.provided?.length ? h("div", { class: "muted", style: "font-size:11px", title: t("Synology's own packages are provided by Cynthology (Debian's PHP, Apache, Perl... and Web Station) and installed automatically") },
            "✓ " + t("Uses") + " " + p.provided.join(", ")) : null,
          h("div", { class: "muted", style: "font-size:11px" }, `v${p.version}`), action));
    }

    async function renderInstalled() {
      const [list, ups] = await Promise.all([API.get("/api/packages/installed"), API.get("/api/packages/updates")]);
      st.updates = ups; nav();
      const upd = Object.fromEntries(ups.map((u) => [u.package, u]));
      pane.replaceChildren(h("div", { style: "display:flex;align-items:center;gap:12px" }, h("h3", { class: "pc-group" }, "Installed"),
          ups.length ? h("button", { class: "btn primary", onclick: async () => {
            pane.replaceChildren(h("p", { class: "muted" }, `Updating ${ups.length} package(s)…`));
            const res = await API.post("/api/packages/update-all").catch((e) => [{ ok: false, error: e.message }]);
            const bad = res.filter((r) => !r.ok);
            toast(bad.length ? `${bad.length} update(s) failed: ` + bad.map((b) => `${b.package}: ${b.error}`).join("; ") : `Updated ${res.length} package(s)`);
            st.catalog = null; renderInstalled();
          } }, `Update all (${ups.length})`) : null),
        list.length ? h("div", { class: "pc-grid" }, list.map((p) => h("div", { class: "pc-tile" },
          pkgIcon(p),
          h("div", {}, h("b", {}, p.dname), h("div", { class: "muted" }, `v${p.version} · `,
            h("span", { class: "pill " + (p.running ? "ok" : "") }, p.standin ? t("Built-in") : p.startable ? (p.running ? "Running" : "Stopped") : "Tool")),
            p.standin ? h("div", { class: "muted", style: "font-size:12px" },
              p.used_by && p.used_by.length ? `${t("Used by")} ${p.used_by.join(", ")}` : t("Not used by any package - you can remove it")) : null,
            upd[p.package] ? h("div", { class: "muted", style: "font-size:12px" }, `Update available: ${upd[p.package].available}`) : null,
            h("div", { class: "toolbar", style: "margin:6px 0 0" },
              upd[p.package] ? h("button", { class: "btn primary", onclick: () => installFlow(() => API.post("/api/packages/prepare", { source: upd[p.package].source, package: p.package })) }, "Update") : null,
              p.url && p.running ? h("a", { class: "btn primary", target: "_blank", href: `${p.url.protocol}://${location.hostname}:${p.url.port}${p.url.path}` }, "Open") : null,
              p.startable ? h("button", { class: "btn", onclick: async () => { await act(() => API.post(`/api/packages/installed/${p.package}/${p.running ? "stop" : "start"}`)); renderInstalled(); } }, p.running ? "Stop" : "Run") : null,
              p.standin && p.used_by && p.used_by.length ? null : h("button", { class: "btn danger", onclick: async () => {
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
        h("h2", {}, s.mode === "upgrade" ? `Update ${s.info.displayname || s.info.package}: ${s.installed_version} → ${s.info.version}`
          : `Install ${s.info.displayname || s.info.package} ${s.info.version}`),
        h("div", { class: "muted" }, s.mode === "upgrade" ? (s.info.changelog || "Settings and data are kept.") : s.info.description || ""),
        s.dependencies?.length ? h("div", { class: "note" }, "Also installs required package(s): ",
          s.dependencies.map((d) => `${d.package} ${d.version}`).join(", ")) : null,
        ...s.problems.map((p) => h("div", { class: "note warn" }, p)));
      // DSM-style wizard steps from WIZARD_UIFILES/install_uifile
      for (const step of s.wizard || []) {
        form.append(h("h3", { class: "pc-group" }, step.step_title || ""));
        for (const item of step.items || []) {
          if (item.desc) form.append(h("div", { class: "muted" }, wizText(item.desc)));
          for (const sub of item.subitems || []) {
            if (item.type === "textfield" || item.type === "password" || item.type === "combobox") {
              answers[sub.key] = sub.defaultValue ?? "";
              form.append(h("label", {}, h("span", {}, wizText(sub.desc || sub.key)),
                h("input", { type: item.type === "password" ? "password" : "text", value: sub.defaultValue ?? "", oninput: (e) => (answers[sub.key] = e.target.value) })));
            } else if (item.type === "singleselect" || item.type === "multiselect") {
              answers[sub.key] = !!sub.defaultValue;
              form.append(h("label", { class: "check" }, h("input", {
                type: item.type === "singleselect" ? "radio" : "checkbox", name: step.step_title + item.desc, checked: !!sub.defaultValue,
                onchange: (e) => { if (item.type === "singleselect") item.subitems.forEach((x) => (answers[x.key] = false)); answers[sub.key] = e.target.checked; } }),
                h("span", {}, wizText(sub.desc || sub.key))));
            }
          }
        }
      }
      form.append(h("div", { class: "toolbar" },
        h("button", { class: "btn", onclick: render }, "Cancel"),
        h("button", { class: "btn primary", disabled: s.problems.length > 0, onclick: async () => {
          pane.replaceChildren(h("p", { class: "muted" }, s.mode === "upgrade" ? "Updating…" : "Installing…"));
          try { await act(() => API.post("/api/packages/install", { staging: s.staging, wizard: answers }), s.mode === "upgrade" ? "Updated" : "Installed"); refreshUpdates(); st.catalog = null; st.view = "installed"; nav(); }
          finally { render(); }
        } }, s.mode === "upgrade" ? "Update" : "Install")));
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
      const auto = await API.get("/api/packages/auto-update").catch(() => ({ enabled: false }));
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
      pane.replaceChildren(h("div", { class: "form", style: "max-width:720px" }, h("h2", {}, "Settings"),
        h("h3", { class: "pc-group" }, "Auto-update"),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: auto.enabled, onchange: (e) =>
          act(() => API.put("/api/packages/auto-update", { enabled: e.target.checked }), e.target.checked ? "Packages will update automatically every night" : "Automatic updates off") }),
          "Install package updates automatically (checked nightly; failures are emailed)"),
        h("h3", { class: "pc-group" }, "Package Sources"),
        h("div", { class: "toolbar" },
          h("button", { class: "btn", onclick: () => edit(-1) }, "Add"),
          h("button", { class: "btn", onclick: () => sel >= 0 && edit(sel) }, "Edit"),
          h("button", { class: "btn", onclick: () => { if (sel >= 0) { sources.splice(sel, 1); sel = -1; draw(); } } }, "Delete")),
        tbl,
        h("div", { class: "toolbar", style: "justify-content:flex-end" },
          h("button", { class: "btn", onclick: render }, "Cancel"),
          h("button", { class: "btn primary", onclick: async () => { await act(() => API.put("/api/packages/sources", sources), "Sources saved"); st.catalog = null; render(); } }, "OK"))));
    }

    // ---------- one-click container apps ----------
    const appUrl = (a) => a.port ? `${a.scheme || "http"}://${location.hostname}:${a.port}${a.path || "/"}` : null;

    function containerActions(a, back) {
      if (!a.installed && a.blocked) return [h("span", { class: "pill warn", title: a.blocked }, "Needs newer Docker")];
      if (!a.installed) return [h("button", { class: "pc-install", onclick: () => containerInstall(a, back) }, "Install")];
      return [
        a.running && appUrl(a) ? h("a", { class: "btn primary", href: appUrl(a), target: "_blank" }, "Open") : h("span", { class: "pill" + (a.running ? " ok" : "") }, a.running ? "Running" : (a.status || "Stopped")),
        a.notes ? h("button", { class: "btn", onclick: () => alert(`${a.name}\n\n${a.notes}`) }, "Info") : null,
        h("button", { class: "btn danger", onclick: async () => {
          if (a.holds_data) {
            if (!confirm(`Stop ${a.name}?\n\nIts data lives inside its container and is kept; start it again in Container Manager → Projects → ${a.project}.`)) return;
            await act(() => API.post(`/api/apps/${a.id}/uninstall`), `${a.name} stopped`); return back();
          }
          if (!confirm(`Uninstall ${a.name}?\n\nIts data folder (docker/${a.project}) is kept for a later reinstall.`)) return;
          const erase = confirm(`Also ERASE ${a.name}'s data (docker/${a.project})? This cannot be undone.\n\nOK = erase, Cancel = keep`);
          await act(() => API.post(`/api/apps/${a.id}/uninstall?delete_data=${erase}`), `${a.name} uninstalled`); back();
        } }, a.holds_data ? "Stop" : "Uninstall")];
    }

    function containerInstall(a, back) {
      const ans = Object.fromEntries((a.fields || []).map((f) => [f.key, f.default || ""]));
      const go = async () => {
        const box = h("div");
        pane.replaceChildren(h("h2", {}, `Installing ${a.name}`), h("p", { class: "muted" }, "Preparing…"));
        let r;
        try { r = await API.post(`/api/apps/${a.id}/install`, { fields: ans }); } catch (e) { toast("Error: " + e.message); return back(); }
        pane.replaceChildren(h("div", { class: "form", style: "max-width:760px" }, h("h2", {}, `Installing ${a.name}`),
          r.notes ? h("div", { class: "note" }, h("b", {}, "Keep this: "), r.notes) : null, box,
          h("p", { class: "muted" }, `It runs as project “${"app-" + a.id}” in Container Manager, with its data in the “docker” shared folder.`),
          h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: back }, "Done"))));
        if (r.job) { if (await ctJob(box, r.job)) toast(`${a.name} is installed`); }
        else box.append(h("span", { class: "pill ok" }, "Done"));
      };
      if (!a.fields?.length && !a.holds_data) return go();
      pane.replaceChildren(h("div", { class: "form", style: "max-width:640px" }, h("h2", {}, `Install ${a.name}`),
        h("div", { class: "muted" }, a.description),
        a.holds_data ? h("div", { class: "note warn" }, "This app keeps its data (websites, mail, databases) inside its container, not in the docker shared folder. ",
          "Snapshots and backups of shared folders don't include it; use the app's own backup. Update it from inside the app.") : null,
        ...(a.fields || []).map((f) => h("label", {}, h("span", {}, f.label), h("input", { value: ans[f.key], oninput: (e) => (ans[f.key] = e.target.value.trim()) }))),
        h("div", { class: "toolbar", style: "justify-content:flex-end" }, h("button", { class: "btn", onclick: back }, "Cancel"),
          h("button", { class: "btn primary", onclick: go }, "Install"))));
    }

    async function renderContainers() {
      const apps = await API.get("/api/apps");
      pane.replaceChildren(h("h3", { class: "pc-group" }, "Containers (Docker)"),
        h("div", { class: "muted", style: "margin:-4px 0 12px" }, "One-click apps. Each becomes a Container Manager project with its data in the “docker” shared folder."),
        h("div", { class: "pc-grid" }, apps.map((a) => h("div", { class: "pc-tile" }, h("div", { class: "pc-noimg app-initial" }, appInitial(a.name)),
          h("div", {}, h("b", {}, a.name), h("div", { class: "muted pc-desc" }, a.blocked || a.description),
            h("div", { class: "toolbar", style: "margin:6px 0 0" }, ...containerActions(a, renderContainers)))))));
    }

    // ---------- Synology app alternatives ----------
    async function renderAlternatives() {
      const alts = await API.get("/api/apps/alternatives");
      const match = (a) => !st.q || JSON.stringify([a.synology, a.options.map((o) => [o.name, o.description])]).toLowerCase().includes(st.q);
      const kindLabel = { spk: "Package", container: "Container", builtin: "Built in" };
      const option = (o) => {
        let acts;
        if (o.kind === "builtin") acts = [h("button", { class: "btn primary", onclick: () => WM.open(o.open) }, "Open")];
        else if (o.kind === "container") acts = containerActions(o, renderAlternatives);
        else if (o.installed) acts = [o.url && o.running ? h("a", { class: "btn primary", target: "_blank", href: `${o.url.protocol}://${location.hostname}:${o.url.port}${o.url.path}` }, "Open")
          : h("span", { class: "pill ok" }, "Installed")];
        else if (o.available) acts = [h("button", { class: "pc-install", onclick: () => installFlow(() => API.post("/api/packages/prepare", { source: o.source, package: o.package })) }, "Install")];
        else if (o.why_not) acts = [h("span", { class: "pill", title: o.why_not }, "Not compatible")];
        else acts = [h("span", { class: "pill", title: "Add a package source that has it (Settings → Package Sources)" }, "Not in your package sources")];
        const img = o.kind === "spk" && o.thumbnail ? h("img", { src: o.thumbnail, loading: "lazy", alt: "" })
          : o.kind === "builtin" ? appTile(o.open, 48) : h("div", { class: "pc-noimg app-initial alt-img" }, appInitial(o.name));
        return h("div", { class: "alt-opt" }, img,
          h("div", { style: "min-width:0" }, h("div", {}, h("b", {}, o.name), " ", h("span", { class: "pill alt-kind" }, kindLabel[o.kind])),
            h("div", { class: "muted pc-desc" }, (o.kind === "spk" && !o.installed && o.why_not) ? o.why_not.replace("DSM-only", "Synology-only") : o.blocked || o.description || ""),
            h("div", { class: "toolbar", style: "margin:6px 0 0" }, ...acts)));
      };
      const groups = {};
      for (const a of alts.filter(match)) (groups[a.category] ||= []).push(a);
      pane.replaceChildren(
        h("div", { class: "note" }, "Open-source replacements for the apps that come with Synology DSM, ready to install here. ",
          "Packages come from your package sources, containers run in Container Manager, and some are built into Cynthology. ",
          h("span", { class: "muted" }, "Cynthology is not affiliated with Synology.")),
        ...Object.entries(groups).map(([cat, list]) => h("div", {}, h("h3", { class: "pc-group" }, cat),
          h("div", { class: "alt-list" }, list.map((a) => h("div", { class: "alt-row" },
            h("div", { class: "alt-syn" }, h("div", { class: "muted", style: "font-size:11px;text-transform:uppercase;letter-spacing:.05em" }, "Instead of"), h("b", {}, a.synology)),
            h("div", { class: "alt-opts" }, a.options.map(option))))))));
      if (!Object.keys(groups).length) pane.append(h("p", { class: "muted" }, "Nothing matches."));
    }

    render();
  },
});

// Package wizard texts come from third-party feeds: show them as plain text (line breaks kept), never as HTML.
function wizText(html) {
  const doc = new DOMParser().parseFromString(String(html).replace(/<br\s*\/?>/gi, "\n"), "text/html");
  return h("span", { style: "white-space:pre-line" }, doc.body.textContent || "");
}

// A package's own icon; packages without one get a neutral tile with their initial.
function pkgIcon(p) {
  const img = h("img", { src: `/api/packages/installed/${encodeURIComponent(p.package)}/icon`, alt: "" });
  img.onerror = () => img.replaceWith(h("div", { class: "pc-noimg app-initial" }, (p.dname || p.package).trim().charAt(0).toUpperCase()));
  return img;
}
