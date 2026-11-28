// Snapshots: schedules, retention and on-demand snapshots of shared folders (Btrfs).
WM.register("snapshots", {
  name: "Snapshots", icon: "🕒", color: "#3a7bd5", w: 980, h: 620,
  open(body) {
    sectioned({
      local: { label: "Snapshots", render: snapshotsLocal },
      send: { label: "Replication", render: replicationSend },
      recv: { label: "Receive replicas", render: replicationReceive },
    }, body);
  },
});

const snapWhen = (n) => n ? new Date(n.replace(/^@GMT-(\d+)\.(\d+)\.(\d+)-(\d+)\.(\d+)\.(\d+)$/, "$1-$2-$3T$4:$5:$6Z")).toLocaleString() : "never";

function snapshotsLocal(pane) {
    const when = (n) => n ? new Date(n.replace(/^@GMT-(\d+)\.(\d+)\.(\d+)-(\d+)\.(\d+)\.(\d+)$/, "$1-$2-$3T$4:$5:$6Z")).toLocaleString() : "never";

    async function render() {
      const rows = await API.get("/api/snapshots");
      pane.replaceChildren(h("h2", {}, "Snapshots"),
        h("div", { class: "note" }, "Snapshots are instant, space-efficient, read-only copies of a shared folder. ",
          "On Windows, right-click a file or folder on the share → Properties → ", h("b", {}, "Previous Versions"),
          " to restore. In File Station, tick “Hidden files” and open .snapshots."),
        h("br"),
        rows.length ? h("table", {},
          h("tr", {}, h("th", {}, "Shared folder"), h("th", {}, "Schedule"), h("th", {}, "Keep"), h("th", { title: "Each new snapshot can't be deleted - by anyone - for this many days. Protects against ransomware and mistakes." }, "Lock (days)"), h("th", {}, "Snapshots"), h("th", {}, "Latest"), h("th", {})),
          rows.map((r) => {
            if (!r.supported) return h("tr", {}, h("td", {}, h("b", {}, r.share)), h("td", { colspan: 6, class: "muted" }, "Not on a Btrfs volume - snapshots unavailable"));
            const pol = { schedule: r.schedule, keep: r.keep, lock_days: r.lock_days || 0 };
            const save = () => act(() => API.put(`/api/snapshots/${r.share}`, pol), "Snapshot schedule saved");
            return h("tr", {},
              h("td", {}, h("b", {}, r.share)),
              h("td", {}, h("select", { onchange: (e) => { pol.schedule = e.target.value; save(); } },
                ["off", "hourly", "daily", "weekly"].map((v) => h("option", { value: v, selected: v === r.schedule }, v[0].toUpperCase() + v.slice(1))))),
              h("td", {}, h("input", { type: "number", min: 0, max: 1000, value: r.keep, style: "width:80px", title: "0 = keep all",
                onchange: (e) => { pol.keep = +e.target.value; save().then(render); } })),
              h("td", {}, h("input", { type: "number", min: 0, max: 3650, value: pol.lock_days, style: "width:80px", title: "0 = not locked",
                onchange: (e) => {
                  const v = +e.target.value;
                  if (v > pol.lock_days && !confirm(t("New snapshots will be locked for") + ` ${v} ` + t("days. Locked snapshots can't be deleted by anyone - not even an administrator - and they keep using space until the lock runs out. Continue?"))) { e.target.value = pol.lock_days; return; }
                  pol.lock_days = v; save();
                } })),
              h("td", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); showList(r.share); } }, String(r.count))),
              h("td", { class: "muted" }, snapWhen(r.latest)),
              h("td", {}, h("button", { class: "btn", onclick: async () => { await act(() => API.post(`/api/snapshots/${r.share}`), "Snapshot taken"); render(); } }, "Take snapshot")));
          }))
          : h("p", { class: "muted" }, "No shared folders yet."));
    }

    async function showList(share) {
      const snaps = await API.get(`/api/snapshots/${share}`);
      pane.replaceChildren(h("h2", {}, `Snapshots of ${share}`),
        h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: render }, "← Back")),
        snaps.length ? h("table", {}, h("tr", {}, h("th", {}, "Taken"), h("th", {}, "Name"), h("th", {})),
          snaps.map((s) => h("tr", {}, h("td", {}, new Date(s.time).toLocaleString(),
              s.locked_until ? h("span", { class: "pill info", style: "margin-left:8px", title: t("Locked until") + " " + new Date(s.locked_until * 1000).toLocaleString() }, "🔒 " + new Date(s.locked_until * 1000).toLocaleDateString()) : null),
            h("td", { class: "mono muted" }, s.name),
            h("td", { style: "white-space:nowrap" },
              h("button", { class: "btn primary", onclick: () => snapBrowse(pane, share, s, () => showList(share)) }, "Browse & restore"), " ",
              h("button", { class: "btn", onclick: async () => {
                if (!confirm(`Put the WHOLE shared folder "${share}" back to how it was on ${new Date(s.time).toLocaleString()}?\n\nEverything changed or added since then is undone. A safety snapshot of the current state is taken first, so you can go back.`)) return;
                const r = await act(() => API.post(`/api/snapshots/${share}/${encodeURIComponent(s.name)}/revert`), "Shared folder restored");
                if (r) toast("Safety snapshot of the previous state: " + snapWhen(r.safety_snapshot));
                showList(share);
              } }, "Restore whole folder"), " ",
              s.locked_until ? null : h("button", { class: "btn danger", onclick: async () => {
              if (!confirm(`Delete snapshot ${s.name}?`)) return;
              await act(() => API.del(`/api/snapshots/${share}/${encodeURIComponent(s.name)}`), "Snapshot deleted"); showList(share);
            } }, "Delete")))))
          : h("p", { class: "muted" }, "No snapshots yet."));
    }
    render();
}

