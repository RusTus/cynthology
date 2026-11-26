// Backup: encrypted, versioned backups of shared folders (restic) to USB, another NAS (SFTP) or the cloud (S3).
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DEST_LABEL = { local: "USB drive / local folder", sftp: "Another NAS or server (SFTP)", s3: "Cloud storage (S3-compatible)", rest: "restic REST server" };

WM.register("backup", {
  name: "Backup", icon: "💾", color: "#2e9d5b", w: 980, h: 640,
  open(body, win) {
    const pane = h("div", { class: "pane" });
    body.append(pane);
    let timer = null;
    win.cleanup.push(() => clearTimeout(timer));

    const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "-");
    const destText = (d) => d.type === "local" ? (d.uuid ? `USB drive ${d.uuid.slice(0, 8)}… / ${d.path || ""}` : d.path)
      : d.type === "sftp" ? `${d.user}@${d.host}:${d.path}` : d.type === "s3" ? `${d.endpoint || "AWS"} / ${d.bucket}/${d.path}` : d.url;

    async function list() {
      clearTimeout(timer);
      const tasks = await API.get("/api/backup");
      const running = tasks.some((t) => t.status.state === "running" || t.status.check?.state === "running");
      pane.replaceChildren(h("h2", {}, "Backup"),
        h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: () => editor() }, "+ Create backup task")),
        tasks.length ? h("div", { class: "cards", style: "grid-template-columns:repeat(auto-fill,minmax(360px,1fr))" }, tasks.map(card))
          : h("div", { class: "note" }, "No backup tasks yet. A NAS with RAID still needs a backup: RAID protects against a failed drive, ",
            "not against deletion, ransomware, fire or theft. Create a task to back up to a USB drive, another NAS or the cloud."));
      if (running) timer = setTimeout(list, 3000);
    }

    function card(t) {
      const s = t.status;
      const pill = { success: "ok", warning: "warn", failed: "bad", running: "warn", never: "" }[s.state] ?? "";
      const label = { success: "Succeeded", warning: "Finished with warnings", failed: "Failed", running: "Running", never: "Never run" }[s.state] || s.state;
      const sum = s.summary;
      return h("div", { class: "card" },
        h("div", {}, h("b", {}, t.name), " ", h("span", { class: "pill " + pill }, label)),
        h("div", { class: "muted" }, `${t.sources.join(", ")} → ${DEST_LABEL[t.dest.type]}`),
        h("div", { class: "muted mono" }, destText(t.dest)),
        s.state === "running" && s.progress ? h("div", {}, h("div", { class: "bar" }, h("div", { style: `width:${s.progress.percent}%` })),
          h("div", { class: "muted" }, `${s.progress.percent}% · ${s.progress.files} files · ${fmtBytes(s.progress.bytes)} of ${fmtBytes(s.progress.total_bytes)}`)) : null,
        s.error ? h("div", { class: "err" }, s.error) : null,
        h("div", { class: "muted" }, `Last run: ${fmtTime(s.finished || s.started)}` + (sum ? ` · ${sum.files_new ?? 0} new, ${sum.files_changed ?? 0} changed, ${fmtBytes(sum.data_added || 0)} added in ${sum.duration}s` : "")),
        h("div", { class: "muted" }, "Next run: " + (t.next_run ? fmtTime(t.next_run) : "not scheduled")),
        s.check ? h("div", { class: "muted" }, `Integrity check: ${s.check.state}` + (s.check.finished ? ` (${fmtTime(s.check.finished)})` : "")) : null,
        h("div", { class: "toolbar", style: "margin:4px 0 0" },
          h("button", { class: "btn primary", disabled: s.state === "running", onclick: async () => { await act(() => API.post(`/api/backup/${t.id}/run`), "Backup started"); list(); } }, "Back up now"),
          h("button", { class: "btn", onclick: () => restoreView(t) }, "Restore"),
          h("button", { class: "btn", onclick: () => editor(t) }, "Edit"),
          h("button", { class: "btn", title: "More", onclick: (e) => more(e, t) }, "⋯")));
    }

    function more(e, t) {
      const menu = h("div", { class: "menu menu-right", style: `top:${e.clientY + 8}px;left:${Math.min(e.clientX, innerWidth - 220)}px;right:auto` },
        h("button", { onclick: async () => { await act(() => API.post(`/api/backup/${t.id}/check`), "Integrity check started"); list(); } }, "Check integrity"),
        h("button", { onclick: () => showLog(t) }, "View log"),
        h("button", { onclick: async () => { const r = await API.get(`/api/backup/${t.id}/password`); prompt("Encryption password - keep a copy somewhere safe (not on this NAS):", r.password); } }, "Show encryption password"),
        h("button", { style: "color:var(--bad)", onclick: async () => {
          if (!confirm(`Delete the task "${t.name}"? The backup data at the destination is NOT deleted.`)) return;
          await act(() => API.del(`/api/backup/${t.id}`), "Task deleted"); list();
        } }, "Delete task"));
      document.body.append(menu);
      setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }));
    }

    async function showLog(t) {
      const r = await API.get(`/api/backup/${t.id}/log`);
      pane.replaceChildren(h("h2", {}, `Log - ${t.name}`), h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: list }, "← Back")),
        h("pre", { class: "mono", style: "white-space:pre-wrap;background:var(--panel-2);padding:12px;border-radius:8px" }, r.log || "(empty)"));
    }

    // ---------- create / edit ----------
    async function editor(t) {
      const [shares, usb] = await Promise.all([API.get("/api/shares"), API.get("/api/backup/drives/usb")]);
      const f = t ? JSON.parse(JSON.stringify(t)) : {
        name: "", sources: shares.map((s) => s.name), exclude: [],
        dest: { type: "local", path: "", uuid: "", host: "", port: 22, user: "", endpoint: "", bucket: "", region: "", access_key: "", secret_key: "", url: "" },
        schedule: { enabled: true, time: "02:00", days: [0, 1, 2, 3, 4, 5, 6] }, retention: { daily: 7, weekly: 4, monthly: 12 },
      };
      if (t) f.dest.secret_key = null;
      f.password = t ? null : "";
      const destBox = h("div", { class: "form" });
      const testOut = h("div");
      const field = (label, obj, key, attrs = {}) => h("label", {}, label, h("input", { value: obj[key] ?? "", oninput: (e) => (obj[key] = attrs.type === "number" ? +e.target.value : e.target.value), ...attrs }));

      async function drawDest() {
        const d = f.dest;
        const parts = [h("label", {}, "Back up to", h("select", { onchange: (e) => { d.type = e.target.value; drawDest(); } },
          Object.entries(DEST_LABEL).map(([v, l]) => h("option", { value: v, selected: d.type === v }, l))))];
        if (d.type === "local") {
          parts.push(h("label", {}, "Drive", h("select", { onchange: (e) => { d.uuid = e.target.value; drawDest(); } },
            h("option", { value: "", selected: !d.uuid }, "A folder on this NAS (enter full path below)"),
            usb.map((u) => h("option", { value: u.uuid, selected: d.uuid === u.uuid }, `USB: ${u.label || u.model || u.device} (${fmtBytes(u.size)}, ${u.fstype})`)))),
            field(d.uuid ? "Folder on the drive" : "Full folder path (e.g. /volume2/backups)", d, "path", { placeholder: d.uuid ? "Cynthology-Backup" : "/volume2/backups" }));
          if (!usb.length) parts.push(h("div", { class: "muted" }, "No USB drive detected. Plug one in (formatted as ext4, exFAT or NTFS) and reopen this form."));
        } else if (d.type === "sftp") {
          const key = await API.get("/api/backup/sshkey");
          parts.push(field("Server", d, "host", { placeholder: "nas2.local or 192.168.1.50" }), field("Port", d, "port", { type: "number" }),
            field("User", d, "user"), field("Folder on the server", d, "path", { placeholder: "backups/cynthology" }),
            h("div", { class: "note" }, "Add this NAS's key to ", h("span", { class: "mono" }, "~/.ssh/authorized_keys"), " of that user on the server:",
              h("textarea", { readonly: true, style: "width:100%;min-height:60px;margin-top:6px", onclick: (e) => e.target.select() }, key.public_key)));
        } else if (d.type === "s3") {
          parts.push(field("Endpoint", d, "endpoint", { placeholder: "https://s3.us-west-004.backblazeb2.com (empty = AWS)" }),
            field("Bucket", d, "bucket"), field("Folder in bucket", d, "path", { placeholder: "cynthology" }), field("Region (optional)", d, "region"),
            field("Access key ID", d, "access_key", { autocomplete: "off" }),
            h("label", {}, "Secret access key", h("input", { type: "password", autocomplete: "new-password", placeholder: t ? "(unchanged)" : "", oninput: (e) => (d.secret_key = e.target.value) })));
        } else {
          parts.push(field("Server URL", d, "url", { placeholder: "https://user:pass@backup.example.com:8000" }), field("Repository path", d, "path"));
        }
        destBox.replaceChildren(...parts);
      }

      const dayBoxes = h("div", { style: "display:flex;gap:10px;flex-wrap:wrap" }, DAYS.map((dn, i) => h("label", { class: "check" },
        h("input", { type: "checkbox", checked: f.schedule.days.includes(i), onchange: (e) => {
          f.schedule.days = e.target.checked ? [...f.schedule.days, i].sort() : f.schedule.days.filter((x) => x !== i);
        } }), dn)));
      const genPw = () => Array.from(crypto.getRandomValues(new Uint8Array(18)), (b) => "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"[b % 57]).join("");
      const pwInput = h("input", { type: "text", value: "", autocomplete: "off", oninput: (e) => (f.password = e.target.value) });

      pane.replaceChildren(h("h2", {}, t ? `Edit ${t.name}` : "Create backup task"),
        h("div", { class: "form", style: "max-width:640px" },
          field("Task name", f, "name", { placeholder: "Nightly to USB" }),
          h("div", {}, h("div", { class: "muted" }, "Shared folders to back up"),
            h("div", { style: "display:flex;gap:12px;flex-wrap:wrap;margin-top:4px" }, shares.map((s) => h("label", { class: "check" },
              h("input", { type: "checkbox", checked: f.sources.includes(s.name), onchange: (e) => {
                f.sources = e.target.checked ? [...f.sources, s.name] : f.sources.filter((x) => x !== s.name);
              } }), s.name)))),
          h("h3", { class: "pc-group" }, "Destination"), destBox,
          h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: async () => {
            testOut.replaceChildren(h("span", { class: "muted" }, "Testing…"));
            try { const r = await API.post("/api/backup/test", f); testOut.replaceChildren(h("div", { class: "note" }, "✓ " + r.message)); }
            catch (e) { testOut.replaceChildren(h("div", { class: "note warn" }, e.message)); }
          } }, "Test connection")), testOut,
          h("h3", { class: "pc-group" }, "Schedule"),
          h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.schedule.enabled, onchange: (e) => (f.schedule.enabled = e.target.checked) }), "Run automatically"),
          h("div", { style: "display:flex;gap:12px;align-items:center" }, h("span", { class: "muted" }, "at"),
            h("input", { type: "time", value: f.schedule.time, onchange: (e) => (f.schedule.time = e.target.value) })), dayBoxes,
          h("h3", { class: "pc-group" }, "Versions to keep"),
          h("div", { style: "display:flex;gap:12px" }, ["daily", "weekly", "monthly"].map((k) => h("label", {}, `${k[0].toUpperCase() + k.slice(1)}`,
            h("input", { type: "number", min: 0, max: 999, value: f.retention[k] ?? 0, style: "width:90px", oninput: (e) => (f.retention[k] = +e.target.value) })))),
          h("div", { class: "muted" }, "0 everywhere = keep every version."),
          t ? null : h("div", {}, h("h3", { class: "pc-group" }, "Encryption password"),
            h("div", { class: "note warn" }, "All backups are encrypted with this password. ", h("b", {}, "Without it the backup can never be restored"),
              " - e.g. after this NAS dies. Write it down or save it in a password manager, not only on this NAS."),
            h("label", {}, "Password (8+ characters)", pwInput),
            h("button", { class: "btn", style: "margin-top:6px", onclick: () => { pwInput.value = f.password = genPw(); } }, "Generate a strong password"),
            h("label", { class: "check", style: "margin-top:8px" }, h("input", { type: "checkbox", id: "pw-saved" }), "I have saved the password somewhere safe")),
          h("div", { class: "toolbar" },
            h("button", { class: "btn", onclick: list }, "Cancel"),
            h("button", { class: "btn primary", onclick: async () => {
              if (!t && !document.getElementById("pw-saved").checked) return toast("Please confirm you saved the encryption password");
              await act(() => (t ? API.put(`/api/backup/${t.id}`, f) : API.post("/api/backup", f)), t ? "Saved" : "Backup task created");
              list();
            } }, t ? "Save" : "Create"))));
      drawDest();
    }

    // ---------- restore ----------
    async function restoreView(t) {
      pane.replaceChildren(h("h2", {}, `Restore from ${t.name}`), h("p", { class: "muted" }, "Reading backup versions…"));
      let versions;
      try { versions = await API.get(`/api/backup/${t.id}/versions`); } catch (e) { pane.append(h("div", { class: "err" }, e.message)); return; }
      // start browsing where the backed-up folders live (e.g. /volume1), not at /
      const startPath = (v) => {
        const parents = (v?.paths || []).map((p) => p.split("/").slice(0, -1).join("/") || "/");
        if (!parents.length) return "/";
        let common = parents[0];
        for (const p of parents) while (common !== "/" && !(p + "/").startsWith(common + "/")) common = common.split("/").slice(0, -1).join("/") || "/";
        return common;
      };
      const st = { version: versions[0]?.id, path: startPath(versions[0]), sel: new Set() };
      const tree = h("div");
      const shares = await API.get("/api/shares");

      async function draw() {
        tree.replaceChildren(h("p", { class: "muted" }, "Loading…"));
        const items = await API.get(`/api/backup/${t.id}/browse?version=${st.version}&path=${encodeURIComponent(st.path)}`);
        const up = st.path === "/" ? null : st.path.split("/").slice(0, -1).join("/") || "/";
        tree.replaceChildren(
          h("div", { class: "fs-crumbs mono" }, st.path),
          h("table", { class: "fs-table" },
            up ? h("tr", { ondblclick: () => { st.path = up; draw(); } }, h("td", { colspan: 3 }, "⬆ ..")) : null,
            items.map((i) => h("tr", { ondblclick: () => { if (i.dir) { st.path = i.path; draw(); } } },
              h("td", {}, h("label", { class: "check" }, h("input", { type: "checkbox", checked: st.sel.has(i.path),
                onchange: (e) => { e.target.checked ? st.sel.add(i.path) : st.sel.delete(i.path); } }), (i.dir ? "📁 " : fileIcon(i.name) + " ") + i.name)),
              h("td", { class: "muted" }, i.dir ? "" : fmtBytes(i.size)),
              h("td", { class: "muted" }, i.mtime ? new Date(i.mtime).toLocaleString() : "")))),
          h("div", { class: "muted", style: "margin-top:6px" }, "Double-click a folder to open it. Tick files or folders to restore."));
      }

      const into = h("select", {}, shares.map((s) => h("option", { value: s.name }, s.name)));
      const mode = { to: "new" };
      pane.replaceChildren(h("h2", {}, `Restore from ${t.name}`),
        h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: list }, "← Back"),
          h("label", {}, "Version ", h("select", { onchange: (e) => { st.version = e.target.value; st.path = startPath(versions.find((v) => v.id === st.version)); st.sel.clear(); draw(); } },
            versions.map((v) => h("option", { value: v.id }, new Date(v.time).toLocaleString() + (v.bytes ? ` (${fmtBytes(v.bytes)})` : "")))))),
        versions.length ? tree : h("p", { class: "muted" }, "No backup versions yet - run the backup first."),
        versions.length ? h("div", { class: "form", style: "margin-top:14px" },
          h("label", { class: "check" }, h("input", { type: "radio", name: "rto", checked: true, onchange: () => (mode.to = "new") }),
            "Restore into a new folder “Restored <date>” in shared folder ", into),
          h("label", { class: "check" }, h("input", { type: "radio", name: "rto", onchange: () => (mode.to = "original") }),
            "Restore to the original location (overwrites current files)"),
          h("div", {}, h("button", { class: "btn primary", onclick: async () => {
            if (!st.sel.size && !confirm("Nothing ticked - restore EVERYTHING from this version?")) return;
            if (mode.to === "original" && !confirm("Overwrite the current files with the backed-up versions?")) return;
            const r = await act(() => API.post(`/api/backup/${t.id}/restore`, { version: st.version, paths: [...st.sel], to: mode.to, share: into.value }), "Restore finished");
            if (r.target !== "/") toast("Restored to " + r.target);
          } }, "Restore"))) : null);
      if (versions.length) draw();
    }

    list();
  },
});
