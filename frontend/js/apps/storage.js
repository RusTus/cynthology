// Storage Manager: overview, volumes (RAID 0/1/5/6/10, SHR, SHR-2), disks + SMART.
const TIER_COLORS = ["#1f7a8c", "#3a9fb0", "#6bbac6", "#9bd0d8"];

WM.register("storage", {
  name: "Storage Manager", icon: "🗄️", color: "#1f7a8c", w: 1000, h: 640,
  open(body, win) {
    const side = h("nav", { class: "side tree" });
    const pane = h("div", { class: "pane" });
    body.append(side, pane);
    let current = "overview";
    const item = (key, label, opts = {}) => h("button", { "data-k": key, class: (current === key ? "on " : "") + (opts.sub ? "sub" : ""), onclick: () => go(key) },
      opts.icon ? svgIcon(opts.icon, 16) : null, h("span", { style: "flex:1" }, label),
      opts.dot ? h("span", { class: "dot-status " + opts.dot }) : null);
    async function drawSide() {
      let pools = [];
      try { pools = await API.get("/api/storage/pools"); } catch { /* keep going */ }
      side.replaceChildren(
        item("overview", "Overview", { icon: "cp_info" }),
        item("pools", "Storage", { icon: "storage" }),
        ...pools.map((x) => item(`pool:${x.id}`, x.label, { sub: true, dot: statusClass(x.status) })),
        item("drives", "Drives", { icon: "hdd" }));
    }
    function go(key) {
      current = key;
      const box = h("div");
      pane.replaceChildren(box);
      drawSide();
      if (key === "overview") storageOverview(box, go);
      else if (key === "pools") storagePools(box);
      else if (key === "drives") storageDisks(box, go);
      else if (key.startsWith("pool:")) poolDetail(box, +key.split(":")[1], go);
    }
    win.show = (k) => go({ volumes: "pools", disks: "drives" }[k] || k);
    win.show.pane = pane;
    go("overview");
  },
});

// Erase drives that hold data from elsewhere (an earlier installation, another NAS...).
async function eraseDrives(names, after) {
  const typed = prompt(`Erase ${names.join(", ")}?\n\nEverything on ${names.length > 1 ? "these drives" : "this drive"} is permanently deleted ` +
    `(including any storage pool on ${names.length > 1 ? "them" : "it"}).\n\nType ERASE to confirm:`);
  if (typed === null) return;
  if (typed.trim() !== "ERASE") return toast("Not erased - you need to type ERASE");
  toast(`Erasing ${names.join(", ")}…`);
  await act(() => API.post("/api/storage/disks/erase", { disks: names, confirm: "ERASE" }), `${names.join(", ")} erased - ready for a new storage pool`);
  after();
}

// Pools found on the drives that this installation doesn't know yet (reinstall, drives moved from another box).
function foreignCard(f, go) {
  return h("div", { class: "health-hero warn", style: "margin-bottom:14px;align-items:flex-start" },
    h("div", { class: "hero-icon" }, svgIcon("storage", 26)),
    h("div", { style: "flex:1;display:grid;gap:6px" },
      h("div", { class: "hero-title" }, `Existing storage pool found: ${f.label}`),
      h("div", { class: "muted" }, `${(RAID_LABEL[f.raid] || f.raid)} · ${fmtBytes(f.size)} · ${f.fs || "unknown file system"} · drives ${f.disks.join(", ")}`,
        f.status !== "healthy" ? h("span", { class: "pill warn", style: "margin-left:8px" }, f.status) : null),
      h("div", {}, f.importable
        ? "These drives hold a storage pool from an earlier Cynthology installation. Import it to get its data and shared folders back, or erase the drives to start fresh."
        : `It can't be imported: ${f.why_not}. You can erase the drives to use them again.`),
      h("div", { class: "toolbar", style: "margin:4px 0 0" },
        f.importable ? h("button", { class: "btn primary", onclick: async (e) => {
          e.target.disabled = true; e.target.textContent = "Importing…";
          try {
            const r = await API.post(`/api/storage/foreign/${f.vg}/import`);
            toast(`Imported ${r.pool.label}` + (r.shares.length ? ` with shared folder(s): ${r.shares.join(", ")}` : ""));
            go("overview");
          } catch (ex) { toast("Error: " + ex.message); e.target.disabled = false; e.target.textContent = "Import"; }
        } }, "Import") : null,
        h("button", { class: "btn danger", onclick: () => eraseDrives(f.disks, () => go("overview")) }, "Erase drives"))));
}

