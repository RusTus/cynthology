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
      return app.replaceChildren(head, el("p", {}, size(d.size)), el("a", { class: "btn", href: dlUrl("") }, t("Download")));
    }
    const crumbs = el("div", { class: "crumbs" }, el("a", { onclick: () => go("") }, d.name));
    let acc = "";
    for (const part of (d.path || "").split("/").filter(Boolean)) {
      acc = acc ? acc + "/" + part : part;
      const target = acc;
      crumbs.append(" / ", el("a", { onclick: () => go(target) }, part));
    }
    const list = el("ul", { class: "items" }, d.items.length ? d.items.map((it) => {
      const p = path ? path + "/" + it.name : it.name;
      return el("li", {}, el("span", {}, it.dir ? "📁" : "📄"),
        el("span", { class: "n" }, it.dir ? el("a", { onclick: () => go(p) }, it.name) : it.name),
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