// ---------- replication: this NAS sends ----------
async function replicationSend(pane) {
  const [tasks, shares, san] = await Promise.all([API.get("/api/replication/tasks"), API.get("/api/snapshots"), API.get("/api/san").catch(() => ({ luns: [] }))]);
  const luns = san.luns || [];
  const what = (t) => (t.lun ? `LUN ${t.lun}` : t.share);
  const redraw = () => { pane.replaceChildren(); replicationSend(pane); };
  const label = { success: ["ok", "Up to date"], failed: ["bad", "Failed"], running: ["warn", "Replicating…"], never: ["", "Never run"] };
  pane.append(h("h2", {}, "Snapshot replication"),
    h("div", { class: "note" }, "Keeps a copy of a shared folder or iSCSI LUN on another Cynthology NAS, updated on a schedule. ",
      "After the first full copy only changes are sent. If this NAS fails, the other one can turn the replica into a normal shared folder or LUN."),
    h("div", { class: "toolbar", style: "margin-top:12px" }, h("button", { class: "btn primary", onclick: () => replicationEditor(pane, null, shares, redraw, luns) }, "+ Replicate a shared folder or LUN")),
    tasks.length ? h("table", {},
      h("tr", {}, h("th", {}, "What"), h("th", {}, "To"), h("th", {}, "Schedule"), h("th", {}, "Status"), h("th", {})),
      tasks.map((t) => {
        const [cls, txt] = label[t.status.state] || ["", t.status.state];
        return h("tr", {},
          h("td", {}, h("b", {}, what(t))), h("td", { class: "mono" }, t.host),
          h("td", {}, t.schedule, t.keep ? h("div", { class: "muted" }, `keep ${t.keep}`) : null),
          h("td", {}, h("span", { class: "pill " + cls }, txt),
            t.status.finished ? h("div", { class: "muted" }, new Date(t.status.finished * 1000).toLocaleString() + (t.status.state === "success" ? (t.status.incremental ? " · changes only" : " · full copy") : "")) : null,
            t.status.error ? h("div", { class: "err" }, t.status.error) : null),
          h("td", {},
            h("button", { class: "btn primary", disabled: t.status.state === "running", onclick: async () => { await act(() => API.post(`/api/replication/tasks/${t.id}/run`), "Replication started"); setTimeout(redraw, 1500); } }, "Sync now"), " ",
            h("button", { class: "btn", onclick: () => replicationEditor(pane, t, shares, redraw, luns) }, "Edit"), " ",
            h("button", { class: "btn danger", onclick: async () => {
              if (!confirm(`Stop replicating ${what(t)}? Replicas already on ${t.host} are kept.`)) return;
              await act(() => API.del(`/api/replication/tasks/${t.id}`), "Removed"); redraw();
            } }, "Remove")));
      }))
      : h("p", { class: "muted" }, "Nothing is replicated yet."));
}

