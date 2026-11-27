// Cloud Sync: keep a folder on the NAS in sync with Google Drive, OneDrive, Dropbox, Box, S3, B2, WebDAV or SFTP.
const CS_DIR = {
  upload: ["NAS → cloud", "Changes on the NAS are copied to the cloud."],
  download: ["Cloud → NAS", "Changes in the cloud are copied to the NAS."],
  both: ["Two-way (beta)", "Changes on either side are copied to the other. Deleting a file deletes it on both sides."],
};
const CS_EVERY = [[0, "Only when I click Sync now"], [5, "Every 5 minutes"], [15, "Every 15 minutes"], [30, "Every 30 minutes"], [60, "Every hour"],
  [180, "Every 3 hours"], [360, "Every 6 hours"], [720, "Every 12 hours"], [1440, "Once a day"]];
const CS_FIELDS = {
  s3: [["provider", "Provider", "select", [["AWS", "Amazon S3"], ["Wasabi", "Wasabi"], ["Cloudflare", "Cloudflare R2"], ["Minio", "MinIO"], ["Other", "Other S3-compatible"]]],
    ["endpoint", "Endpoint (leave empty for Amazon)", "text"], ["region", "Region", "text"], ["access_key_id", "Access key ID", "text"], ["secret_access_key", "Secret access key", "password"]],
  b2: [["account", "Key ID (account)", "text"], ["key", "Application key", "password"]],
  webdav: [["url", "Address (https://…)", "text"], ["vendor", "Server", "select", [["nextcloud", "Nextcloud"], ["owncloud", "ownCloud"], ["sharepoint", "SharePoint"], ["other", "Other WebDAV"]]],
    ["user", "User name", "text"], ["pass", "Password", "password"]],
  sftp: [["host", "Server", "text"], ["port", "Port", "text"], ["user", "User name", "text"], ["pass", "Password", "password"]],
};

