// File Station: folder tree, back/forward, path bar, search, list or icon view, sortable columns,
// Create / Upload / Action menus, right-click menu, keyboard shortcuts, drag & drop upload,
// zip download of folders and multiple items, Copy to / Move to.
WM.register("files", {
  name: "File Station", icon: "📁", color: "#e0a526", w: 1060, h: 640,
  open(body) {
    const st = { path: "", items: [], access: "ro", sel: new Set(), last: null, clip: null, hidden: false,
      view: "list", sort: { key: "name", asc: true }, back: [], fwd: [], search: null };
    try { st.view = localStorage.getItem("cyn-fs-view") || "list"; } catch {}

    // ---------- layout ----------
    const tree = h("nav", { class: "side fs-tree" });
    const pathbar = h("div", { class: "fs-path" });
    const searchBox = h("input", { class: "fs-search", placeholder: "Search in this folder", type: "search",
      onkeydown: (e) => { if (e.key === "Enter") runSearch(searchBox.value); if (e.key === "Escape") { searchBox.value = ""; closeSearch(); } } });
    const navBtn = (icon, title, fn) => h("button", { class: "btn fs-nav", title, onclick: fn }, icon);
    const bBack = navBtn("‹", "Back", () => history(-1)), bFwd = navBtn("›", "Forward", () => history(1));
    const top = h("div", { class: "fs-top" }, bBack, bFwd, navBtn("⟳", "Refresh", () => (st.search ? runSearch(st.search.q) : go(st.path, true))), pathbar, searchBox);
    const menuBtn = (label, items) => h("button", { class: "btn fs-menu-btn", onclick: (e) => { e.stopPropagation(); popup(e.currentTarget, items()); } }, label, h("span", { class: "fs-caret" }, "▾"));
    const viewBtn = h("button", { class: "btn fs-nav", title: "List / icons", onclick: () => { st.view = st.view === "list" ? "grid" : "list"; try { localStorage.setItem("cyn-fs-view", st.view); } catch {} render(); } });
    const cmd = h("div", { class: "fs-cmd" },
      menuBtn("Create", () => [["New folder", newFolder, !rw()]]),
      menuBtn("Upload", () => [["Upload files…", () => fileInput.click(), !rw()], ["Upload a folder…", () => folderInput.click(), !rw()]]),
      menuBtn("Action", actionItems),
      h("span", { style: "flex:1" }), viewBtn,
      menuBtn("Settings", () => [[(st.hidden ? "✓ " : "") + "Show hidden files", () => { st.hidden = !st.hidden; go(st.path, true); }]]));
    const list = h("div", { class: "fs-list", tabindex: 0 });
    const status = h("div", { class: "fs-status muted" });
    const fileInput = h("input", { type: "file", multiple: true, style: "display:none", onchange: (e) => { uploadFiles([...e.target.files]); e.target.value = ""; } });
    const folderInput = h("input", { type: "file", multiple: true, webkitdirectory: true, style: "display:none", onchange: (e) => { uploadFiles([...e.target.files]); e.target.value = ""; } });
    body.append(tree, h("div", { class: "fs-main" }, top, cmd, list, status, fileInput, folderInput));

    const parent = (p) => p.split("/").slice(0, -1).join("/");
    const join = (a, b) => (a ? `${a}/${b}` : b);
    const rw = () => st.access === "rw" && !st.search;
    const itemPath = (it) => join(it.folder || st.path, it.name);
    const selected = () => st.items.filter((i) => st.sel.has(itemPath(i)));

    // ---------- folder tree ----------
    const nodes = new Map();   // path -> { row, kids, caret, open, loaded, depth }
    function treeNode(path, name, depth, extra) {
      const kids = h("div", { class: "hidden" });
      const caret = h("span", { class: "fs-twisty", onclick: (e) => { e.stopPropagation(); toggle(path); } }, "▸");
      const row = h("button", { class: "fs-node", translate: "no", style: `padding-left:${8 + depth * 14}px`, onclick: () => go(path) },
        caret, fsIcon({ dir: true, name }, 18), h("span", { class: "fs-node-name" }, name), extra || null);
      nodes.set(path, { row, kids, caret, open: false, loaded: false, depth });
      return h("div", {}, row, kids);
    }
    async function toggle(path, open) {
      const n = nodes.get(path); if (!n) return;
      n.open = open ?? !n.open;
      n.kids.classList.toggle("hidden", !n.open);
      n.caret.textContent = n.open ? "▾" : "▸";
      if (n.open && !n.loaded) {
        n.loaded = true;
        try {
          const r = await API.get(`/api/files/list?path=${encodeURIComponent(path)}&dirs_only=true&hidden=${st.hidden}`);
          n.kids.replaceChildren(...r.items.map((it) => treeNode(join(path, it.name), it.name, n.depth + 1)));
          n.caret.style.visibility = r.items.length ? "" : "hidden";
        } catch { n.loaded = false; }
      }
    }
    async function revealInTree(path) {
      const parts = path.split("/");
      for (let i = 1; i < parts.length; i++) await toggle(parts.slice(0, i).join("/"), true);
      nodes.forEach((n, p) => n.row.classList.toggle("on", p === path));
    }
    async function loadShares() {
      const shares = await API.get("/api/files/shares");
      nodes.clear();
      tree.replaceChildren(...(shares.length ? shares.map((s) => treeNode(s.name, s.name, 0, s.access === "ro" ? h("span", { class: "muted fs-ro" }, "read-only") : null))
        : [h("p", { class: "muted", style: "padding:8px" }, "No shared folders yet. Create one in Control Panel → Shared Folder.")]));
      if (shares.length && !st.path) go(shares[0].name);
    }

    // ---------- navigation ----------
    async function go(path, keepHistory = false, fromHistory = false) {
      if (!path) return;
      let r;
      try { r = await API.get(`/api/files/list?path=${encodeURIComponent(path)}&hidden=${st.hidden}`); }
      catch (e) { return toast("Error: " + e.message); }
      if (!fromHistory && !keepHistory && st.path && st.path !== r.path) { st.back.push(st.path); st.fwd = []; }
      Object.assign(st, { path: r.path, items: r.items, access: r.access, search: null, last: null });
      st.sel.clear();
      searchBox.value = "";
      searchBox.placeholder = `Search in ${r.path.split("/").pop()}`;
      render();
      revealInTree(r.path);
    }
    function history(dir) {
      const from = dir < 0 ? st.back : st.fwd, to = dir < 0 ? st.fwd : st.back;
      const p = from.pop(); if (!p) return;
      to.push(st.path);
      go(p, false, true);
    }
    async function runSearch(q) {
      if (!q.trim()) return closeSearch();
      status.textContent = "Searching…";
      const base = st.search ? st.search.base : st.path;
      let r;
      try { r = await API.get(`/api/files/search?path=${encodeURIComponent(base)}&q=${encodeURIComponent(q)}&hidden=${st.hidden}`); }
      catch (e) { return toast(e.message); }
      st.search = { q, truncated: r.truncated, base };
      st.items = r.items; st.sel.clear(); st.last = null;
      render();
    }
    function closeSearch() { if (st.search) go(st.search.base, true); }

    // ---------- rendering ----------
    const cmp = {
      name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }),
      size: (a, b) => a.size - b.size,
      type: (a, b) => fileType(a).localeCompare(fileType(b)) || cmp.name(a, b),
      mtime: (a, b) => a.mtime - b.mtime,
      folder: (a, b) => (a.folder || "").localeCompare(b.folder || "") || cmp.name(a, b),
    };
    function sorted() {
      const f = cmp[st.sort.key] || cmp.name;
      return [...st.items].sort((a, b) => (b.dir - a.dir) || (st.sort.asc ? f(a, b) : f(b, a)));
    }
    function renderPath() {
      if (st.search) {
        pathbar.onclick = null;
        pathbar.replaceChildren(h("span", { class: "fs-crumb static" }, `Search results for “${st.search.q}” in ${st.search.base}`),
          h("span", { style: "flex:1" }), h("button", { class: "btn fs-nav", title: "Close search", onclick: closeSearch }, "✕"));
        return;
      }
      const parts = st.path.split("/");
      pathbar.replaceChildren(...parts.flatMap((p, i) => [i ? h("span", { class: "fs-sep" }, "›") : null,
        h("button", { class: "fs-crumb", translate: "no", onclick: (e) => { e.stopPropagation(); go(parts.slice(0, i + 1).join("/")); } }, p)]),
        h("span", { style: "flex:1" }));
      pathbar.onclick = () => {   // click the empty part of the bar to type a path
        const inp = h("input", { value: st.path, class: "fs-path-input",
          onkeydown: (e) => { if (e.key === "Enter") go(inp.value.trim().replace(/^\/+|\/+$/g, "")); if (e.key === "Escape") renderPath(); },
          onblur: () => setTimeout(renderPath, 150) });
        pathbar.onclick = null;
        pathbar.replaceChildren(inp); inp.focus(); inp.select();
      };
    }
    function clickItem(e, it, idx, rows) {
      const p = itemPath(it);
      if (e.shiftKey && st.last != null) {
        const [a, b] = [Math.min(st.last, idx), Math.max(st.last, idx)];
        if (!(e.ctrlKey || e.metaKey)) st.sel.clear();
        rows.slice(a, b + 1).forEach((x) => st.sel.add(itemPath(x)));
      } else if (e.ctrlKey || e.metaKey) {
        st.sel.has(p) ? st.sel.delete(p) : st.sel.add(p);
        st.last = idx;
      } else {
        st.sel.clear(); st.sel.add(p); st.last = idx;
      }
      render();
    }
    function render() {
      renderPath();
      bBack.disabled = !st.back.length; bFwd.disabled = !st.fwd.length;
      viewBtn.textContent = st.view === "list" ? "▦" : "☰";
      const rows = sorted();
      const open = (it) => (it.dir ? go(itemPath(it)) : openFile(it));
      const ctx = (e, it, idx) => {
        e.preventDefault(); e.stopPropagation();
        if (it && !st.sel.has(itemPath(it))) { st.sel.clear(); st.sel.add(itemPath(it)); st.last = idx; render(); }
        if (!it && st.sel.size) { st.sel.clear(); render(); }
        popup(null, it ? actionItems() : [["New folder", newFolder, !rw()], ["Upload files…", () => fileInput.click(), !rw()],
          ["Paste", paste, !rw() || !st.clip], null, ["Refresh", () => go(st.path, true)], ["Properties", properties, !!st.search]], e.clientX, e.clientY);
      };
      let content;
      if (!rows.length) {
        content = h("div", { class: "fs-empty muted" }, st.search ? "Nothing found." : rw() ? "This folder is empty. Drop files here to upload them." : "This folder is empty.");
      } else if (st.view === "grid") {
        content = h("div", { class: "fs-grid" }, rows.map((it, i) => h("div", {
          class: "fs-tile" + (st.sel.has(itemPath(it)) ? " sel" : ""), title: it.folder ? `${it.folder}/${it.name}` : it.name,
          onclick: (e) => { e.stopPropagation(); clickItem(e, it, i, rows); }, ondblclick: () => open(it), oncontextmenu: (e) => ctx(e, it, i) },
          fsIcon(it, 56), h("div", { class: "fs-tile-name" }, it.name))));
      } else {
        const col = (key, label, style) => h("th", { style, class: "fs-sortable", onclick: () => { st.sort = { key, asc: st.sort.key === key ? !st.sort.asc : true }; render(); } },
          label, st.sort.key === key ? h("span", { class: "fs-caret" }, st.sort.asc ? " ▲" : " ▼") : null);
        content = h("table", { class: "fs-table" },
          h("tr", {}, col("name", "Name"), st.search ? col("folder", "Folder", "width:200px") : null, col("size", "Size", "width:100px"),
            col("type", "File Type", "width:160px"), col("mtime", "Modified Date", "width:150px")),
          rows.map((it, i) => h("tr", { class: st.sel.has(itemPath(it)) ? "sel" : "",
            onclick: (e) => { e.stopPropagation(); clickItem(e, it, i, rows); }, ondblclick: () => open(it), oncontextmenu: (e) => ctx(e, it, i) },
            h("td", {}, h("div", { class: "fs-name" }, fsIcon(it, 20), h("span", {}, it.name))),
            st.search ? h("td", { class: "muted fs-ellipsis" }, h("a", { href: "#", translate: "no", onclick: (e) => { e.preventDefault(); go(it.folder); } }, it.folder)) : null,
            h("td", { class: "muted" }, it.dir ? "" : fmtBytes(it.size)),
            h("td", { class: "muted" }, fileType(it)),
            h("td", { class: "muted" }, new Date(it.mtime * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })))));
      }
      list.replaceChildren(content);
      list.oncontextmenu = (e) => ctx(e, null);
      list.onclick = () => { if (st.sel.size) { st.sel.clear(); render(); } };
      const n = st.sel.size;
      const bytes = (n ? selected() : st.items).filter((i) => !i.dir).reduce((a, i) => a + i.size, 0);
      status.replaceChildren(h("span", {}, `${st.items.length} item${st.items.length === 1 ? "" : "s"}` + (st.search?.truncated ? " (first results only)" : "")),
        n ? h("span", {}, ` · ${n} selected${bytes ? " · " + fmtBytes(bytes) : ""}`) : null,
        st.access !== "rw" && !st.search ? h("span", {}, " · read-only") : null,
        st.clip ? h("span", {}, ` · ${st.clip.paths.length} item(s) to ${st.clip.move ? "move" : "copy"} - open a folder and Paste`) : null);
    }

    // ---------- menus ----------
    function actionItems() {
      const sel = selected(), n = sel.length, one = n === 1 ? sel[0] : null;
      const canWrite = st.access === "rw" || (st.search && n);
      return [
        ["Open", () => (one.dir ? go(itemPath(one)) : openFile(one)), !one],
        ["Download", downloadSel, !n],
        null,
        ["Copy to…", () => transferTo(false), !n],
        ["Move to…", () => transferTo(true), !n || !canWrite],
        ["Copy", () => clip(false), !n],
        ["Cut", () => clip(true), !n || !canWrite],
        ["Paste", paste, !rw() || !st.clip],
        null,
        ["Rename", renameSel, !one || !canWrite],
        ["Delete", deleteSel, !n || !canWrite, "danger"],
        null,
        ["Properties", properties, !n && !!st.search],
      ];
    }
    function popup(anchor, items, x, y) {
      document.querySelectorAll(".fs-pop").forEach((m) => m.remove());
      const m = h("div", { class: "fs-pop" }, items.map((it) => it === null ? h("div", { class: "fs-pop-sep" })
        : h("button", { disabled: !!it[2], class: it[3] || "", onclick: () => { m.remove(); it[1](); } }, it[0])));
      document.body.append(m);
      const r = anchor ? anchor.getBoundingClientRect() : { left: x, bottom: y };
      m.style.left = Math.max(8, Math.min(r.left, innerWidth - m.offsetWidth - 8)) + "px";
      m.style.top = Math.max(8, Math.min(r.bottom + 4, innerHeight - m.offsetHeight - 8)) + "px";
      setTimeout(() => document.addEventListener("click", () => m.remove(), { once: true }), 0);
    }

    // ---------- actions ----------
    async function newFolder() {
      const name = prompt("New folder name", "New folder"); if (!name) return;
      await act(() => API.post("/api/files/mkdir", { path: st.path, name }));
      refreshTreeAt(st.path);
      go(st.path, true);
    }
    async function renameSel() {
      const it = selected()[0];
      const name = prompt("New name", it.name); if (!name || name === it.name) return;
      await act(() => API.post("/api/files/rename", { path: itemPath(it), new_name: name }));
      refreshTreeAt(it.folder || st.path);
      st.search ? runSearch(st.search.q) : go(st.path, true);
    }
    async function deleteSel() {
      const sel = selected();
      if (!confirm(`Delete ${sel.length === 1 ? `"${sel[0].name}"` : `${sel.length} items`}?\n\nShared folders with a recycle bin keep deleted items in #recycle.`)) return;
      await act(() => API.post("/api/files/delete", { paths: sel.map(itemPath) }), "Deleted");
      refreshTreeAt(st.path);
      st.search ? runSearch(st.search.q) : go(st.path, true);
    }
    function clip(move) { st.clip = { paths: selected().map(itemPath), move }; render(); }
    async function paste() {
      await act(() => API.post("/api/files/transfer", { paths: st.clip.paths, dest: st.path, move: st.clip.move }), st.clip.move ? "Moved" : "Copied");
      if (st.clip.move) st.clip = null;
      refreshTreeAt(st.path);
      go(st.path, true);
    }
    async function transferTo(move) {
      const paths = selected().map(itemPath);
      const dest = await pickFolder(move ? "Move to" : "Copy to", st.search ? st.search.base : st.path);
      if (!dest) return;
      await act(() => API.post("/api/files/transfer", { paths, dest, move }), `${move ? "Moved" : "Copied"} to ${dest}`);
      refreshTreeAt(dest);
      st.search ? runSearch(st.search.q) : go(st.path, true);
    }
    function refreshTreeAt(path) {
      const n = nodes.get(path); if (!n) return;
      n.loaded = false; if (n.open) toggle(path, true);
    }
    async function downloadSel() {
      const sel = selected();
      const r = await API.post("/api/files/ticket", { paths: sel.map(itemPath) });
      const a = h("a", { href: r.url, download: "" }); document.body.append(a); a.click(); a.remove();
      if (sel.length > 1 || sel[0]?.dir) toast("Downloading as a .zip file");
    }
    async function openFile(it) {
      const r = await API.post("/api/files/ticket", { path: itemPath(it) });
      window.open(r.url + "&inline=true", "_blank", "noopener");
    }
    async function properties() {
      const sel = selected();
      const kv = (k, v) => h("tr", {}, h("td", { class: "muted", style: "width:130px" }, k), h("td", {}, v));
      const size = h("span", { class: "muted" }, "calculating…");
      const self = !sel.length;
      const target = self ? [{ name: st.path.split("/").pop(), dir: true }] : sel;
      const one = target.length === 1 ? target[0] : null;
      const paths = self ? [st.path] : sel.map(itemPath);
      const where = self ? parent(st.path) : (one ? one.folder || st.path : st.path);
      modal(one ? one.name : `${target.length} items`, h("table", {},
        one ? kv("Type", fileType(one)) : null,
        kv("Location", "/" + where),
        kv("Size", size),
        one && one.mtime ? kv("Modified", new Date(one.mtime * 1000).toLocaleString()) : null));
      let bytes = 0, files = 0;
      for (const p of paths) {
        const u = await API.get(`/api/files/usage?path=${encodeURIComponent(p)}`).catch(() => ({ bytes: 0, files: 0 }));
        bytes += u.bytes; files += u.files;
      }
      size.replaceChildren(`${fmtBytes(bytes)} (${bytes.toLocaleString()} bytes)` + (target.some((t) => t.dir) ? ` · ${files.toLocaleString()} file(s)` : ""));
      size.className = "";
    }
    async function uploadFiles(files) {
      if (!files.length) return;
      if (!rw()) return toast("This folder is read-only for you");
      const made = new Set();
      let done = 0, skipped = 0;
      for (const f of files) {
        const rel = (f.webkitRelativePath || f.name).split("/");
        const name = rel.pop();
        let folder = st.path;
        for (const d of rel) {   // folder upload: create the sub-folders first
          const next = join(folder, d);
          if (!made.has(next)) {
            await API.post("/api/files/mkdir", { path: folder, name: d }).catch(() => {});
            made.add(next);
          }
          folder = next;
        }
        status.textContent = `Uploading ${name} (${done + skipped + 1}/${files.length})…`;
        const url = `/api/files/upload?path=${encodeURIComponent(folder)}&name=${encodeURIComponent(name)}`;
        let res = await fetch(url, { method: "POST", headers: { Authorization: `Bearer ${API.token}` }, body: f });
        if (res.status === 409 && confirm(`${name} already exists. Replace it?`))
          res = await fetch(url + "&overwrite=true", { method: "POST", headers: { Authorization: `Bearer ${API.token}` }, body: f });
        if (res.ok) done++;
        else if (res.status === 409) skipped++;
        else { toast(`${name}: ` + ((await res.json().catch(() => ({}))).detail || res.statusText)); skipped++; }
      }
      toast(`Uploaded ${done} file(s)` + (skipped ? `, ${skipped} skipped` : ""));
      if (made.size) refreshTreeAt(st.path);
      go(st.path, true);
    }

    // drag & drop upload
    list.addEventListener("dragover", (e) => { e.preventDefault(); list.classList.add("drop"); });
    list.addEventListener("dragleave", () => list.classList.remove("drop"));
    list.addEventListener("drop", (e) => { e.preventDefault(); list.classList.remove("drop"); uploadFiles([...e.dataTransfer.files]); });

    // keyboard shortcuts (when the file list has focus)
    list.addEventListener("keydown", (e) => {
      const mod = e.ctrlKey || e.metaKey, sel = selected(), canWrite = st.access === "rw" || st.search;
      if (mod && e.key === "a") { st.items.forEach((i) => st.sel.add(itemPath(i))); render(); }
      else if (mod && e.key === "c" && sel.length) clip(false);
      else if (mod && e.key === "x" && sel.length && canWrite) clip(true);
      else if (mod && e.key === "v" && rw() && st.clip) paste();
      else if (e.key === "Delete" && sel.length && canWrite) deleteSel();
      else if (e.key === "F2" && sel.length === 1 && canWrite) renameSel();
      else if (e.key === "Enter" && sel.length === 1) sel[0].dir ? go(itemPath(sel[0])) : openFile(sel[0]);
      else if (e.key === "Backspace" && !st.search && st.path.includes("/")) go(parent(st.path));
      else return;
      e.preventDefault();
    });

    // Folder picker for Copy to / Move to
    async function pickFolder(title, start) {
      let chosen = start;
      const label = h("div", { class: "mono muted", style: "font-size:12px;margin-top:8px" }, "/" + chosen);
      const box = h("div", { class: "fs-picker" });
      const rowsByPath = new Map();
      const node = (path, name, depth) => {
        const kids = h("div", { class: "hidden" });
        let loaded = false, opened = false;
        const caret = h("span", { class: "fs-twisty" }, "▸");
        const row = h("button", { class: "fs-node" + (path === chosen ? " on" : ""), translate: "no", style: `padding-left:${8 + depth * 14}px`, onclick: async () => {
          chosen = path; label.textContent = "/" + path;
          rowsByPath.forEach((r, p) => r.classList.toggle("on", p === path));
          opened = !opened; kids.classList.toggle("hidden", !opened); caret.textContent = opened ? "▾" : "▸";
          if (opened && !loaded) {
            loaded = true;
            const r = await API.get(`/api/files/list?path=${encodeURIComponent(path)}&dirs_only=true`).catch(() => ({ items: [] }));
            kids.replaceChildren(...r.items.map((it) => node(join(path, it.name), it.name, depth + 1)));
            if (!r.items.length) caret.style.visibility = "hidden";
          }
        } }, caret, fsIcon({ dir: true, name }, 18), h("span", {}, name));
        rowsByPath.set(path, row);
        return h("div", {}, row, kids);
      };
      const shares = (await API.get("/api/files/shares")).filter((s) => s.access === "rw");
      box.append(...shares.map((s) => node(s.name, s.name, 0)));
      return new Promise((resolve) => {
        const close = modal(title, h("div", {}, box, label), [
          ["Cancel", () => { close(); resolve(null); }],
          ["OK", () => { close(); resolve(chosen); }, "primary"]]);
      });
    }

    loadShares();
  },
});

