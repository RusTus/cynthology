// SAN Manager: iSCSI targets and LUNs (block storage for VMware ESXi, Hyper-V, Proxmox...).
WM.register("san", {
  name: "SAN Manager", icon: "🧱", color: "#0f766e", w: 1000, h: 640,
  open(body) {
    sectioned({
      overview: { label: "Overview", render: sanOverview },
      luns: { label: "LUN", render: sanLuns },
      targets: { label: "iSCSI targets", render: sanTargets },
    }, body);
  },
});

async function sanData(p) {
  try { return await API.get("/api/san"); } catch (e) { p.append(h("div", { class: "note warn" }, e.message)); return null; }
}

async function sanOverview(p) {
  const d = await sanData(p); if (!d) return;
  const connected = d.targets.flatMap((t) => t.sessions || []);
  p.append(h("h2", {}, "SAN Manager"),
    !d.available ? h("div", { class: "note warn" }, `iSCSI isn't available: ${d.why}`) : null,
    h("div", { class: "cards" },
      h("div", { class: "card" }, h("div", { class: "muted" }, "LUNs"), h("div", { style: "font-size:28px;font-weight:600" }, String(d.luns.length))),
      h("div", { class: "card" }, h("div", { class: "muted" }, "iSCSI targets"), h("div", { style: "font-size:28px;font-weight:600" }, String(d.targets.length))),
      h("div", { class: "card" }, h("div", { class: "muted" }, "Connected hosts"), h("div", { style: "font-size:28px;font-weight:600;color:var(--ok)" }, String(new Set(connected).size)))),
    h("h3", { class: "section-title" }, "Connect VMware ESXi"),
    h("ol", { class: "wiz-list" },
      h("li", {}, "Here: create a LUN and an iSCSI target for it (LUN → Create)."),
      h("li", {}, "In vSphere: host → Configure → Storage Adapters → iSCSI Software Adapter (add it if missing) → Dynamic Discovery → Add, and enter ",
        h("b", { class: "mono" }, d.portals.join(" or ") || "this NAS's address:3260"), "."),
      h("li", {}, "Rescan the adapter, then Storage → New Datastore → VMFS on the new “LIO-ORG” iSCSI disk."),
      h("li", {}, "Check: the datastore's Hardware Acceleration column shows ", h("b", {}, "Supported"),
        " - ESXi then uses this NAS for cloning, zeroing, locking and space reclaim (VAAI). No plugin is needed.")),
    h("p", { class: "muted", style: "font-size:12px" },
      "To limit access to specific hosts, copy each host's initiator name (iSCSI Software Adapter → Properties → iSCSI Name, e.g. iqn.1998-01.com.vmware:esx01-…) into the target's allowed initiators. ",
      "Use a separate network (or VLAN) for storage traffic if you can, and turn on jumbo frames (MTU 9000) on both sides only if every switch supports them."));
}