async function storageOverview(p, go) {
  const [pools, disks, foreign] = await Promise.all([API.get("/api/storage/pools"), API.get("/api/storage/disks"),
    API.get("/api/storage/foreign").catch(() => [])]);
  p.append(...foreign.map((f) => foreignCard(f, go)));
  const loose = disks.filter((d) => d.usage === "foreign" && !foreign.some((f) => f.disks.includes(d.name)));
  if (loose.length) p.append(h("div", { class: "note warn", style: "margin-bottom:14px" },
    `${loose.map((d) => d.name).join(", ")} contain${loose.length > 1 ? "" : "s"} data from another system. Erase ${loose.length > 1 ? "them" : "it"} under Drives to use ${loose.length > 1 ? "them" : "it"} in a storage pool. `,
    h("button", { class: "btn", onclick: () => go("drives") }, "Go to Drives")));
  const bad = pools.filter((x) => statusClass(x.status) === "bad" || statusClass(x.status) === "warn");
  const total = pools.reduce((a, x) => a + (x.total || 0), 0), used = pools.reduce((a, x) => a + (x.used || 0), 0);
  p.append(h("div", { class: "health-hero " + (bad.length ? "warn" : pools.length ? "ok" : "") },
      h("div", { class: "hero-icon" }, bad.length ? "!" : "✓"),
      h("div", {}, h("div", { class: "hero-title" }, bad.length ? "Attention needed" : pools.length ? "Healthy" : "No storage yet"),
        h("div", { class: "muted" }, bad.length ? `${bad.length} storage pool(s) need attention.` : pools.length ? "All storage pools and drives are working well." : "Create a storage pool to start using your drives."))),
    pools.length ? h("div", { class: "stat-row" },
      h("div", { class: "stat" }, h("div", { class: "stat-num" }, fmtBytes(total)), h("div", { class: "muted" }, "total capacity")),
      h("div", { class: "stat" }, h("div", { class: "stat-num" }, fmtBytes(used)), h("div", { class: "muted" }, "used")),
      h("div", { class: "stat" }, h("div", { class: "stat-num" }, String(pools.length)), h("div", { class: "muted" }, "storage pool(s)")),
      h("div", { class: "stat" }, h("div", { class: "stat-num" }, String(disks.filter((d) => !d.system).length)), h("div", { class: "muted" }, "data drive(s)"))) : null,
    h("div", { class: "cards", style: "margin-top:14px" }, pools.map(poolCard)));
}