async function replicationEditor(pane, t, shares, done, luns = []) {
  const key = await API.get("/api/replication/key");
  const f = t ? { lun: "", ...t } : { share: shares.find((s) => s.supported)?.share || "", lun: "", host: "", port: 22, schedule: "daily", keep: 30 };
  const cur = f.lun ? `lun:${f.lun}` : `share:${f.share}`;
  const out = h("div");
  pane.replaceChildren(h("h2", {}, t ? `Edit replication of ${t.lun ? "LUN " + t.lun : t.share}` : "Replicate a shared folder or LUN"),
    h("div", { class: "form" },
      h("div", { class: "note" }, h("b", {}, "Step 1 - on the other NAS: "), "Snapshots → Receive replicas → Allow a NAS, name it ",
        h("b", { class: "mono" }, key.hostname.toLowerCase().replace(/[^a-z0-9-]/g, "-")), " and paste this key:",
        h("textarea", { readonly: true, style: "width:100%;min-height:58px;margin-top:6px", onclick: (e) => e.target.select() }, key.public_key)),
      h("label", {}, "What to replicate", h("select", { onchange: (e) => {
          const [kind, name] = e.target.value.split(/:(.*)/s);
          f.share = kind === "share" ? name : ""; f.lun = kind === "lun" ? name : "";
        } },
        h("optgroup", { label: t ? "Shared folders" : "Shared folders" }, shares.map((s) => h("option", { value: `share:${s.share}`, selected: cur === `share:${s.share}`, disabled: !s.supported, translate: "no" }, s.share + (s.supported ? "" : " (not on Btrfs)")))),
        luns.length ? h("optgroup", { label: "iSCSI LUNs" }, luns.map((l) => h("option", { value: `lun:${l.name}`, selected: cur === `lun:${l.name}`, translate: "no" }, `LUN ${l.name}`))) : null)),
      h("label", {}, "Other NAS (name or IP address)", h("input", { value: f.host, placeholder: "192.168.1.60", oninput: (e) => (f.host = e.target.value.trim()) })),
      h("label", {}, "SSH port", h("input", { type: "number", value: f.port, style: "width:100px", oninput: (e) => (f.port = +e.target.value) })),
      h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: async () => {
        out.replaceChildren(h("span", { class: "muted" }, "Connecting…"));
        try { const r = await API.post("/api/replication/test", { host: f.host, port: f.port }); out.replaceChildren(h("div", { class: "note" }, "✓ " + r.message)); }
        catch (e) { out.replaceChildren(h("div", { class: "note warn" }, e.message)); }
      } }, "Test connection")), out,
      h("label", {}, "Schedule", h("select", { onchange: (e) => (f.schedule = e.target.value) },
        [["hourly", "Every hour"], ["daily", "Every day"], ["weekly", "Every week"], ["manual", "Only when I click Sync now"]].map(([v, l]) => h("option", { value: v, selected: f.schedule === v }, l)))),
      h("label", {}, "Replicas to keep on the other NAS (0 = all)", h("input", { type: "number", min: 0, value: f.keep, style: "width:100px", oninput: (e) => (f.keep = +e.target.value) })),
      h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: done }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => {
          await act(() => (t ? API.put(`/api/replication/tasks/${t.id}`, f) : API.post("/api/replication/tasks", f)), "Saved"); done();
        } }, t ? "Save" : "Create"))));
}