async function sanLuns(p) {
  const d = await sanData(p); if (!d) return;
  const pools = await API.get("/api/storage/pools");
  const reload = () => { p.replaceChildren(); sanLuns(p); };
  p.append(h("div", { class: "toolbar" }, h("h2", { style: "margin:0;flex:1" }, "LUN"),
      h("button", { class: "btn primary", disabled: !d.available || !pools.length, onclick: () => sanLunWizard(p, d, pools, reload) }, "Create")),
    !pools.length ? h("p", { class: "muted" }, "Create a storage pool first.") : null,
    d.luns.length ? h("table", {},
      h("tr", {}, h("th", {}, "Name"), h("th", {}, "Capacity"), h("th", {}, "Used"), h("th", {}, "Type"), h("th", {}, "Location"), h("th", {}, "Target"), h("th", {}, "Snapshots"), h("th", {})),
      d.luns.map((l) => h("tr", {},
        h("td", {}, h("b", { translate: "no" }, l.name)),
        h("td", {}, fmtBytes(l.size)),
        h("td", { class: "muted" }, fmtBytes(l.used)),
        h("td", {}, l.thin ? "Thin" : "Thick"),
        h("td", { class: "muted" }, l.pool),
        h("td", {}, h("select", { translate: "no", onchange: async (e) => {
          await act(() => API.post(`/api/san/luns/${l.name}/map`, { target: e.target.value }), e.target.value ? `Mapped to ${e.target.value}` : "Unmapped");
          reload();
        } }, h("option", { value: "" }, "— not mapped —"), d.targets.map((t) => h("option", { value: t.name, selected: l.target === t.name }, t.name)))),
        h("td", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); p.replaceChildren(); sanLunSnaps(p, l, reload); } }, String(l.snapshots || 0))),
        h("td", { style: "white-space:nowrap" },
          h("button", { class: "btn", onclick: () => { p.replaceChildren(); sanLunSnaps(p, l, reload); } }, "Snapshots"), " ",
          h("button", { class: "btn", onclick: async () => {
            const name = prompt(`Clone ${l.name} as (new LUN name):`, `${l.name}-clone`); if (!name) return;
            await act(() => API.post("/api/san/luns", { name, pool_id: l.pool_id, size_gb: 1, clone_of: l.name }), "LUN cloned"); reload();
          } }, "Clone"), " ",
          h("button", { class: "btn danger", onclick: async () => {
            if (prompt(`Delete LUN ${l.name} and ALL data on it?\n\nType the LUN name to confirm:`) !== l.name) return;
            await act(() => API.del(`/api/san/luns/${l.name}`), "LUN deleted"); reload();
          } }, "Delete")))))
      : h("p", { class: "muted" }, "No LUNs yet."),
    h("p", { class: "muted", style: "font-size:12px" }, "Thin LUNs use pool space only as data is written, and get it back when ESXi reclaims it (UNMAP). ",
      "Thick LUNs reserve their full size up front. Clone makes an instant copy on Btrfs pools (best while the LUN isn't in use)."));
}

function sanLunWizard(p, d, pools, done) {
  const v = { name: `lun-${d.luns.length + 1}`, pool_id: pools[0].id, size_gb: 100, thin: true, target: d.targets[0]?.name || "", newTarget: !d.targets.length };
  const err = h("div", { class: "err" });
  const box = h("div", { class: "form", style: "padding:0" });
  const draw = () => {
    const pool = pools.find((x) => x.id === +v.pool_id);
    box.replaceChildren(
      h("label", {}, "Name", h("input", { value: v.name, oninput: (e) => (v.name = e.target.value.trim().toLowerCase()) })),
      h("label", {}, "Location", h("select", { onchange: (e) => { v.pool_id = +e.target.value; draw(); } },
        pools.map((x) => h("option", { value: x.id, selected: x.id === +v.pool_id }, `${x.label} (${fmtBytes(x.total - x.used)} free)`)))),
      h("label", {}, "Capacity (GB)", h("input", { type: "number", min: 1, value: v.size_gb, oninput: (e) => (v.size_gb = +e.target.value) })),
      h("label", { class: "check" }, h("input", { type: "radio", name: "prov", checked: v.thin, onchange: () => { v.thin = true; draw(); } }),
        h("div", {}, h("b", {}, "Thin provisioning"), h("div", { class: "muted" }, "Uses space as data is written; can be larger than the free space (watch the pool's usage)."))),
      h("label", { class: "check" }, h("input", { type: "radio", name: "prov", checked: !v.thin, onchange: () => { v.thin = false; draw(); } }),
        h("div", {}, h("b", {}, "Thick provisioning"), h("div", { class: "muted" }, "Reserves the full capacity now, so the LUN can never run out of space."))),
      !v.thin && pool && v.size_gb * 1e9 > pool.total - pool.used ? h("div", { class: "note warn" }, "Not enough free space for a thick LUN of this size.") : null,
      h("label", {}, "iSCSI target", h("select", { onchange: (e) => { v.newTarget = e.target.value === "+"; v.target = v.newTarget ? "" : e.target.value; draw(); } },
        h("option", { value: "+", selected: v.newTarget }, "Create a new target for it"),
        h("option", { value: "", selected: !v.newTarget && !v.target }, "Don't map it yet"),
        d.targets.map((t) => h("option", { value: t.name, selected: !v.newTarget && v.target === t.name, translate: "no" }, t.name)))),
      v.newTarget ? h("div", { class: "muted", style: "font-size:12px" }, "A target with the same name is created; any host can connect until you limit it under iSCSI targets.") : null);
  };
  draw();
  const close = modal("Create LUN", h("div", {}, box, err), [
    ["Cancel", () => close()],
    ["Create", async (e) => {
      err.textContent = ""; e.target.disabled = true;
      try {
        if (v.newTarget) { await API.post("/api/san/targets", { name: v.name }); v.target = v.name; }
        await API.post("/api/san/luns", { name: v.name, pool_id: v.pool_id, size_gb: v.size_gb, thin: v.thin, target: v.target });
        close(); toast("LUN created"); done();
      } catch (ex) { err.textContent = ex.message; e.target.disabled = false; }
    }, "primary"]]);
}