WM.register("cloudsync", {
  name: "Cloud Sync", icon: "☁", color: "#0ea5e9", w: 980, h: 640,
  open(body, win) {
    let data = null, timer = null;
    win.cleanup.push(() => clearTimeout(timer));
    const reload = async () => (data = await API.get("/api/cloudsync"));
    const fmtT = (s) => (s ? new Date(s * 1000).toLocaleString() : "-");
    let nav = () => {};
    nav = sectioned({
      tasks: { label: "Sync tasks", render: (p) => tasksView(p) },
      connections: { label: "Connections", render: (p) => connsView(p) },
    }, body);

    async function tasksView(p) {
      clearTimeout(timer);
      await reload();
      const draw = () => {
        p.replaceChildren(h("h2", {}, "Cloud Sync"),
          h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: () => data.connections.length ? taskEditor() : connEditor(null, () => taskEditor()) }, "+ Create sync task")),
          data.tasks.length ? h("div", { class: "cards", style: "grid-template-columns:repeat(auto-fill,minmax(380px,1fr))" }, data.tasks.map(card))
            : h("div", { class: "note" }, t_("Keep a folder on the NAS in sync with Google Drive, OneDrive, Dropbox, Box, an S3 bucket, a Nextcloud or another server. "),
              t_("Unlike Backup (versions you can go back to), Cloud Sync keeps both sides the same.")));
        if (data.tasks.some((t) => t.running)) timer = setTimeout(async () => { await reload(); draw(); }, 4000);
      };
      draw();
    }

    function card(t) {
      const s = t.status;
      const [pill, label] = t.running ? ["warn", t_("Syncing…")] : s.state === "ok" ? ["ok", t_("Up to date")] : s.state === "failed" ? ["bad", t_("Failed")] : ["", t_("Not synced yet")];
      const arrow = { upload: "→", download: "←", both: "⇄" }[t.direction];
      return h("div", { class: "card" },
        h("div", {}, h("b", {}, t.name), " ", h("span", { class: "pill " + pill }, label), t.enabled ? null : h("span", { class: "pill" }, t_("Paused"))),
        h("div", { class: "mono", style: "font-size:13px", translate: "no" }, `${t.share}/${t.local_path}`.replace(/\/$/, ""), ` ${arrow} `,
          `${t.connection_name}: ${t.remote_path.startsWith("/") ? "" : "/"}${t.remote_path}`),
        h("div", { class: "muted" }, t_(CS_DIR[t.direction][0]), t.direction !== "both" && t.mirror ? ` · ${t_("mirror (deletions too)")}` : "",
          " · ", t_(CS_EVERY.find(([v]) => v === t.every)?.[1] || "")),
        s.error ? h("div", { class: "err" }, s.error) : null,
        h("div", { class: "muted" }, `${t_("Last sync")}: ${fmtT(s.finished || s.started)}` + (s.state === "ok" ? ` · ${s.copied || 0} ${t_("copied")}, ${s.deleted || 0} ${t_("deleted")}` : "")),
        h("div", { class: "toolbar", style: "margin:4px 0 0" },
          t.running ? h("button", { class: "btn", onclick: async () => { await act(() => API.post(`/api/cloudsync/tasks/${t.id}/stop`), "Stopped"); nav("tasks"); } }, "Stop")
            : h("button", { class: "btn primary", onclick: async () => { await act(() => API.post(`/api/cloudsync/tasks/${t.id}/run`), "Sync started"); nav("tasks"); } }, "Sync now"),
          h("button", { class: "btn", onclick: () => taskEditor(t) }, "Edit"),
          h("button", { class: "btn", onclick: async () => {
            const r = await API.get(`/api/cloudsync/tasks/${t.id}/log`);
            modal(t.name, h("pre", { class: "mono", style: "max-height:60vh;overflow:auto;font-size:12px;white-space:pre-wrap" }, r.log || t_("(empty)")));
          } }, "Log"),
          h("button", { class: "btn danger", onclick: async () => {
            if (!confirm(t_("Delete this sync task? Files on the NAS and in the cloud stay as they are."))) return;
            await act(() => API.del(`/api/cloudsync/tasks/${t.id}`), "Deleted"); nav("tasks");
          } }, "Delete")));
    }

    async function taskEditor(t) {
      if (!data) await reload();
      const shares = (await API.get("/api/shares")).map((s) => s.name);
      const v = t ? { ...t } : { name: "", connection: data.connections[0]?.id, share: shares[0], local_path: "", remote_path: "", direction: "upload",
        mirror: false, every: 60, bwlimit: "", enabled: true };
      const name = h("input", { value: v.name, placeholder: t_("e.g. Photos to Google Drive") });
      const conn = h("select", {}, data.connections.map((c) => h("option", { value: c.id, selected: c.id === v.connection }, `${c.name} · ${t_(c.type_name)}`)));
      const share = h("select", {}, shares.map((s) => h("option", { value: s, selected: s === v.share, translate: "no" }, s)));
      const lpath = h("input", { value: v.local_path, placeholder: t_("(the whole shared folder)") });
      const rpath = h("input", { value: v.remote_path, placeholder: t_("(the top folder)") });
      const every = h("select", {}, CS_EVERY.map(([n, l]) => h("option", { value: n, selected: n === v.every }, l)));
      const bw = h("input", { value: v.bwlimit, placeholder: t_("unlimited (e.g. 2M = 2 MB/s)"), style: "max-width:240px" });
      const mirror = h("input", { type: "checkbox", checked: v.mirror });
      const enabled = h("input", { type: "checkbox", checked: v.enabled });
      let dir = v.direction;
      const dirBox = h("div", { class: "disk-pick" });
      const drawDir = () => dirBox.replaceChildren(...Object.entries(CS_DIR).map(([k, [l, d]]) =>
        h("label", {}, h("input", { type: "radio", name: "csdir", checked: dir === k, onchange: () => { dir = k; drawDir(); } }),
          h("div", {}, h("b", {}, l), h("div", { class: "muted", style: "font-size:12px" }, d)))),
        dir !== "both" ? h("label", { class: "check", style: "border:0" }, mirror,
          t_("Mirror: also delete files that were deleted on the source")) : null);
      drawDir();
      const browseBtn = h("button", { class: "btn", onclick: () => pickRemote(conn.value, rpath) }, "Browse…");
      const err = h("div", { class: "err" });
      const close = modal(t ? t_("Edit sync task") : t_("Create sync task"), h("div", { class: "form" },
        h("label", {}, "Name", name),
        h("label", {}, "Cloud connection", h("div", { class: "toolbar", style: "margin:0" }, conn,
          h("button", { class: "btn", onclick: () => { close(); connEditor(null, () => taskEditor(t)); } }, "+ New connection"))),
        h("label", {}, "Shared folder on the NAS", share),
        h("label", {}, "Folder inside it", lpath),
        h("label", {}, "Folder in the cloud", h("div", { class: "toolbar", style: "margin:0" }, rpath, browseBtn)),
        h("div", { style: "font-size:13px;color:var(--muted)" }, t_("Direction")), dirBox,
        h("label", {}, "When", every),
        h("label", {}, "Speed limit", bw),
        h("label", { class: "check" }, enabled, t_("Enabled")), err),
        [["Cancel", null], [t ? "Save" : "Create", async () => {
          const body = { name: name.value, connection: conn.value, share: share.value, local_path: lpath.value, remote_path: rpath.value,
            direction: dir, mirror: mirror.checked, every: +every.value, bwlimit: bw.value.trim(), enabled: enabled.checked };
          try {
            await (t ? API.put(`/api/cloudsync/tasks/${t.id}`, body) : API.post("/api/cloudsync/tasks", body));
            close(); toast(t_("Saved")); nav("tasks");
            if (!t && dir === "both") toast(t_("The first two-way sync compares both sides; nothing is deleted on that run."));
          } catch (e) { err.textContent = e.message; }
        }, "primary"]]);
    }

    async function pickRemote(cid, input) {
      let path = input.value.replace(/\/+$/g, "");
      const box = h("div", {}, h("p", { class: "muted" }, t_("Opening…")));
      const close = modal(t_("Choose a cloud folder"), box, [["Cancel", null], ["Use this folder", () => { input.value = path; close(); }, "primary"]]);
      const draw = async () => {
        box.replaceChildren(h("p", { class: "muted" }, t_("Opening…")));
        try {
          const r = await API.get(`/api/cloudsync/connections/${cid}/browse?path=${encodeURIComponent(path)}`);
          box.replaceChildren(h("div", { class: "mono", style: "margin-bottom:8px" }, path.startsWith("/") ? path : "/" + path),
            h("div", { class: "side", style: "width:auto;max-height:45vh;border:1px solid var(--line);border-radius:8px" },
              path && path !== "/" ? h("button", { onclick: () => { path = path.split("/").slice(0, -1).join("/") || (path.startsWith("/") ? "/" : ""); draw(); } }, "↩ ..") : null,
              ...r.folders.map((f) => h("button", { translate: "no", onclick: () => { path = !path ? f : path.endsWith("/") ? path + f : `${path}/${f}`; draw(); } }, "📁 " + f)),
              r.folders.length ? null : h("div", { class: "muted", style: "padding:8px" }, t_("No folders here."))),
            h("p", { class: "muted", style: "font-size:12px" }, t_("A folder that doesn't exist yet is created on the first sync - you can also type a new name.")));
        } catch (e) { box.replaceChildren(h("div", { class: "err" }, e.message)); }
      };
      draw();
    }

    async function connsView(p) {
      await reload();
      p.replaceChildren(h("h2", {}, "Connections"),
        h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: () => connEditor(null, () => nav("connections")) }, "+ Add connection")),
        data.connections.length ? h("table", {}, data.connections.map((c) => h("tr", {},
          h("td", {}, h("b", {}, c.name), h("div", { class: "muted", style: "font-size:12px" }, t_(c.type_name),
            c.cfg.url || c.cfg.host ? ` · ${c.cfg.url || c.cfg.host}` : "")),
          h("td", { style: "text-align:right;white-space:nowrap" },
            h("button", { class: "btn", onclick: async (e) => {
              e.target.disabled = true;
              try { await API.get(`/api/cloudsync/connections/${c.id}/browse`); toast(t_("Connection works")); }
              catch (x) { toast(x.message); }
              e.target.disabled = false;
            } }, "Test"),
            h("button", { class: "btn", onclick: () => connEditor(c, () => nav("connections")) }, "Edit"),
            h("button", { class: "btn danger", onclick: async () => {
              if (!confirm(t_("Remove this connection? Nothing in the cloud is deleted."))) return;
              await act(() => API.del(`/api/cloudsync/connections/${c.id}`), "Removed"); nav("connections");
            } }, "Remove")))))
          : h("p", { class: "muted" }, "No connections yet."));
    }

    function connEditor(c, after) {
      let type = c?.type || "drive", oauth = null;
      const box = h("div");
      const name = h("input", { value: c?.name || "" });
      const err = h("div", { class: "err" });
      const inputs = {};
      const draw = () => {
        const types = Object.entries(data.types);
        box.replaceChildren(
          c ? null : h("div", { class: "cs-types" }, types.map(([k, l]) => h("button", { class: "cs-type" + (k === type ? " on" : ""),
            onclick: () => { type = k; oauth = null; draw(); } }, t_(l)))),
          h("div", { class: "form", style: "margin-top:12px" },
            h("label", {}, "Name", name),
            ...(data.oauth_types.includes(type) ? [oauthBox()] : (CS_FIELDS[type] || []).map(([k, l, kind, opts]) => {
              const cur = c?.type === type ? c.cfg[k] ?? "" : "";
              inputs[k] = kind === "select" ? h("select", {}, opts.map(([v, ol]) => h("option", { value: v, selected: v === cur }, ol)))
                : h("input", { type: kind, value: kind === "password" ? "" : cur, placeholder: kind === "password" && c ? t_("(unchanged)") : "",
                  autocomplete: kind === "password" ? "new-password" : "off" });
              return h("label", {}, l, inputs[k]);
            })), err));
      };
      const oauthBox = () => {
        const signed = oauth?.done || (c?.type === type && c.signed_in);
        const paste = h("input", { placeholder: "http://127.0.0.1:53682/?state=…&code=…", style: "flex:1" });
        return h("div", { class: "card" },
          signed ? h("div", { class: "note" }, "✓ ", t_("Signed in.")) : null,
          !oauth?.session ? h("div", {},
            h("p", {}, t_("Sign in on the provider's own page - your password never goes through the NAS.")),
            h("button", { class: "btn primary", onclick: async (e) => {
              e.target.disabled = true;
              try {
                const r = await API.post("/api/cloudsync/oauth/start", { type });
                oauth = { session: r.session, url: r.url };
                window.open(r.url, "_blank", "noopener");
                draw();
              } catch (x) { err.textContent = x.message; e.target.disabled = false; }
            } }, signed ? "Sign in again" : `${t_("Sign in to")} ${t_(data.types[type])}`))
          : oauth.done ? null : h("div", {},
            h("ol", { class: "wiz-list" },
              h("li", {}, t_("A new tab opened with the sign-in page "), h("a", { href: oauth.url, target: "_blank", rel: "noopener" }, t_("(open it again)")), "."),
              h("li", {}, t_("Sign in and allow access.")),
              h("li", {}, t_("You end up on a page that doesn't load (\"This site can't be reached\"). That's expected: copy the whole address from that tab's address bar and paste it here:"))),
            h("div", { class: "toolbar" }, paste, h("button", { class: "btn primary", onclick: async (e) => {
              e.target.disabled = true; err.textContent = "";
              try { await API.post("/api/cloudsync/oauth/finish", { session: oauth.session, url: paste.value }); oauth.done = true; draw(); }
              catch (x) { err.textContent = x.message; e.target.disabled = false; }
            } }, "Finish"))));
      };
      draw();
      const close = modal(c ? t_("Edit connection") : t_("Add a cloud connection"), box, [["Cancel", null], ["Save", async () => {
        const cfg = {};
        for (const [k, el] of Object.entries(inputs)) if (el.isConnected) cfg[k] = el.value.trim();
        const body = { name: name.value || t_(data.types[type]), type, cfg, oauth_session: oauth?.done ? oauth.session : "" };
        try {
          await (c ? API.put(`/api/cloudsync/connections/${c.id}`, body) : API.post("/api/cloudsync/connections", body));
          close(); toast(t_("Saved")); await reload(); after && after();
        } catch (x) { err.textContent = x.message; }
      }, "primary"]]);
    }
  },
});