// ---------- replication: this NAS receives ----------
async function replicationReceive(pane) {
  const [senders, received, watch, shares] = await Promise.all([API.get("/api/replication/senders"), API.get("/api/replication/received"),
    API.get("/api/replication/watch").catch(() => ({})), API.get("/api/shares").catch(() => [])]);
  const localNames = new Set(shares.map((x) => x.name.toLowerCase()));
  const watchEdit = (src) => {
    const w = { max_age_h: 26, address: "", port: 5001, ...(watch[src] || {}) };
    const close = modal(`${t("Watch")} ${src}`, h("div", { class: "form" },
      h("p", { class: "muted" }, t("This NAS keeps copies of the other one's shared folders. Get an alert when the copies stop arriving, or when the other NAS stops answering - then you can bring its folders up here.")),
      h("label", {}, "Alert when no new copy arrives for (hours, 0 = never)", h("input", { type: "number", min: 0, value: w.max_age_h, oninput: (e) => (w.max_age_h = +e.target.value) })),
      h("label", {}, "Its address (optional: checked every 10 minutes)", h("input", { value: w.address, placeholder: "192.168.1.20", oninput: (e) => (w.address = e.target.value.trim()) })),
      h("label", {}, "Port", h("input", { type: "number", value: w.port, oninput: (e) => (w.port = +e.target.value) }))),
      [["Cancel", null], ["Save", async () => { await act(() => API.put(`/api/replication/watch/${src}`, w), "Saved"); close(); redraw(); }, "primary"]]);
  };
  const redraw = () => { pane.replaceChildren(); replicationReceive(pane); };
  const nf = { source: "", public_key: "" };
  pane.append(h("h2", {}, "Receive replicas"),
    h("h3", { class: "pc-group" }, "NAS devices allowed to send replicas here"),
    senders.length ? h("table", {}, senders.map((s) => h("tr", {}, h("td", {}, h("b", {}, s.source),
        watch[s.source] ? h("div", { class: "muted", style: "font-size:12px" }, `${t("watched")}` + (watch[s.source].address ? ` · ${watch[s.source].address}` : "")) : null),
      h("td", { class: "muted mono" }, s.comment || "…" + s.fingerprint),
      h("td", { style: "white-space:nowrap" }, h("button", { class: "btn", onclick: () => watchEdit(s.source) }, "Watch…"), h("button", { class: "btn danger", onclick: async () => {
        if (!confirm(`Stop accepting replicas from ${s.source}? Replicas already here are kept.`)) return;
        await act(() => API.del(`/api/replication/senders/${s.source}`), "Removed"); redraw();
      } }, "Remove")))))
      : h("p", { class: "muted" }, "None yet."),
    h("details", { open: !senders.length }, h("summary", {}, "Allow a NAS"),
      h("div", { class: "form", style: "margin-top:8px" },
        h("label", {}, "Name for the sending NAS", h("input", { placeholder: "nas1", oninput: (e) => (nf.source = e.target.value) })),
        h("label", {}, "Its replication key (Snapshots → Replication → + Replicate on that NAS)", h("textarea", { placeholder: "ssh-ed25519 AAAA… cynthology-replication@nas1", oninput: (e) => (nf.public_key = e.target.value) })),
        h("div", {}, h("button", { class: "btn primary", onclick: async () => { await act(() => API.post("/api/replication/senders", nf), "NAS allowed"); redraw(); } }, "Allow")),
        h("div", { class: "muted" }, "The key can only deliver replicas: no login, no shell, nothing outside the replica area."))),
    h("h3", { class: "pc-group", style: "margin-top:20px" }, "Replicas stored on this NAS"),
    received.length ? h("table", {},
      h("tr", {}, h("th", {}, "From"), h("th", {}, "What"), h("th", {}, "Versions"), h("th", {}, "Latest"), h("th", {})),
      received.map((r) => {
        const pick = h("select", {}, r.snapshots.map((n) => h("option", { value: n }, snapWhen(n))));
        return h("tr", {}, h("td", {}, r.source), h("td", {}, h("b", {}, r.lun ? `LUN ${r.lun}` : r.share)), h("td", {}, String(r.count)), h("td", { class: "muted" }, snapWhen(r.latest)),
          h("td", {}, r.count ? h("div", { style: "display:flex;gap:6px;flex-wrap:wrap" }, pick,
            r.lun ? h("button", { class: "btn primary", title: "Make this version available as a writable iSCSI LUN (SAN Manager)", onclick: async () => {
              const name = prompt("Name of the new LUN (lowercase letters, digits, -):", `${r.lun}-${r.source}`.toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 31)); if (!name) return;
              await act(() => API.post("/api/san/luns/from-replica", { source: r.source, lun: r.lun, snapshot: pick.value, new_name: name }), `LUN ${name} created - map it to a target in SAN Manager`);
            } }, "Use as LUN")
            : localNames.has(r.share.toLowerCase())
              ? h("button", { class: "btn", title: t("Failback: this copy replaces the shared folder of the same name here (the current contents are kept beside it)"), onclick: async () => {
                  if (!confirm(t("Replace the live shared folder") + ` "${r.share}" ` + t("with this copy? Its current contents are kept as a separate folder, nothing is deleted. Disconnect users first."))) return;
                  await act(() => API.post("/api/replication/failover", { source: r.source, share: r.share, snapshot: pick.value, new_name: r.share, replace: true }), "Replaced"); redraw();
                } }, "Replace the live folder")
              : h("button", { class: "btn primary", title: "Make this version available as a normal, writable shared folder", onclick: async () => {
                  // (the same name as on the other NAS: \\nas\\docs keeps working once clients point here)
                  const name = prompt(t("Name of the shared folder (the same name as on the other NAS keeps paths working):"), r.share); if (!name) return;
                  const ok = await act(() => API.post("/api/replication/failover", { source: r.source, share: r.share, snapshot: pick.value, new_name: name }), `Shared folder ${name} created - only administrators can open it until you set its permissions (Control Panel > Shared Folders)`);
                  if (ok) modal(t("Failed over"), h("div", { class: "form" },
                    h("p", {}, t("The folder is live on this NAS. Point users at this NAS (or move its address/DNS name here).")),
                    h("p", { class: "muted" }, t("Later, to fail back: create a replication task here that sends this folder to the original NAS, then on that NAS use 'Replace the live folder' on the copy it received."))));
                  redraw();
                } }, "Use as shared folder"),
            h("button", { class: "btn danger", onclick: async () => {
              if (!confirm(`Delete all ${r.count} replica(s) of ${r.lun ? "LUN " + r.lun : r.share} from ${r.source}?`)) return;
              await act(() => API.post("/api/replication/received/delete", { source: r.source, share: r.share }), "Deleted"); redraw();
            } }, "Delete")) : null));
      }))
      : h("p", { class: "muted" }, "No replicas received yet."));
}

