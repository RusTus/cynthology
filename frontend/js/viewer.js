// File viewer: photos (swipe / arrow keys), video and music, PDF (Debian's pdf.js, served by
// the NAS itself), and text files with editing. Used by File Station.
const PREVIEW_KIND = (() => {
  const m = {};
  "jpg jpeg png gif webp bmp tif tiff heic heif avif".split(" ").forEach((e) => (m[e] = "image"));
  "mp4 m4v mov mkv webm avi wmv mpg mpeg 3gp ts mts m2ts".split(" ").forEach((e) => (m[e] = "video"));
  "mp3 m4a aac flac ogg oga opus wav".split(" ").forEach((e) => (m[e] = "audio"));
  m.pdf = "pdf";
  "txt md log csv json xml yml yaml ini conf cfg sh py js css html htm srt nfo toml env sql c h cpp java go rs".split(" ").forEach((e) => (m[e] = "text"));
  return m;
})();
const previewKind = (name) => PREVIEW_KIND[(name.split(".").pop() || "").toLowerCase()] || null;
const THUMBABLE = new Set(["image", "video", "pdf"]);

// authenticated fetch -> object URL (cached per path+size for this page)
const _thumbCache = new Map();
const _thumbQueue = [];
let _thumbBusy = 0;
function thumbUrl(path, size = 256) {
  const key = size + "|" + path;
  if (_thumbCache.has(key)) return _thumbCache.get(key);
  const p = new Promise((resolve) => {
    _thumbQueue.push(async () => {
      try {
        const r = await fetch(`/api/preview/thumb?size=${size}&path=${encodeURIComponent(path)}`, { headers: { Authorization: `Bearer ${API.token}` } });
        resolve(r.ok ? URL.createObjectURL(await r.blob()) : null);
      } catch { resolve(null); }
    });
    _pumpThumbs();
  });
  _thumbCache.set(key, p);
  return p;
}
function _pumpThumbs() {
  while (_thumbBusy < 4 && _thumbQueue.length) {
    const job = _thumbQueue.shift();
    _thumbBusy++;
    job().finally(() => { _thumbBusy--; _pumpThumbs(); });
  }
}
// show a thumbnail in `el` once it scrolls into view
const _thumbObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    _thumbObserver.unobserve(e.target);
    const el = e.target;
    thumbUrl(el.dataset.thumb, 256).then((u) => {
      if (!u) return;
      el.replaceChildren(h("img", { src: u, alt: "", class: "fs-thumb", loading: "lazy", draggable: "false" }));
    });
  }
}, { rootMargin: "200px" }) : null;
function lazyThumb(path, fallback) {
  const box = h("div", { class: "fs-thumb-box", "data-thumb": path }, fallback);
  if (_thumbObserver) _thumbObserver.observe(box);
  return box;
}