// One storage pool: summary, info, data scrubbing, drives.
async function poolDetail(p, id, go) {
  const [pools, disks] = await Promise.all([API.get("/api/storage/pools"), API.get("/api/storage/disks")]);
  const x = pools.find((q) => q.id === id);
  if (!x) return p.append(h("p", { class: "muted" }, "This storage pool no longer exists."));
  const scrub = await API.get(`/api/storage/pools/${id}/scrub`).catch(() => ({ supported: false }));
  // unused space on bigger drives (after replacing drives): offer to put it to work
  const expandBanner = h("div");
  API.get(`/api/storage/pools/${id}/expansion`).then((e) => {
    if (!e.gain) return;
    expandBanner.replaceChildren(h("div", { class: "note", style: "margin-bottom:12px" },
      h("b", {}, `${fmtBytes(e.gain)} ${t("of unused space can be added to this pool")}`), " ",
      t("- the drives are bigger than the pool uses. Growing runs in the background; the volume stays usable."), " ",
      h("button", { class: "btn primary", disabled: !!e.busy, title: e.busy ? t("Wait until the rebuild/reshape has finished") : "", onclick: async () => {
        if (!confirm(t("Grow the pool into the unused space now?"))) return;
        await act(() => API.post(`/api/storage/pools/${id}/expand`), "Expanding - this runs in the background"); reload();
      } }, "Expand")));
  }).catch(() => {});
  const pct = x.total ? Math.round((x.used / x.total) * 100) : 0;
  const cls = statusClass(x.status);
  const tol = { basic: "none", raid0: "none", raid1: "all but one drive", raid5: "1 drive", raid6: "2 drives", raid10: "1 drive per mirror pair", shr: "1 drive", shr2: "2 drives" }[x.raid];
  const byName = Object.fromEntries(disks.map((d) => [d.name, d]));
  const rows = [...x.disks.map((d) => [d, "Member"]), ...(x.spares || []).map((d) => [d, "Hot spare"]), ...(x.failed || []).map((d) => [d, "Failed"])];
  const reload = () => go(`pool:${id}`);
  const row = (k, v) => h("tr", {}, h("th", { style: "width:200px" }, k), h("td", {}, v));
  const scrubText = !scrub.supported ? "Not available (ext4)" : scrub.state === "running" ? `Running… ${scrub.rate || ""}` :
    scrub.state === "finished" ? `Finished ${scrub.started || ""} · ${scrub.errors || ""}` : scrub.state === "aborted" ? "Stopped before finishing" : "Never run";
  p.append(
    h("div", { class: "pool-head" },
      h("div", { class: "pool-icon " + cls }, appTile("storage", 44)),
      h("div", { style: "flex:1;min-width:0" },
        h("div", {}, h("b", { style: "font-size:17px" }, x.label), "  ", h("span", { class: "pill " + cls }, x.status)),
        h("div", { class: "muted" }, `${RAID_LABEL[x.raid]} · ${(x.fs || "btrfs").toUpperCase()} · ${x.mount}`),
        h("div", { class: "bar", style: "margin-top:8px" }, h("div", { style: `width:${pct}%` })),
        h("div", { class: "muted", style: "font-size:12px;margin-top:4px" }, `${fmtBytes(x.used)} used of ${fmtBytes(x.total)} (${pct}%)`))),
    h("div", { class: "toolbar", style: "margin:14px 0" },
      h("button", { class: "btn" + (x.status === "degraded" ? " primary" : ""), onclick: () => addDiskDialog(p, x) }, x.status === "degraded" ? "Repair" : "Add drive"),
      ["raid0", "basic"].includes(x.raid) ? null : h("button", { class: "btn", onclick: () => addDiskDialog(p, x, true) }, "Add hot spare"),
      x.failed?.length ? h("button", { class: "btn", onclick: async () => { await act(() => API.post(`/api/storage/pools/${id}/remove-failed`), "Failed drive detached - you can pull it now"); reload(); } }, "Detach failed drive") : null,
      (x.raid === "raid1" && x.disks.length === 2) || x.raid === "raid5" ? h("button", { class: "btn", onclick: () => changeRaidDialog(x, disks, reload) }, "Change RAID type") : null,
      h("span", { style: "flex:1" }),
      h("button", { class: "btn danger", onclick: () => {
        const name = h("input", { placeholder: x.label });
        const close = modal(t("Remove storage pool") + " " + x.label, h("div", { class: "form" },
          h("div", { class: "note warn" }, t("Everything on this pool is erased and its drives become empty. This can't be undone. (Not possible while shared folders, LUNs, the Backup Vault or Docker still use it.)")),
          h("label", {}, t("Type the pool's name to confirm"), name)), [["Cancel", null, ""], ["Remove pool", async () => {
            await act(() => API.call("DELETE", `/api/storage/pools/${id}`, { confirm: name.value }), "Storage pool removed");
            close(); go("overview");
          }, "danger"]]);
      } }, "Remove pool")),
    expandBanner,
    h("h3", { class: "section-title" }, "Info"),
    h("table", { class: "kv" },
      row("RAID type", `${RAID_LABEL[x.raid]} - survives ${tol} failing`),
      row("File system", (x.fs || "btrfs") === "btrfs" ? "Btrfs (snapshots, checksums, compression)" : "ext4"),
      row("Capacity", `${fmtBytes(x.total)} (${fmtBytes(x.total - x.used)} free)`),
      row("Location", h("span", { class: "mono" }, x.mount))),
    h("h3", { class: "section-title" }, "SSD cache"),
    cacheSection(x, disks, reload),
    h("h3", { class: "section-title" }, "Data scrubbing"),
    h("table", { class: "kv" },
      row("Status", scrubText),
      scrub.duration ? row("Duration", scrub.duration) : null,
      scrub.supported ? row("Schedule", h("select", { onchange: (e) => act(() => API.put("/api/storage/scrub-schedule", { schedule: e.target.value }), "Schedule saved") },
        [["off", "Off"], ["monthly", "Every month (recommended)"], ["quarterly", "Every 3 months"]].map(([v, l]) => h("option", { value: v, selected: scrub.schedule === v }, l)))) : null,
      scrub.supported ? row("", h("button", { class: "btn", disabled: scrub.state === "running", onclick: async () => { await act(() => API.post(`/api/storage/pools/${id}/scrub`), "Data scrubbing started"); reload(); } }, "Run now")) : null),
    h("div", { class: "muted", style: "font-size:12px" }, "Scrubbing reads all data and repairs silent corruption from the RAID redundancy."),
    h("h3", { class: "section-title" }, "Drives"),
    h("table", {},
      h("tr", {}, h("th", {}, "Drive"), h("th", {}, "Model"), h("th", {}, "Size"), h("th", {}, "Role"), h("th", {}, "")),
      rows.map(([d, role]) => h("tr", {},
        h("td", {}, h("span", { style: "display:inline-flex;gap:8px;align-items:center" }, svgIcon(byName[d]?.rota === false ? "ssd" : "hdd", 18), h("b", {}, d))),
        h("td", { class: "muted" }, byName[d]?.model || ""),
        h("td", {}, byName[d] ? fmtBytes(byName[d].size) : ""),
        h("td", {}, h("span", { class: "pill " + (role === "Failed" ? "bad" : role === "Hot spare" ? "info" : "ok") }, role)),
        h("td", {}, role === "Hot spare" ? h("button", { class: "btn", onclick: async () => {
          if (!confirm(`Remove hot spare ${d}?`)) return;
          await act(() => API.post(`/api/storage/pools/${id}/spare/remove`, { disk: d }), "Hot spare removed"); reload();
        } }, "Remove") : role === "Member" && !["raid0", "basic"].includes(x.raid) ? h("button", { class: "btn", title: t("Swap this drive for another (e.g. bigger) one without losing protection"),
          onclick: () => replaceDialog(x, d, disks, reload) }, "Replace") : null)))));
}