async function sanTargets(p) {
  const d = await sanData(p); if (!d) return;
  const reload = () => { p.replaceChildren(); sanTargets(p); };
  p.append(h("div", { class: "toolbar" }, h("h2", { style: "margin:0;flex:1" }, "iSCSI targets"),
      h("button", { class: "btn primary", disabled: !d.available, onclick: () => sanTargetDialog(null, reload, d) }, "Create")),
    d.targets.length ? d.targets.map((t) => h("div", { class: "card", style: "margin-bottom:12px" },
      h("div", {}, h("b", { translate: "no" }, t.name), " ",
        h("span", { class: "pill " + (t.sessions?.length ? "ok" : "") }, t.sessions?.length ? `${t.sessions.length} connected` : "no connections"),
        t.chap ? h("span", { class: "pill info", style: "margin-left:6px" }, "CHAP") : null),
      h("div", { style: "display:flex;gap:8px;align-items:center" }, h("span", { class: "mono muted", style: "font-size:12px", translate: "no" }, t.iqn),
        h("button", { class: "btn", style: "padding:1px 8px;font-size:11px", onclick: () => navigator.clipboard?.writeText(t.iqn).then(() => toast("IQN copied")) }, "Copy")),
      h("div", { class: "muted", style: "font-size:13px" }, "LUNs: ", d.luns.filter((l) => l.target === t.name).map((l) => l.name).join(", ") || "none"),
      h("div", { class: "muted", style: "font-size:13px" }, "Allowed hosts: ", t.initiators.length ? t.initiators.join(", ") : "any initiator"),
      t.sessions?.length ? h("div", { style: "font-size:13px" }, "Connected: ", h("span", { class: "mono" }, t.sessions.join(", "))) : null,
      h("div", { class: "toolbar", style: "margin:6px 0 0" },
        h("button", { class: "btn", onclick: () => sanTargetDialog(t, reload, d) }, "Edit"),
        h("button", { class: "btn danger", onclick: async () => {
          if (!confirm(`Delete target ${t.name}?`)) return;
          await act(() => API.del(`/api/san/targets/${t.name}`), "Target deleted"); reload();
        } }, "Delete"))))
      : h("p", { class: "muted" }, "No targets yet - creating a LUN can create one for you."));
}

const VMWARE_IQN = "iqn.1998-01.com.vmware:";

