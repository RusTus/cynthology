// Storage Manager: overview, volumes (RAID 0/1/5/6/10, SHR, SHR-2), disks + SMART.
const TIER_COLORS = ["#1f7a8c", "#3a9fb0", "#6bbac6", "#9bd0d8"];

WM.register("storage", {
  name: "Storage Manager", icon: "🗄️", color: "#1f7a8c",
  open(body, win) {
    win.show = sectioned({
      overview: { label: "Overview", render: storageOverview },
      volumes: { label: "Storage Pools", render: storagePools },
      disks: { label: "Drives", render: storageDisks },
    }, body);
  },
});

async function storageOverview(p) {
  const [pools, disks] = await Promise.all([API.get("/api/storage/pools"), API.get("/api/storage/disks")]);
  const bad = pools.filter((x) => x.status !== "healthy");
  p.append(h("h2", {}, "Overview"),
    h("div", { class: "note" + (bad.length ? " warn" : "") },
      bad.length ? `${bad.length} storage pool(s) need attention.` : `All storage is healthy. ${pools.length} pool(s), ${disks.length} drive(s).`),
    h("br"),
    h("div", { class: "cards" }, pools.map(poolCard)));
}

function poolCard(x) {
  const pct = x.total ? Math.round((x.used / x.total) * 100) : 0;
  const cls = { healthy: "ok", rebuilding: "warn", degraded: "bad" }[x.status];
  return h("div", { class: "card" },
    h("div", {}, h("b", {}, x.label), " ", h("span", { class: "pill " + cls }, x.status)),
    h("div", { class: "muted" }, `${x.mount} · ${RAID_LABEL[x.raid] || x.raid} · ${(x.fs || "btrfs").toUpperCase()}`),
    h("div", { class: "bar" }, h("div", { style: `width:${pct}%` })),
    h("div", { class: "muted" }, `${fmtBytes(x.used)} used of ${fmtBytes(x.total)}`),
    h("div", { class: "muted" }, `Drives: ${x.disks.join(", ")}`));
}

const RAID_LABEL = { basic: "Basic", raid0: "RAID 0", raid1: "RAID 1", raid5: "RAID 5", raid6: "RAID 6", raid10: "RAID 10", shr: "SHR", shr2: "SHR-2" };
const RAID_HELP = {
  shr: "Mixes drive sizes efficiently. Survives 1 drive failure. Recommended.",
  shr2: "Mixes drive sizes. Survives 2 drive failures. 4+ drives.",
  basic: "No protection. One drive, or drives joined end to end.",
  raid0: "Fastest, no protection. Any failure loses everything.",
  raid1: "Mirror. Survives all but one drive failing.",
  raid5: "Survives 1 drive failure. 3+ drives.",
  raid6: "Survives 2 drive failures. 4+ drives.",
  raid10: "Striped mirrors. Fast, survives 1 failure per mirror pair.",
};

async function storagePools(p) {
  const pools = await API.get("/api/storage/pools");
  p.append(h("h2", {}, "Storage Pools"),
    h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: () => createPoolWizard(p) }, "+ Create")),
    pools.length ? h("table", {},
      h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", {}, "Drives"), h("th", {}, "Capacity"), h("th", {}, "Status"), h("th", {})),
      pools.map((x) => h("tr", {},
        h("td", {}, h("b", {}, x.label), h("div", { class: "muted" }, x.mount)),
        h("td", {}, RAID_LABEL[x.raid], h("div", { class: "muted" }, (x.fs || "btrfs").toUpperCase())),
        h("td", {}, x.disks.join(", ")),
        h("td", {}, fmtBytes(x.total)),
        h("td", {}, h("span", { class: "pill " + ({ healthy: "ok", rebuilding: "warn" }[x.status] || "bad") }, x.status)),
        h("td", {},
          h("button", { class: "btn", onclick: () => addDiskDialog(p, x) }, x.status === "degraded" ? "Repair" : "Add drive"), " ",
          x.fs !== "ext4" ? h("button", { class: "btn", onclick: () => act(() => API.post(`/api/storage/pools/${x.id}/scrub`), "Data scrub started") }, "Scrub") : null))))
      : h("p", { class: "muted" }, "No storage pools yet. Click Create to set up your drives."));
}

async function addDiskDialog(p, pool) {
  const disks = (await API.get("/api/storage/disks")).filter((d) => !d.in_use && !d.system);
  p.replaceChildren(h("h2", {}, `Add drive to ${pool.label}`));
  if (!disks.length) { p.append(h("p", { class: "muted" }, "No unused drives found.")); return; }
  const sel = h("select", {}, disks.map((d) => h("option", { value: d.name }, `${d.name} – ${d.model} (${fmtBytes(d.size)})`)));
  p.append(h("div", { class: "form" },
    h("div", { class: "note" }, pool.status === "degraded"
      ? "The new drive replaces the failed one and the pool rebuilds onto it."
      : "The pool grows onto the new drive. This runs in the background and can take many hours; the volume stays usable."),
    h("label", {}, "Drive", sel),
    h("div", { class: "toolbar" },
      h("button", { class: "btn", onclick: () => { p.replaceChildren(); storagePools(p); } }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => {
        await act(() => API.post(`/api/storage/pools/${pool.id}/add-disk`, { disk: sel.value }), "Drive added");
        p.replaceChildren(); storagePools(p);
      } }, "Add drive"))));
}