// Replace a working drive (hot replace: the pool stays protected while it copies).
function replaceDialog(pool, old, disks, reload) {
  const oldSize = disks.find((d) => d.name === old)?.size || 0;
  const free = disks.filter((d) => d.usage === "free" && !d.system && d.size >= oldSize);
  if (!free.length) return toast(t("Connect a free drive at least as large as") + ` ${old} (${fmtBytes(oldSize)})`);
  const sel = h("select", {}, free.map((d) => h("option", { value: d.name }, `${d.name} - ${d.model || ""} (${fmtBytes(d.size)})`)));
  const close = modal(t("Replace") + " " + old, h("div", { class: "form" },
    h("div", { class: "muted", style: "font-size:13px" }, t("Everything on the new drive is erased. The data is copied over while the pool stays fully protected; then the old drive is free to remove. If all drives get bigger, use Expand afterwards to use the extra space.")),
    h("label", {}, "New drive", sel)), [["Cancel", null, ""], ["Replace", async () => {
      const r = await act(() => API.post(`/api/storage/pools/${pool.id}/replace`, { old, new: sel.value }));
      close(); if (r) toast(r.note); reload();
    }, "primary"]]);
}

function changeRaidDialog(pool, disks, reload) {
  const target = pool.raid === "raid1" ? "raid5" : "raid6";
  const big = Math.max(...pool.disks.map((n) => disks.find((d) => d.name === n)?.size || 0));
  const free = disks.filter((d) => d.usage === "free" && !d.system && d.size >= big);
  const text = target === "raid5" ? t("RAID 1 → RAID 5: a third drive is added and the pool grows by one drive's capacity. It still survives one failing drive.")
    : t("RAID 5 → RAID 6: one more drive is added; the pool then survives two failing drives (capacity stays the same).");
  if (!free.length) return toast(t("Connect a free drive at least as large as the pool's largest drive"));
  const sel = h("select", {}, free.map((d) => h("option", { value: d.name }, `${d.name} - ${d.model || ""} (${fmtBytes(d.size)})`)));
  const close = modal(t("Change RAID type"), h("div", { class: "form" }, h("div", { class: "muted", style: "font-size:13px" }, text,
    " ", t("The conversion runs in the background and can take many hours; the volume stays usable.")), h("label", {}, "Drive to add", sel)), [
    ["Cancel", null, ""], ["Convert", async () => {
      const r = await act(() => API.post(`/api/storage/pools/${pool.id}/change-raid`, { level: target, disk: sel.value }));
      close(); if (r) toast(r.note); reload();
    }, "primary"]]);
}

// SSD cache: read-only (1-4 drives, safe) or read-write (2 mirrored drives).
function cacheSection(x, disks, reload) {
  const c = x.cache;
  const row = (k, v) => h("tr", {}, h("th", { style: "width:200px" }, k), h("td", {}, v));
  if (c) {
    return h("div", {}, h("table", { class: "kv" },
      row("Mode", c.mode === "writeback" ? "Read-write" : "Read-only"),
      row("Layout", c.disks.length < 2 ? "Single drive" : c.level === "0" ? "Striped (RAID 0) - no redundancy" : "Mirrored (RAID 1) - redundant"),
      row("Drives", c.disks.join(", ")),
      row("Size", fmtBytes(c.size || 0)),
      row("Cache in use", `${c.used_pct ?? 0}%`),
      row("Read hit rate", c.hit_rate == null ? "no reads yet" : `${c.hit_rate}%`),
      c.mode === "writeback" ? row("Not yet written to drives", c.dirty ? `${c.dirty} block(s)` : "nothing") : null,
      row("", h("button", { class: "btn danger", onclick: async (e) => {
        if (!confirm(`Remove the SSD cache from ${x.label}?` + (c.mode === "writeback" ? "\n\nCached writes are first copied to the drives; this can take a few minutes." : ""))) return;
        e.target.disabled = true; e.target.textContent = "Removing…";
        const r = await act(() => API.del(`/api/storage/pools/${x.id}/cache`), "SSD cache removed");
        if (r?.warning) toast(r.warning);
        reload();
      } }, "Remove SSD cache"))));
  }
  const free = disks.filter((d) => d.usage === "free");
  return h("div", {}, h("p", { class: "muted", style: "margin:0 0 8px" },
      "An SSD cache keeps frequently used data on fast drives, which speeds up random reads (databases, virtual machines, many small files)."),
    h("button", { class: "btn", disabled: !free.length, title: free.length ? "" : "No unused drives", onclick: () => cacheWizard(x, free, reload) }, "Create SSD cache"));
}