// Browse a snapshot like a folder and restore selected items.
async function snapBrowse(pane, share, snap, back) {
  const st = { path: "", sel: new Set() };
  const list = h("div"), crumbs = h("div", { class: "fs-crumbs" });
  const draw = async () => {
    const items = await API.get(`/api/snapshots/${share}/${encodeURIComponent(snap.name)}/browse?path=${encodeURIComponent(st.path)}`);
    const parts = st.path ? st.path.split("/") : [];
    crumbs.replaceChildren(h("a", { href: "#", onclick: (e) => { e.preventDefault(); st.path = ""; draw(); } }, share),
      ...parts.flatMap((x, i) => [h("span", { class: "muted" }, " › "), h("a", { href: "#", onclick: (e) => { e.preventDefault(); st.path = parts.slice(0, i + 1).join("/"); draw(); } }, x)]));
    const rel = (n) => (st.path ? `${st.path}/${n}` : n);
    list.replaceChildren(h("table", { class: "fs-table" },
      h("tr", {}, h("th", {}, "Name"), h("th", {}, "Size"), h("th", {}, "Modified then"), h("th", {}, "Now")),
      items.length ? items.map((i) => h("tr", { ondblclick: () => { if (i.dir) { st.path = rel(i.name); draw(); } } },
        h("td", {}, h("label", { class: "check" }, h("input", { type: "checkbox", checked: st.sel.has(rel(i.name)),
            onchange: (e) => (e.target.checked ? st.sel.add(rel(i.name)) : st.sel.delete(rel(i.name))) }),
          fsIcon(i, 18), " " + i.name)),
        h("td", { class: "muted" }, i.dir ? "" : fmtBytes(i.size)),
        h("td", { class: "muted" }, new Date(i.mtime * 1000).toLocaleString()),
        h("td", {}, !i.exists_now ? h("span", { class: "pill bad" }, "deleted since") : i.changed ? h("span", { class: "pill warn" }, "changed since") : h("span", { class: "muted" }, "same"))))
        : h("tr", {}, h("td", { colspan: 4, class: "muted" }, "Empty folder"))));
  };
  const restore = async (to) => {
    if (!st.sel.size) return toast("Tick the files or folders to restore");
    if (to === "original" && !confirm(`Restore ${st.sel.size} item(s) to their original place? Current versions are overwritten.`)) return;
    const r = await act(() => API.post(`/api/snapshots/${share}/${encodeURIComponent(snap.name)}/restore`, { paths: [...st.sel], to }),
      to === "copy" ? "Restored into a new folder" : "Restored");
    if (r && to === "copy") toast(`Find them in ${share}/${r.target.split("/").pop()}`);
    st.sel.clear(); draw();
  };
  pane.replaceChildren(h("h2", {}, `${share} - ${new Date(snap.time).toLocaleString()}`),
    h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: back }, "← Back"),
      h("button", { class: "btn primary", onclick: () => restore("original") }, "Restore to original location"),
      h("button", { class: "btn", onclick: () => restore("copy") }, "Restore as copy")),
    h("div", { class: "muted", style: "margin-bottom:6px" }, "Double-click a folder to open it, tick items to restore."),
    crumbs, list);
  draw();
}