const FS_TYPES = {
  image: [["jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp", "svg", "tif", "tiff", "raw", "cr2", "nef", "arw", "dng"], "#22c55e", "Image"],
  video: [["mp4", "mkv", "mov", "avi", "webm", "m4v", "wmv", "mts", "m2ts", "ts", "flv"], "#a855f7", "Video"],
  audio: [["mp3", "flac", "wav", "m4a", "ogg", "aac", "opus", "wma", "alac", "aiff"], "#ec4899", "Audio"],
  archive: [["zip", "7z", "rar", "gz", "tgz", "tar", "xz", "bz2", "zst", "iso", "img", "dmg"], "#a16207", "Archive"],
  pdf: [["pdf"], "#ef4444", "PDF document"],
  doc: [["doc", "docx", "odt", "rtf", "pages"], "#3b82f6", "Document"],
  sheet: [["xls", "xlsx", "ods", "csv", "numbers"], "#16a34a", "Spreadsheet"],
  slides: [["ppt", "pptx", "odp", "key"], "#f97316", "Presentation"],
  text: [["txt", "md", "log", "ini", "conf", "cfg", "nfo", "srt"], "#64748b", "Text"],
  code: [["js", "ts", "py", "sh", "html", "css", "json", "xml", "yml", "yaml", "php", "c", "h", "cpp", "go", "rs", "java", "sql"], "#0ea5e9", "Code"],
  exe: [["exe", "msi", "deb", "rpm", "apk", "spk", "pkg", "appimage"], "#475569", "Program"],
};
function fsKind(name) {
  const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
  for (const [k, [exts]] of Object.entries(FS_TYPES)) if (exts.includes(ext)) return [k, ext];
  return ["file", ext];
}
function fileType(it) {
  if (it.dir) return t("Folder");
  const [k, ext] = fsKind(it.name);
  if (k === "file") return ext ? `${ext.toUpperCase()} ${t("file")}` : t("File");
  return FS_TYPES[k][0].length === 1 ? t(FS_TYPES[k][2]) : `${t(FS_TYPES[k][2])} (${ext.toUpperCase()})`;
}
// SVG folder / file icon; files get a coloured badge with their extension.
function fsIcon(it, size = 20) {
  const span = document.createElement("span");
  span.className = "fs-icon";
  if (it.dir) {
    span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 6.5a2 2 0 0 1 2-2h4.6l2 2h8.4a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-15a2 2 0 0 1-2-2z" fill="#f5b83d"/><path d="M2.5 9h19v8.5a2 2 0 0 1-2 2h-15a2 2 0 0 1-2-2z" fill="#fbcd5a"/></svg>`;
    return span;
  }
  const [k, ext] = fsKind(it.name);
  const color = FS_TYPES[k]?.[1] || "#94a3b8";
  const label = (ext || "").slice(0, 4).toUpperCase().replace(/[^A-Z0-9]/g, "");
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 2.5h8l4.5 4.5v13a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 20V4a1.5 1.5 0 0 1 1.5-1.5z" fill="#e8edf3"/><path d="M14.5 2.5V7H19z" fill="#c3ccd8"/>` +
    (label && size >= 20 ? `<rect x="3" y="12" width="15" height="7" rx="1.5" fill="${color}"/><text x="10.5" y="17.3" font-size="${label.length > 3 ? 4.2 : 5}" font-family="system-ui,sans-serif" font-weight="700" fill="#fff" text-anchor="middle">${label}</text>`
      : `<rect x="7.5" y="12.5" width="9" height="1.8" rx=".9" fill="${color}"/><rect x="7.5" y="16" width="6" height="1.8" rx=".9" fill="${color}"/>`) + `</svg>`;
  return span;
}