function cacheWizard(x, free, reload) {
  const st = { mode: "readonly", mirror: true, disks: new Set(free.filter((d) => !d.rota).slice(0, 1).map((d) => d.name)) };
  const err = h("div", { class: "err" });
  const box = h("div", { class: "form" });
  const draw = () => {
    const n = st.disks.size;
    const chosen = free.filter((d) => st.disks.has(d.name));
    const mirrored = st.mode === "readwrite" || st.mirror || n < 2;
    const size = mirrored ? Math.min(...chosen.map((d) => d.size)) : chosen.reduce((a, d) => a + d.size, 0);
    box.replaceChildren(
      h("label", { class: "check" }, h("input", { type: "radio", name: "cm", checked: st.mode === "readonly", onchange: () => { st.mode = "readonly"; draw(); } }),
        h("div", {}, h("b", {}, "Read-only cache"), h("div", { class: "muted" }, "Speeds up reads. All data also stays on the pool's drives, so a failing SSD never loses data. 1–4 drives; mirror them to keep the cache running when one fails."))),
      h("label", { class: "check" }, h("input", { type: "radio", name: "cm", checked: st.mode === "readwrite", onchange: () => { st.mode = "readwrite"; draw(); } }),
        h("div", {}, h("b", {}, "Read-write cache"), h("div", { class: "muted" }, "Speeds up reads and writes. Needs exactly 2 drives, mirrored, because writes live on the SSDs until they are copied to the pool."))),
      h("div", { class: "muted" }, "Drives (everything on them is erased):"),
      h("div", { class: "disk-pick" }, free.map((d) => h("label", {},
        h("input", { type: "checkbox", checked: st.disks.has(d.name), onchange: (e) => { e.target.checked ? st.disks.add(d.name) : st.disks.delete(d.name); draw(); } }),
        h("span", {}, `${d.name} – ${d.model || "Unknown"}`), h("span", { class: "muted" }, fmtBytes(d.size)),
        h("span", { class: "pill" + (d.rota ? " warn" : " ok") }, d.rota ? "HDD" : "SSD")))),
      chosen.some((d) => d.rota) ? h("div", { class: "note warn" }, "A hard drive as cache gives little or no speed-up. (Virtual disks in VMware often report as HDD even on an SSD; that's fine for testing.)") : null,
      st.mode === "readwrite" && n !== 2 ? h("div", { class: "note warn" }, "Choose exactly 2 drives for a read-write cache.") : null,
      st.mode === "readonly" && n >= 2 ? h("div", {},
        h("label", { class: "check" }, h("input", { type: "radio", name: "cl", checked: st.mirror, onchange: () => { st.mirror = true; draw(); } }),
          h("div", {}, h("b", {}, "Mirror (RAID 1)"), h("div", { class: "muted" }, "Redundant: the cache keeps working if an SSD fails. Size of one drive."))),
        h("label", { class: "check" }, h("input", { type: "radio", name: "cl", checked: !st.mirror, onchange: () => { st.mirror = false; draw(); } }),
          h("div", {}, h("b", {}, "Stripe (RAID 0)"), h("div", { class: "muted" }, "More cache space (all drives added up), no redundancy. If an SSD fails, the pool can show read errors until you remove the cache; no data is lost.")))) : null,
      st.mode === "readwrite" ? h("div", { class: "muted" }, "Layout: mirrored (RAID 1) - required for a read-write cache.") : null,
      n ? h("div", {}, `Cache size: `, h("b", {}, fmtBytes(size))) : null);
  };
  draw();
  modal(`Create SSD cache for ${x.label}`, h("div", {}, box, err), [
    ["Cancel", null],
    ["Create", async (e) => {
      err.textContent = "";
      if (!st.disks.size) return (err.textContent = "Choose at least one drive.");
      if (!confirm(`Erase ${[...st.disks].join(", ")} and use ${st.disks.size > 1 ? "them" : "it"} as SSD cache for ${x.label}?`)) return;
      e.target.disabled = true; e.target.textContent = "Creating…";
      try {
        await API.post(`/api/storage/pools/${x.id}/cache`, { disks: [...st.disks], mode: st.mode, mirror: st.mirror });
        document.querySelectorAll(".wiz").forEach((w) => w.remove());
        toast("SSD cache created"); reload();
      } catch (ex) { err.textContent = ex.message; e.target.disabled = false; e.target.textContent = "Create"; }
    }, "primary"]]);
}