// Create / edit a target. Long iSCSI names are real, editable text (not placeholders that vanish):
// the target IQN follows the name as you type until you edit it yourself; the initiator list starts
// with VMware's prefix; hosts that already connected to this NAS can be added with one click.
function sanTargetDialog(t, done, d = {}) {
  const prefix = d.iqn_prefix || "iqn.2000-01.org.cynthology:nas";
  const v = { name: t?.name || "", iqn: t?.iqn || `${prefix}.`, iqnEdited: false,
    initiators: t?.initiators?.length ? t.initiators.join("\n") + "\n" : VMWARE_IQN,
    chap_user: t?.chap?.user || "", chap_password: "" };
  const err = h("div", { class: "err" });
  const iqnInput = h("input", { class: "mono", value: v.iqn, readonly: !!t, style: "font-size:13px;width:100%;box-sizing:border-box",
    oninput: (e) => { v.iqn = e.target.value.trim(); v.iqnEdited = true; } });
  const ini = h("textarea", { rows: 4, class: "mono", style: "font-size:13px", spellcheck: "false",
    oninput: (e) => (v.initiators = e.target.value) }, v.initiators);
  const addIni = (iqn) => {
    const lines = ini.value.split(/\s+/).filter((x) => x && x !== VMWARE_IQN && x !== iqn);
    ini.value = v.initiators = [...lines, iqn].join("\n") + "\n";
  };
  // initiators seen on this NAS (connected to any target) - one click to allow them
  const seen = [...new Set((d.targets || []).flatMap((x) => x.sessions || []))];
  const copyBtn = (getText) => h("button", { class: "btn", type: "button", onclick: () => navigator.clipboard?.writeText(getText()).then(() => toast("Copied")) }, "Copy");
  const close = modal(t ? `Edit ${t.name}` : "Create iSCSI target", h("div", { class: "form", style: "padding:0" },
    t ? null : h("label", {}, "Name", h("input", { value: v.name, placeholder: "esx-datastores", oninput: (e) => {
      v.name = e.target.value.trim().toLowerCase();
      if (!v.iqnEdited) { v.iqn = `${prefix}.${v.name}`; iqnInput.value = v.iqn; }
    } })),
    h("label", {}, t ? "Target IQN" : "Target IQN (filled in from the name - edit it if you like)",
      h("div", { class: "toolbar", style: "margin:0;flex-wrap:nowrap" }, h("div", { style: "flex:1" }, iqnInput), copyBtn(() => iqnInput.value))),
    h("label", {}, "Allowed initiators (one per line; leave empty to allow any host)", ini),
    h("div", { class: "muted", style: "font-size:12px;margin-top:-6px" }, "VMware example: ",
      h("span", { class: "mono", translate: "no" }, "iqn.1998-01.com.vmware:esx01-1a2b3c4d"),
      ". Find yours in vSphere: host → Configure → Storage Adapters → iSCSI Software Adapter → iSCSI Name."),
    seen.length ? h("div", { style: "display:flex;gap:6px;flex-wrap:wrap;align-items:center" }, h("span", { class: "muted", style: "font-size:12px" }, "Hosts that connected to this NAS:"),
      seen.map((iqn) => h("button", { class: "btn", type: "button", translate: "no", style: "font-size:12px", onclick: () => addIni(iqn) }, "+ " + iqn))) : null,
    h("label", {}, "CHAP user (optional)", h("input", { value: v.chap_user, autocomplete: "off", oninput: (e) => (v.chap_user = e.target.value.trim()) })),
    h("label", {}, t?.chap ? "CHAP secret (leave empty to keep the current one)" : "CHAP secret (12-16 characters)",
      h("input", { type: "password", autocomplete: "new-password", oninput: (e) => (v.chap_password = e.target.value) })),
    t?.sessions?.length ? h("div", { class: "note warn" }, "Saving briefly disconnects the connected hosts; ESXi reconnects by itself if it is still allowed.") : null,
    err), [
    ["Cancel", () => close()],
    ["Save", async (e) => {
      err.textContent = ""; e.target.disabled = true;
      // a line that is only the VMware prefix (nothing typed after it) doesn't count
      const initiators = v.initiators.split(/\s+/).filter((x) => x && x !== VMWARE_IQN);
      const body = { name: v.name, iqn: t ? "" : v.iqn, initiators, chap_user: v.chap_user, chap_password: v.chap_password };
      try {
        if (t) await API.put(`/api/san/targets/${t.name}`, body); else await API.post("/api/san/targets", body);
        close(); toast("Target saved"); done();
      } catch (ex) { err.textContent = ex.message; e.target.disabled = false; }
    }, "primary"]]);
}