async function createPoolWizard(p) {
  const disks = await API.get("/api/storage/disks");
  const free = disks.filter((d) => !d.in_use && !d.system);
  const state = { raid: "shr", disks: new Set(free.map((d) => d.name)), fs: "btrfs", label: "" };
  const raidBox = h("div", { class: "raid-opts" });
  const preview = h("div");

  const refresh = async () => {
    const r = await API.post("/api/storage/plan", { raid: state.raid, disks: [...state.disks] });
    raidBox.replaceChildren(...r.compare.map((c) => h("label", { class: "raid-opt" + (c.ok ? "" : " bad") },
      h("input", { type: "radio", name: "raid", value: c.layout, checked: c.layout === state.raid, disabled: !c.ok,
        onchange: () => { state.raid = c.layout; refresh(); } }),
      h("div", {}, h("b", {}, RAID_LABEL[c.layout]), h("div", { class: "muted" }, c.ok ? RAID_HELP[c.layout] : c.error)),
      h("span", { class: "cap" }, c.ok ? fmtBytes(c.usable) : "–"))));
    preview.replaceChildren();
    if (r.plan) {
      const total = r.plan.raw || 1;
      preview.append(
        h("div", { class: "muted" }, `Usable ${fmtBytes(r.plan.usable)} of ${fmtBytes(r.plan.raw)} raw · survives ${r.plan.tolerance} drive failure(s)`),
        r.plan.tiers.length > 1 ? h("div", {}, h("div", { class: "muted" }, "SHR layout (one RAID array per drive-size tier, joined with LVM):"),
          h("div", { class: "tiers" }, r.plan.tiers.map((t, i) => h("div", {
            style: `flex:${t.usable / total};background:${TIER_COLORS[i % 4]}`,
            title: t.members.join(", ") }, `${t.level === "1" ? "mirror" : "RAID " + t.level} · ${fmtBytes(t.usable)}`)))) : null,
        ...r.plan.warnings.map((w) => h("div", { class: "note warn" }, w)));
    }
  };

  p.replaceChildren(h("h2", {}, "Create storage pool"),
    h("div", { class: "form" },
      h("label", {}, "Name", h("input", { placeholder: "Volume 1", oninput: (e) => (state.label = e.target.value) })),
      h("div", {}, h("div", { class: "muted" }, "Drives (all data on selected drives will be erased)"),
        h("div", { class: "disk-pick" }, free.length ? free.map((d) => h("label", {},
          h("input", { type: "checkbox", checked: true, onchange: (e) => { e.target.checked ? state.disks.add(d.name) : state.disks.delete(d.name); refresh(); } }),
          h("span", {}, `${d.name} – ${d.model || "Unknown"}`), h("span", { class: "muted" }, fmtBytes(d.size)),
          h("span", { class: "pill" }, d.rota ? "HDD" : "SSD"))) : h("p", { class: "muted" }, "No unused drives."))),
      h("div", {}, h("div", { class: "muted" }, "RAID type (usable capacity shown for the selected drives)"), raidBox),
      preview,
      h("label", {}, "File system", h("select", { onchange: (e) => (state.fs = e.target.value) },
        h("option", { value: "btrfs" }, "Btrfs (recommended: snapshots, checksums, compression)"),
        h("option", { value: "ext4" }, "ext4"))),
      h("div", { class: "toolbar" },
        h("button", { class: "btn", onclick: () => { p.replaceChildren(); storagePools(p); } }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => {
          if (!confirm(`Erase ${[...state.disks].join(", ")} and create ${RAID_LABEL[state.raid]}?`)) return;
          await act(() => API.post("/api/storage/pools", { raid: state.raid, disks: [...state.disks], label: state.label, fs: state.fs }), "Storage pool created");
          p.replaceChildren(); storagePools(p);
        } }, "Create"))));
  refresh();
}

async function storageDisks(p) {
  const disks = await API.get("/api/storage/disks");
  const tbl = h("table", {}, h("tr", {}, h("th", {}, "Drive"), h("th", {}, "Model"), h("th", {}, "Size"), h("th", {}, "Type"), h("th", {}, "Status"), h("th", {}, "Health")));
  for (const d of disks) {
    const health = h("td", {}, h("button", { class: "btn", onclick: async () => {
      const s = await API.get(`/api/storage/disks/${d.name}/smart`);
      if (!s.supported) return health.replaceChildren(h("span", { class: "pill" }, "SMART not available"), h("div", { class: "muted" }, "Virtual disks and some USB enclosures don't report health"));
      health.replaceChildren(h("span", { class: "pill " + (s.passed ? "ok" : "bad") }, s.passed ? "Healthy" : "Failing"),
        h("div", { class: "muted" }, `${s.temperature ?? "?"}°C · ${s.power_on_hours ?? "?"} h · realloc ${s.reallocated ?? "?"}`));
    } }, "Check"));
    tbl.append(h("tr", {}, h("td", {}, d.path), h("td", {}, d.model, h("div", { class: "muted" }, d.serial || "")),
      h("td", {}, fmtBytes(d.size)), h("td", {}, d.rota ? "HDD" : "SSD"),
      h("td", {}, d.system ? "System" : d.in_use ? "In use" : "Not initialised"), health));
  }
  p.append(h("h2", {}, "Drives"), tbl);
}

// Used by the setup wizard: jump straight to "Create storage pool".
function openCreatePool() {
  WM.open("storage");
  const w = WM.wins.storage;
  w.show("volumes");
  createPoolWizard(w.show.pane);
}
