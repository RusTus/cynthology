// File Station: browse, upload, download and organise files in shared folders.
WM.register("files", {
  name: "File Station", icon: "📁", color: "#e0a526", w: 980, h: 600,
  open(body) {
    const st = { path: "", items: [], access: "ro", sel: new Set(), clip: null, hidden: false };
    const side = h("nav", { class: "side" });
    const crumbs = h("div", { class: "fs-crumbs" });
    const list = h("div", { class: "fs-list" });
    const status = h("div", { class: "fs-status muted" });
    const fileInput = h("input", { type: "file", multiple: true, style: "display:none", onchange: (e) => uploadFiles([...e.target.files]) });
    const btn = (label, fn, opts = {}) => h("button", { class: "btn", onclick: fn, ...opts }, label);
    const tb = {
      up: btn("↑", () => go(parent(st.path)), { title: "Up one level" }),
      newf: btn("New folder", newFolder),
      upload: btn("Upload", () => fileInput.click()),
      download: btn("Download", downloadSel),
      rename: btn("Rename", renameSel),
      copy: btn("Copy", () => clip(false)),
      cut: btn("Move", () => clip(true)),
      paste: btn("Paste", paste),
      del: btn("Delete", deleteSel, { class: "btn danger" }),
    };
    const toolbar = h("div", { class: "pc-top fs-toolbar" }, Object.values(tb), h("span", { style: "flex:1" }),
      h("label", { class: "check muted", style: "font-size:12px" }, h("input", { type: "checkbox", onchange: (e) => { st.hidden = e.target.checked; go(st.path); } }), "Hidden files"),
      btn("⟳", () => go(st.path), { title: "Refresh" }), fileInput);
    body.append(side, h("div", { class: "fs-main" }, toolbar, crumbs, list, status));

    // drag & drop upload
    list.addEventListener("dragover", (e) => { e.preventDefault(); list.classList.add("drop"); });
    list.addEventListener("dragleave", () => list.classList.remove("drop"));
    list.addEventListener("drop", (e) => { e.preventDefault(); list.classList.remove("drop"); uploadFiles([...e.dataTransfer.files]); });

    const parent = (p) => p.split("/").slice(0, -1).join("/");
    const join = (a, b) => (a ? `${a}/${b}` : b);
    const selPaths = () => [...st.sel].map((n) => join(st.path, n));

    async function loadShares() {
      const shares = await API.get("/api/files/shares");
      side.replaceChildren(...(shares.length ? shares.map((s) => h("button", { "data-share": s.name, onclick: () => go(s.name) },
        "📁 ", s.name, s.access === "ro" ? h("span", { class: "muted" }, " (read-only)") : null))
        : [h("p", { class: "muted", style: "padding:8px" }, "No shared folders yet. Create one in Control Panel → Shared Folders.")]));
      if (shares.length && !st.path) go(shares[0].name);
    }

    async function go(path) {
      if (!path) return;
      let r;
      try { r = await API.get(`/api/files/list?path=${encodeURIComponent(path)}&hidden=${st.hidden}`); }
      catch (e) { return toast("Error: " + e.message); }
      Object.assign(st, { path: r.path, items: r.items, access: r.access });
      st.sel.clear();
      side.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.share === path.split("/")[0]));
      render();
    }

    function render() {
      const parts = st.path.split("/");
      crumbs.replaceChildren(...parts.flatMap((p, i) => [i ? h("span", { class: "muted" }, " › ") : "",
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(parts.slice(0, i + 1).join("/")); } }, p)]));
      const rows = st.items.map((it) => {
        const tr = h("tr", { class: st.sel.has(it.name) ? "sel" : "",
          onclick: (e) => {
            if (!(e.ctrlKey || e.metaKey)) st.sel.clear();
            st.sel.has(it.name) && (e.ctrlKey || e.metaKey) ? st.sel.delete(it.name) : st.sel.add(it.name);
            render();
          },
          ondblclick: () => it.dir ? go(join(st.path, it.name)) : openFile(it) },
          h("td", {}, (it.dir ? "📁 " : fileIcon(it.name) + " ") + it.name),
          h("td", { class: "muted" }, it.dir ? "" : fmtBytes(it.size)),
          h("td", { class: "muted" }, new Date(it.mtime * 1000).toLocaleString()));
        return tr;
      });
      list.replaceChildren(h("table", { class: "fs-table" },
        h("tr", {}, h("th", {}, "Name"), h("th", { style: "width:110px" }, "Size"), h("th", { style: "width:190px" }, "Modified")),
        rows.length ? rows : h("tr", {}, h("td", { colspan: 3, class: "muted", style: "padding:30px;text-align:center" }, "Empty folder - drop files here to upload"))));
      const rw = st.access === "rw", n = st.sel.size, one = n === 1 && !st.items.find((i) => i.name === [...st.sel][0])?.dir;
      tb.up.disabled = !st.path.includes("/");
      tb.newf.disabled = tb.upload.disabled = !rw;
      tb.rename.disabled = !rw || n !== 1;
      tb.del.disabled = tb.cut.disabled = !rw || !n;
      tb.copy.disabled = !n;
      tb.download.disabled = !one;
      tb.paste.disabled = !rw || !st.clip;
      tb.paste.textContent = st.clip ? `Paste (${st.clip.paths.length})` : "Paste";
      const total = st.items.reduce((a, i) => a + i.size, 0);
      status.textContent = `${st.items.length} item(s) · ${fmtBytes(total)}` + (n ? ` · ${n} selected` : "") + (rw ? "" : " · read-only");
    }

    async function newFolder() {
      const name = prompt("Folder name"); if (!name) return;
      await act(() => API.post("/api/files/mkdir", { path: st.path, name })); go(st.path);
    }
    async function renameSel() {
      const old = [...st.sel][0];
      const name = prompt("New name", old); if (!name || name === old) return;
      await act(() => API.post("/api/files/rename", { path: join(st.path, old), new_name: name })); go(st.path);
    }
    async function deleteSel() {
      if (!confirm(`Delete ${st.sel.size} item(s)? Shares with a recycle bin keep them in #recycle.`)) return;
      await act(() => API.post("/api/files/delete", { paths: selPaths() }), "Deleted"); go(st.path);
    }
    function clip(move) { st.clip = { paths: selPaths(), move }; toast(`${st.clip.paths.length} item(s) ready - open the destination and press Paste`); render(); }
    async function paste() {
      await act(() => API.post("/api/files/transfer", { paths: st.clip.paths, dest: st.path, move: st.clip.move }), st.clip.move ? "Moved" : "Copied");
      if (st.clip.move) st.clip = null;
      go(st.path);
    }
    async function downloadSel() {
      const r = await API.post("/api/files/ticket", { path: selPaths()[0] });
      const a = h("a", { href: r.url, download: "" }); document.body.append(a); a.click(); a.remove();
    }
    async function openFile(it) {
      const r = await API.post("/api/files/ticket", { path: join(st.path, it.name) });
      window.open(r.url + "&inline=true", "_blank");
    }
    async function uploadFiles(files) {
      if (!files.length) return;
      if (st.access !== "rw") return toast("This folder is read-only for you");
      let done = 0;
      for (const f of files) {
        status.textContent = `Uploading ${f.name} (${done + 1}/${files.length})…`;
        const url = `/api/files/upload?path=${encodeURIComponent(st.path)}&name=${encodeURIComponent(f.name)}`;
        let res = await fetch(url, { method: "POST", headers: { Authorization: `Bearer ${API.token}` }, body: f });
        if (res.status === 409 && confirm(`${f.name} already exists. Replace it?`))
          res = await fetch(url + "&overwrite=true", { method: "POST", headers: { Authorization: `Bearer ${API.token}` }, body: f });
        if (!res.ok && res.status !== 409) toast(`${f.name}: ` + ((await res.json().catch(() => ({}))).detail || res.statusText));
        else if (res.ok) done++;
      }
      toast(`Uploaded ${done} file(s)`);
      go(st.path);
    }

    loadShares();
  },
});

function fileIcon(name) {
  const ext = name.split(".").pop().toLowerCase();
  if (["jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "svg"].includes(ext)) return "🖼️";
  if (["mp4", "mkv", "mov", "avi", "webm"].includes(ext)) return "🎞️";
  if (["mp3", "flac", "wav", "m4a", "ogg"].includes(ext)) return "🎵";
  if (["zip", "7z", "rar", "gz", "tar", "xz"].includes(ext)) return "🗜️";
  if (["pdf"].includes(ext)) return "📕";
  if (["doc", "docx", "odt", "txt", "md", "rtf"].includes(ext)) return "📝";
  if (["xls", "xlsx", "ods", "csv"].includes(ext)) return "📊";
  return "📄";
}