// Snapshots of one LUN: schedule, take now, revert, clone a snapshot as a new LUN.
async function sanLunSnaps(p, l, back) {
  const d = await API.get(`/api/san/luns/${l.name}/snapshots`);
  const reload = () => { p.replaceChildren(); sanLunSnaps(p, l, back); };
  const pol = { ...d.policy };
  const when = (iso) => new Date(iso).toLocaleString();
  p.append(h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: () => { p.replaceChildren(); back(); } }, "← Back"),
      h("h2", { style: "margin:0;flex:1" }, `Snapshots of ${l.name}`),
      h("button", { class: "btn primary", disabled: !d.supported, onclick: async () => { await act(() => API.post(`/api/san/luns/${l.name}/snapshots`), "Snapshot taken"); reload(); } }, "Take snapshot")),
    !d.supported ? h("div", { class: "note warn" }, "Snapshots need the LUN to be on a Btrfs storage pool.") : null,
    h("div", { class: "toolbar", style: "margin:12px 0" },
      h("label", {}, "Schedule ", h("select", { disabled: !d.supported, onchange: (e) => (pol.schedule = e.target.value) },
        [["off", "Off"], ["hourly", "Every hour"], ["daily", "Every day"], ["weekly", "Every week"]].map(([v, t]) => h("option", { value: v, selected: pol.schedule === v }, t)))),
      h("label", {}, "Keep ", h("input", { type: "number", min: 0, max: 1024, value: pol.keep, style: "width:80px", oninput: (e) => (pol.keep = +e.target.value) })),
      h("button", { class: "btn", disabled: !d.supported, onclick: () => act(() => API.put(`/api/san/luns/${l.name}/snapshots/policy`, pol), "Snapshot schedule saved") }, "Save")),
    h("p", { class: "muted", style: "font-size:12px;margin-top:0" },
      "Snapshots are instant and only use space for blocks that change afterwards. They are crash-consistent (like pulling the plug at that moment); ",
      "for application-consistent copies, take a VMware snapshot of the VMs first or quiesce them. Replicate them to another Cynthology in Snapshots → Replication."),
    d.snapshots.length ? h("table", {},
      h("tr", {}, h("th", {}, "Taken"), h("th", {})),
      d.snapshots.map((s) => h("tr", {}, h("td", {}, when(s.time)),
        h("td", { style: "white-space:nowrap;text-align:right" },
          h("button", { class: "btn primary", onclick: async () => {
            const name = prompt("Name of the new LUN (writable copy of this snapshot):", `${l.name}-${s.name.slice(5, 15).replace(/\./g, "")}`); if (!name) return;
            await act(() => API.post(`/api/san/luns/${l.name}/snapshots/${encodeURIComponent(s.name)}/clone`, { new_name: name }),
              `LUN ${name} created - map it to a target to use it`); reload();
          } }, "Clone as new LUN"), " ",
          h("button", { class: "btn", onclick: async () => {
            if (!confirm(`Put the WHOLE LUN ${l.name} back to ${when(s.time)}?\n\nEverything written since then is undone. Hosts must be disconnected first (unmount the datastore). A safety snapshot of the current state is taken first.`)) return;
            const r = await act(() => API.post(`/api/san/luns/${l.name}/snapshots/${encodeURIComponent(s.name)}/revert`), "LUN reverted");
            reload();
          } }, "Revert"), " ",
          h("button", { class: "btn danger", onclick: async () => {
            if (!confirm(`Delete the snapshot of ${when(s.time)}?`)) return;
            await act(() => API.del(`/api/san/luns/${l.name}/snapshots/${encodeURIComponent(s.name)}`), "Snapshot deleted"); reload();
          } }, "Delete")))))
      : h("p", { class: "muted" }, "No snapshots yet."));
}