function poolCard(x) {
  const pct = x.total ? Math.round((x.used / x.total) * 100) : 0;
  const cls = statusClass(x.status);
  return h("div", { class: "card" },
    h("div", {}, h("b", {}, x.label), " ", h("span", { class: "pill " + cls }, x.status)),
    h("div", { class: "muted" }, `${x.mount} · ${RAID_LABEL[x.raid] || x.raid} · ${(x.fs || "btrfs").toUpperCase()}`),
    h("div", { class: "bar" }, h("div", { style: `width:${pct}%` })),
    h("div", { class: "muted" }, `${fmtBytes(x.used)} used of ${fmtBytes(x.total)}`),
    h("div", { class: "muted" }, `Drives: ${x.disks.join(", ")}`),
    x.spares?.length ? h("div", { class: "muted" }, `Hot spare: ${x.spares.join(", ")}`) : null,
    x.failed?.length ? h("div", { class: "err" }, `Failed: ${x.failed.join(", ")}`) : null);
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
        h("td", {}, x.disks.join(", "),
          x.spares?.length ? h("div", { class: "muted" }, "Hot spare: ", x.spares.map((d) => h("span", {}, d, " ",
            h("a", { href: "#", title: "Remove hot spare", onclick: async (e) => {
              e.preventDefault();
              if (!confirm(`Remove hot spare ${d} from ${x.label}?`)) return;
              await act(() => API.post(`/api/storage/pools/${x.id}/spare/remove`, { disk: d }), "Hot spare removed"); p.replaceChildren(); storagePools(p);
            } }, "✕")))) : null,
          x.failed?.length ? h("div", { class: "err" }, `Failed: ${x.failed.join(", ")} `,
            h("a", { href: "#", onclick: async (e) => { e.preventDefault(); await act(() => API.post(`/api/storage/pools/${x.id}/remove-failed`), "Failed drive detached - you can pull it now"); p.replaceChildren(); storagePools(p); } }, "Detach")) : null),
        h("td", {}, fmtBytes(x.total)),
        h("td", {}, h("span", { class: "pill " + statusClass(x.status) }, x.status),
          x.status.startsWith("optimizing") ? h("div", { class: "muted", style: "font-size:12px" }, "first sync - the pool is ready to use") : null,
          x.status.startsWith("expanding") ? h("div", { class: "muted", style: "font-size:12px" }, "growing onto the new drive - usable meanwhile") : null),
        h("td", {},
          h("button", { class: "btn", onclick: () => addDiskDialog(p, x) }, x.status === "degraded" ? "Repair" : "Add drive"), " ",
          ["raid0", "basic"].includes(x.raid) ? null : h("button", { class: "btn", onclick: () => addDiskDialog(p, x, true) }, "Add hot spare"), " ",
          x.fs !== "ext4" ? h("button", { class: "btn", onclick: () => act(() => API.post(`/api/storage/pools/${x.id}/scrub`), "Data scrub started") }, "Scrub") : null))))
      : h("p", { class: "muted" }, "No storage pools yet. Click Create to set up your drives."));
}

async function addDiskDialog(p, pool, spare = false) {
  const disks = (await API.get("/api/storage/disks")).filter((d) => !d.in_use && !d.system);
  p.replaceChildren(h("h2", {}, spare ? `Add hot spare to ${pool.label}` : `Add drive to ${pool.label}`));
  if (!disks.length) { p.append(h("p", { class: "muted" }, "No unused drives found.")); return; }
  const sel = h("select", {}, disks.map((d) => h("option", { value: d.name }, `${d.name} – ${d.model} (${fmtBytes(d.size)})`)));
  p.append(h("div", { class: "form" },
    h("div", { class: "note" }, spare
      ? "The drive stands by unused. If a drive in the pool fails, the pool automatically rebuilds onto it - no action needed. It must be at least as large as the pool's largest drive."
      : pool.status === "degraded"
      ? "The new drive replaces the failed one and the pool rebuilds onto it."
      : "The pool grows onto the new drive. This runs in the background and can take many hours; the volume stays usable."),
    h("label", {}, "Drive", sel),
    h("div", { class: "toolbar" },
      h("button", { class: "btn", onclick: () => { p.replaceChildren(); storagePools(p); } }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => {
        await act(() => API.post(`/api/storage/pools/${pool.id}/${spare ? "spare" : "add-disk"}`, { disk: sel.value }), spare ? "Hot spare added" : "Drive added");
        p.replaceChildren(); storagePools(p);
      } }, spare ? "Add hot spare" : "Add drive"))));
}

