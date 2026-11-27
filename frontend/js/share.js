// Public page for a sharing link (/s/<id>): no account needed. Talks only to /s/<id>/... .
(function () {
  const id = location.pathname.split("/")[2] || "";
  const base = `/s/${encodeURIComponent(id)}`;
  const app = document.getElementById("app");
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) k.startsWith("on") ? e.addEventListener(k.slice(2), v) : e.setAttribute(k, v);
    for (const k of kids.flat()) if (k != null) e.append(typeof k === "string" ? document.createTextNode(k) : k);
    return e;
  };
  const size = (b) => { if (!b) return "0 B"; const u = ["B", "KB", "MB", "GB", "TB"], i = Math.min(4, Math.floor(Math.log10(b) / 3)); return (b / 1000 ** i).toFixed(i ? 1 : 0) + " " + u[i]; };
  const dlUrl = (p) => `${base}/dl${p ? "?p=" + encodeURIComponent(p) : ""}`;
  const thumbUrl = (p, size = 256) => `${base}/thumb?size=${size}&p=${encodeURIComponent(p)}`;
  const ext = (n) => (n.split(".").pop() || "").toLowerCase();
  const IMG = new Set("jpg jpeg png gif webp bmp tif tiff heic heif avif".split(" "));
  const THUMB = new Set([...IMG, ..."mp4 m4v mov mkv webm avi pdf".split(" ")]);
  // photo lightbox (uses the preview size - downloads aren't counted until "Download")
  function lightbox(list, idx) {
    let i = idx;
    const img = el("img", { style: "max-width:100%;max-height:calc(100vh - 90px);object-fit:contain" });
    const cap = el("div", { class: "muted", style: "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" });
    const dl = el("a", { class: "btn", href: "#" }, t("Download"));
    const box = el("div", { class: "lb" }, el("div", { class: "lb-bar" }, cap, dl, el("button", { class: "btn ghost", onclick: () => close() }, "✕")),
      el("div", { class: "lb-stage" }, img),
      el("button", { class: "lb-nav prev", onclick: () => go(-1) }, "‹"), el("button", { class: "lb-nav next", onclick: () => go(1) }, "›"));
    const show = () => { img.src = thumbUrl(list[i], 1600); cap.textContent = list[i].split("/").pop() + (list.length > 1 ? `  ·  ${i + 1}/${list.length}` : ""); dl.href = dlUrl(list[i]); };
    const go = (d) => { if (i + d >= 0 && i + d < list.length) { i += d; show(); } };
    const key = (e) => { if (e.key === "Escape") close(); if (e.key === "ArrowLeft") go(-1); if (e.key === "ArrowRight") go(1); };
    let sx = null;
    box.addEventListener("pointerdown", (e) => { if (e.pointerType === "touch") sx = e.clientX; });
    box.addEventListener("pointerup", (e) => { if (sx != null && Math.abs(e.clientX - sx) > 60) go(e.clientX < sx ? 1 : -1); sx = null; });
    const close = () => { box.remove(); document.removeEventListener("keydown", key); };
    document.addEventListener("keydown", key);
    document.body.append(box); show();
  }
  let path = "";

  async function load() {
    let r;
    try { r = await fetch(`${base}/info${path ? "?p=" + encodeURIComponent(path) : ""}`, { credentials: "same-origin" }); }
    catch { return fail(t("The NAS can't be reached right now.")); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return fail(t(d.detail || "This link doesn't exist or has expired."));
    document.title = d.name;
    const head = el("div", { class: "head" }, el("div", { class: "icon" }, d.is_dir ? "📁" : "📄"),
      el("div", {}, el("h1", {}, d.name), el("div", { class: "muted" },
        `${t("Shared by")} ${d.owner}` + (d.expires ? ` · ${t("available until")} ${new Date(d.expires * 1000).toLocaleDateString()}` : ""))));
    if (!d.unlocked) return app.replaceChildren(head, passwordForm());
    if (d.used_up) return app.replaceChildren(head, el("p", { class: "err" }, t("This link has reached its download limit.")));
    if (!d.is_dir) {
      const pic = THUMB.has(ext(d.name)) ? el("img", { src: thumbUrl("", 1600), class: "single-preview", alt: "" }) : null;
      if (pic) pic.onerror = () => pic.remove();
      return app.replaceChildren(head, pic, el("p", {}, size(d.size)), el("a", { class: "btn", href: dlUrl("") }, t("Download")));
    }
    const crumbs = el("div", { class: "crumbs" }, el("a", { onclick: () => go("") }, d.name));
    let acc = "";
    for (const part of (d.path || "").split("/").filter(Boolean)) {
      acc = acc ? acc + "/" + part : part;
      const target = acc;
      crumbs.append(" / ", el("a", { onclick: () => go(target) }, part));
    }
    const photos = d.items.filter((it) => !it.dir && IMG.has(ext(it.name))).map((it) => (path ? path + "/" : "") + it.name);
    const list = el("ul", { class: "items" }, d.items.length ? d.items.map((it) => {
      const p = path ? path + "/" + it.name : it.name;
      let icon = el("span", { class: "ic" }, it.dir ? "📁" : "📄");
      if (!it.dir && THUMB.has(ext(it.name))) {
        const im = el("img", { src: thumbUrl(p), alt: "", loading: "lazy", class: "th" });
        im.onerror = () => im.replaceWith(el("span", { class: "ic" }, "📄"));
        icon = im;
      }
      const isPhoto = photos.includes(p);
      return el("li", {}, icon,
        el("span", { class: "n" }, it.dir ? el("a", { onclick: () => go(p) }, it.name)
          : isPhoto ? el("a", { onclick: () => lightbox(photos, photos.indexOf(p)) }, it.name) : it.name),
        el("span", { class: "s" }, it.dir ? "" : size(it.size)),
        el("a", { class: "dl", href: dlUrl(p) }, it.dir ? t("Download as zip") : t("Download")));
    }) : el("li", { class: "muted" }, t("This folder is empty.")));
    app.replaceChildren(head, el("div", {}, el("a", { class: "btn", href: dlUrl(path) }, t("Download all (zip)"))), crumbs, list);
  }
  function go(p) { path = p; load(); }
  function fail(msg) { app.replaceChildren(el("h1", {}, t("Not available")), el("p", { class: "muted" }, msg)); }
  function passwordForm() {
    const err = el("div", { class: "err" });
    const pw = el("input", { type: "password", autocomplete: "off", placeholder: t("Password") });
    const f = el("form", { onsubmit: async (e) => {
      e.preventDefault(); err.textContent = "";
      const r = await fetch(`${base}/unlock`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw.value }) });
      if (r.ok) return load();
      const d = await r.json().catch(() => ({}));
      err.textContent = t(d.detail || "wrong password");
    } }, el("div", { class: "muted" }, t("This link is protected with a password.")), pw, el("button", { class: "btn", type: "submit" }, t("Open")), err);
    setTimeout(() => pw.focus(), 0);
    return f;
  }
  load();
})();