// items: [{ name, path, size, writable }], index: which to show first
function openViewer(items, index) {
  let i = index;
  const stage = h("div", { class: "viewer-stage" });
  const title = h("div", { class: "viewer-title", translate: "no" });
  const count = h("span", { class: "viewer-count" });
  const prevB = h("button", { class: "viewer-nav prev", title: "Previous", onclick: (e) => { e.stopPropagation(); go(-1); } }, "‹");
  const nextB = h("button", { class: "viewer-nav next", title: "Next", onclick: (e) => { e.stopPropagation(); go(1); } }, "›");
  const dl = h("button", { class: "btn", onclick: () => download(items[i]) }, "Download");
  const wrap = h("div", { class: "viewer", tabindex: 0 },
    h("div", { class: "viewer-bar" }, title, count, h("span", { style: "flex:1" }), dl,
      h("button", { class: "btn", title: "Close (Esc)", onclick: () => close() }, "✕")),
    stage, prevB, nextB);
  let cleanup = [];
  const close = () => { cleanup.forEach((f) => f()); wrap.remove(); document.removeEventListener("keydown", key); };
  const key = (e) => {
    if (e.target.tagName === "TEXTAREA") { if (e.key === "Escape") close(); return; }
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "ArrowRight") go(1);
  };
  document.addEventListener("keydown", key);
  // swipe on phones
  let sx = null;
  stage.addEventListener("pointerdown", (e) => { if (e.pointerType === "touch") sx = e.clientX; });
  stage.addEventListener("pointerup", (e) => {
    if (sx == null) return;
    const dx = e.clientX - sx; sx = null;
    if (Math.abs(dx) > 60) go(dx < 0 ? 1 : -1);
  });
  async function download(it) {
    const r = await API.post("/api/files/ticket", { path: it.path });
    const a = h("a", { href: r.url, download: "" }); document.body.append(a); a.click(); a.remove();
  }
  function go(d) {
    const n = i + d;
    if (n < 0 || n >= items.length) return;
    i = n; show();
  }
  async function show() {
    cleanup.forEach((f) => f()); cleanup = [];
    const it = items[i];
    title.textContent = it.name;
    count.textContent = items.length > 1 ? `${i + 1} / ${items.length}` : "";
    prevB.style.visibility = i > 0 ? "visible" : "hidden";
    nextB.style.visibility = i < items.length - 1 ? "visible" : "hidden";
    const kind = previewKind(it.name);
    stage.replaceChildren(h("div", { class: "muted" }, t("Loading…")));
    try {
      if (kind === "image") {
        const u = await thumbUrl(it.path, 1600);
        if (items[i] !== it) return;
        stage.replaceChildren(u ? h("img", { src: u, class: "viewer-img", alt: it.name, draggable: "false" }) : noPreview(it));
        // preload neighbours so swiping feels instant
        [items[i + 1], items[i - 1]].forEach((n) => n && previewKind(n.name) === "image" && thumbUrl(n.path, 1600));
      } else if (kind === "video" || kind === "audio") {
        const { url } = await API.post("/api/preview/stream", { path: it.path });
        const el = h(kind, { src: url, controls: true, autoplay: true, class: "viewer-media", playsinline: true });
        el.addEventListener("error", () => stage.replaceChildren(noPreview(it, "Your browser can't play this format. Download it and use a player like VLC.")));
        stage.replaceChildren(el);
        cleanup.push(() => { el.pause(); el.removeAttribute("src"); el.load(); });
      } else if (kind === "pdf") {
        await showPdf(it);
      } else if (kind === "text") {
        await showText(it);
      } else {
        stage.replaceChildren(noPreview(it));
      }
    } catch (e) {
      stage.replaceChildren(noPreview(it, e.message));
    }
  }
  function noPreview(it, why) {
    return h("div", { class: "viewer-none" }, h("div", { style: "font-size:48px" }, "📄"), h("div", { translate: "no" }, it.name),
      h("div", { class: "muted" }, why || t("No preview for this kind of file.")),
      h("button", { class: "btn primary", onclick: () => download(it) }, "Download"));
  }
  async function showPdf(it) {
    if (!window.pdfjsLib) {
      await new Promise((ok, fail) => { const s = h("script", { src: "/vendor/pdfjs/build/pdf.js" }); s.onload = ok; s.onerror = () => fail(new Error(t("The PDF viewer isn't installed - update Cynthology."))); document.head.append(s); });
      pdfjsLib.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/build/pdf.worker.js";
    }
    const { url } = await API.post("/api/preview/stream", { path: it.path });
    // (scripts inside the PDF never run: pdf.js only draws it)
    const doc = await pdfjsLib.getDocument({ url, isEvalSupported: false, enableScripting: false }).promise;
    const pages = h("div", { class: "viewer-pdf" });
    stage.replaceChildren(pages);
    const width = Math.min(stage.clientWidth - 24, 1000);
    for (let n = 1; n <= Math.min(doc.numPages, 200); n++) {
      if (items[i] !== it) return;
      const page = await doc.getPage(n);
      const vp0 = page.getViewport({ scale: 1 });
      const scale = (width / vp0.width) * (devicePixelRatio || 1);
      const vp = page.getViewport({ scale });
      const cv = h("canvas", { width: vp.width, height: vp.height, style: `width:${width}px` });
      pages.append(cv);
      await page.render({ canvasContext: cv.getContext("2d"), viewport: vp }).promise;
    }
    if (doc.numPages > 200) pages.append(h("div", { class: "muted" }, t("Only the first 200 pages are shown - download the file for the rest.")));
    cleanup.push(() => doc.destroy());
  }
  async function showText(it) {
    const d = await API.get(`/api/preview/text?path=${encodeURIComponent(it.path)}`);
    const ta = h("textarea", { class: "viewer-text mono", spellcheck: "false", readonly: !d.writable || d.truncated });
    ta.value = d.text;
    const save = h("button", { class: "btn primary", disabled: true, onclick: async () => {
      const folder = it.path.split("/").slice(0, -1).join("/");
      const res = await fetch(`/api/files/upload?path=${encodeURIComponent(folder)}&name=${encodeURIComponent(it.name)}&overwrite=true`, {
        method: "POST", headers: { Authorization: `Bearer ${API.token}` }, body: new Blob([ta.value], { type: "text/plain" }) });
      if (!res.ok) return toast("Error: " + ((await res.json().catch(() => ({}))).detail || res.statusText));
      save.disabled = true; toast(t("Saved"));
    } }, "Save");
    ta.addEventListener("input", () => (save.disabled = false));
    stage.replaceChildren(h("div", { class: "viewer-textbox" }, ta,
      h("div", { class: "toolbar" }, d.writable && !d.truncated ? save : null,
        d.truncated ? h("span", { class: "muted" }, t("Only the first 1 MB is shown (read-only).")) : !d.writable ? h("span", { class: "muted" }, t("Read-only for you.")) : null)));
  }
  document.body.append(wrap);
  wrap.focus();
  show();
  return close;
}