async function createPoolWizard(p) {
  const disks = await API.get("/api/storage/disks");
  const free = disks.filter((d) => !d.in_use && !d.system);
  const leftover = disks.filter((d) => d.usage === "foreign");
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
          h("span", { class: "pill" }, d.rota ? "HDD" : "SSD"))) : h("p", { class: "muted" }, "No unused drives.")),
        leftover.length ? h("div", { class: "note warn", style: "margin-top:8px" },
          `${leftover.map((d) => d.name).join(", ")} still hold${leftover.length > 1 ? "" : "s"} data (e.g. a storage pool from an earlier installation). `,
          "To keep that data, import the pool on the Overview page. To use the drive(s) here, erase them first. ",
          h("button", { class: "btn danger", onclick: () => eraseDrives(leftover.map((d) => d.name), () => createPoolWizard(p)) }, "Erase " + leftover.map((d) => d.name).join(", "))) : null),
      h("div", {}, h("div", { class: "muted" }, "RAID type (usable capacity shown for the selected drives)"), raidBox),
      preview,
      h("label", {}, "File system", h("select", { onchange: (e) => (state.fs = e.target.value) },
        h("option", { value: "btrfs" }, "Btrfs (recommended: snapshots, checksums, compression)"),
        h("option", { value: "ext4" }, "ext4"))),
      h("div", { class: "toolbar" },
        h("button", { class: "btn", onclick: () => { p.replaceChildren(); storagePools(p); } }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => {
          if (!confirm(`Erase ${[...state.disks].join(", ")} and create ${RAID_LABEL[state.raid]}?`)) return;
          let r;
          try { r = await API.post("/api/storage/pools", { raid: state.raid, disks: [...state.disks], label: state.label, fs: state.fs }); }
          catch (e) { return toast("Error: " + e.message); }
          poolProgress(p, r.job, `${RAID_LABEL[state.raid]} on ${[...state.disks].join(", ")}`);
        } }, "Create"))));
  refresh();
}

async function storageDisks(p, go) {
  const [disks, pools] = await Promise.all([API.get("/api/storage/disks"), API.get("/api/storage/pools")]);
  const role = {};
  for (const x of pools) {
    x.disks.forEach((d) => (role[d] = [`${x.label}`, "ok"]));
    (x.spares || []).forEach((d) => (role[d] = [`Hot spare · ${x.label}`, "info"]));
    (x.failed || []).forEach((d) => (role[d] = [`Failed · ${x.label}`, "bad"]));
  }
  p.append(h("h2", {}, "Drives"), h("div", { class: "drive-list" }, disks.map((d) => {
    const [r, cls] = d.system ? ["System drive", "info"] : role[d.name] || (d.usage === "pool" ? [d.pool, "ok"]
      : d.usage === "foreign" ? ["Contains data from another system or an earlier installation", "warn"] : ["Not used - available for a pool", ""]);
    const details = h("div", { class: "drive-details hidden" });
    const rowEl = h("div", { class: "drive-row" },
      h("div", { class: "drive-main", onclick: async () => {
        details.classList.toggle("hidden");
        if (details.dataset.loaded) return;
        details.dataset.loaded = 1;
        details.replaceChildren(h("span", { class: "muted" }, "Reading health data…"));
        const s = await API.get(`/api/storage/disks/${d.name}/smart`).catch(() => ({ supported: false }));
        const kv = (k, v) => h("div", {}, h("span", { class: "muted" }, k + ": "), v);
        details.replaceChildren(kv("Device", h("span", { class: "mono" }, d.path)), kv("Serial", d.serial || "-"), kv("Type", d.rota ? "Hard drive" : "SSD"),
          s.supported ? [kv("SMART", h("span", { class: "pill " + (s.passed ? "ok" : "bad") }, s.passed ? "Passed" : "FAILING")),
            kv("Temperature", s.temperature != null ? `${s.temperature} °C` : "-"), kv("Power-on hours", s.power_on_hours ?? "-"),
            kv("Reallocated sectors", s.reallocated ?? "-"),
            kv("Last self-test", s.test_running_remaining_pct != null ? `running (${100 - s.test_running_remaining_pct}% done)`
              : s.last_test ? h("span", { class: s.last_test.passed ? "" : "err" }, `${s.last_test.type}: ${s.last_test.result}${s.last_test.hours ? ` (at ${s.last_test.hours} h)` : ""}`) : "never"),
            h("div", { class: "toolbar", style: "margin:6px 0 0" },
              h("button", { class: "btn", onclick: async (e) => { e.stopPropagation(); const r = await act(() => API.post(`/api/storage/disks/${d.name}/smart-test`, { test: "short" })); if (r) toast(r.message); } }, "Run short test"),
              h("button", { class: "btn", onclick: async (e) => { e.stopPropagation(); const r = await act(() => API.post(`/api/storage/disks/${d.name}/smart-test`, { test: "long" })); if (r) toast(r.message); } }, "Run extended test"),
              h("span", { class: "muted", style: "font-size:12px" }, "Schedule them in Control Panel → Task Scheduler"))]
            : kv("SMART", "not available (virtual disks and some USB enclosures don't report health)"));
      } },
        h("div", { class: "drive-icon" }, svgIcon(d.rota ? "hdd" : "ssd", 26), h("span", { class: "dot-status " + (cls || "none") })),
        h("div", { style: "flex:1;min-width:0" },
          h("div", {}, h("b", {}, d.name), h("span", { class: "muted" }, `  ${d.model || "Unknown model"}  ·  ${fmtBytes(d.size)}`)),
          h("div", { class: cls === "bad" ? "err" : "muted", style: "font-size:13px" }, r)),
        driveVitals(d),
        d.usage === "foreign" ? h("button", { class: "btn danger", onclick: (e) => { e.stopPropagation(); eraseDrives([d.name], () => go("drives")); } }, "Erase") : null,
        h("span", { class: "muted" }, "⌄")),
      details);
    return rowEl;
  })));
}

// Used by the setup wizard: jump straight to "Create storage pool".
function openCreatePool() {
  WM.open("storage");
  const w = WM.wins.storage;
  w.show("volumes");
  createPoolWizard(w.show.pane);
}

// healthy = green, optimizing/expanding = normal background work, rebuilding = warning, else = problem
function statusClass(s) {
  if (s === "healthy") return "ok";
  if (s.startsWith("optimizing") || s.startsWith("expanding")) return "info";
  if (s.startsWith("rebuilding") || s.includes("cache")) return "warn";
  return "bad";
}

// Step-by-step progress while a pool is being built (it takes a few minutes).
async function poolProgress(p, jobId, what) {
  const list = h("div", { class: "steps" });
  const note = h("div", { class: "muted" }, "This takes a few minutes. You can close this window - building continues.");
  p.replaceChildren(h("h2", {}, "Creating storage pool"), h("div", { class: "muted", style: "margin-bottom:12px" }, what), list, note);
  for (;;) {
    let j;
    try { j = await API.get(`/api/storage/jobs/${jobId}`); } catch { await new Promise((r) => setTimeout(r, 2000)); continue; }
    const secs = Math.round((Date.now() / 1000) - j.started);
    list.replaceChildren(...j.steps.map((s, i) => h("div", { class: "step " + (i < j.step ? "done" : i === j.step && j.state === "running" ? "now" : i === j.step && j.state === "failed" ? "fail" : "") },
      h("span", { class: "dot" }, i < j.step ? "✓" : i === j.step && j.state === "failed" ? "✕" : ""), s,
      i === j.step && j.state === "running" ? h("span", { class: "muted" }, `  ${secs}s`) : null)));
    if (j.state === "done") {
      note.replaceChildren(h("div", { class: "note" }, "✓ Storage pool created. It now runs a first background sync (“optimizing”) - you can already use it."),
        h("div", { class: "toolbar", style: "margin-top:10px" },
          h("button", { class: "btn primary", onclick: () => { p.replaceChildren(); storagePools(p); } }, "Show storage pools"),
          h("button", { class: "btn", onclick: () => { WM.open("control"); } }, "Create a shared folder")));
      toast("Storage pool created");
      return;
    }
    if (j.state === "failed") {
      note.replaceChildren(h("div", { class: "note warn" }, "Creating the pool failed: ", h("span", { class: "mono" }, j.error || "unknown error")),
        h("div", { class: "toolbar", style: "margin-top:10px" }, h("button", { class: "btn", onclick: () => { p.replaceChildren(); storagePools(p); } }, "Back")));
      return;
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

// temperature and power-on hours in the drive list (never wakes a sleeping drive)
function driveVitals(d) {
  const box = h("span", { class: "muted drive-vitals", style: "font-size:12px;white-space:nowrap" });
  if (d.system && !d.rota) return box;
  API.get(`/api/storage/disks/${d.name}/smart?let_sleep=true`).then((s) => {
    if (s.asleep) return box.replaceChildren("💤 " + t("sleeping"));
    if (!s.supported) return;
    const parts = [];
    if (s.temperature != null) parts.push(h("span", { class: s.temperature >= 55 ? "err" : "" }, `${s.temperature} °C`));
    if (s.power_on_hours != null) {
      const hrs = s.power_on_hours;
      parts.push(" · " + (hrs < 48 ? `${hrs} h` : hrs < 24 * 365 ? `${Math.round(hrs / 24)} ${t("days")}` : `${(hrs / 24 / 365).toFixed(1)} ${t("years")}`) + " " + t("in use"));
    }
    box.replaceChildren(...parts);
  }).catch(() => {});
  return box;
}
