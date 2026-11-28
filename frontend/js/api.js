// DOM append()/replaceChildren() turn null/false into the text "null"/"false". Every page here builds
// children with `cond ? el : null`, so skip empty values (and flatten arrays) globally.
for (const m of ["append", "prepend", "replaceChildren"]) {
  const orig = Element.prototype[m];
  Element.prototype[m] = function (...kids) { return orig.apply(this, kids.flat(Infinity).filter((k) => k != null && k !== false)); };
}

// Tiny API client + DOM helpers shared by every app.
const API = {
  token: sessionStorage.getItem("cyn-token") || "",
  async call(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.token}` },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401 && !path.startsWith("/api/auth/")) { location.reload(); throw new Error("signed out"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail?.[0]?.msg || data.detail || res.statusText);
    return data;
  },
  get(p) { return this.call("GET", p); },
  post(p, b = {}) { return this.call("POST", p, b); },
  put(p, b = {}) { return this.call("PUT", p, b); },
  del(p) { return this.call("DELETE", p); },
  async raw(method, path, blob) {          // (binary body, e.g. a picture)
    const res = await fetch(path, { method, body: blob, headers: { "Content-Type": "application/octet-stream", Authorization: `Bearer ${this.token}` } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  },
};

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  const tr = typeof t === "function" && attrs?.translate !== "no";
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, tr && (k === "placeholder" || k === "title" || k === "label") ? t(v) : v);
  }
  const trKids = tr && translatable(el, attrs);
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : trKids && typeof kid === "string" ? t(kid) : String(kid));
  return el;
}

function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log10(b) / 3));
  return `${(b / 10 ** (3 * i)).toFixed(i >= 3 ? 2 : 0)} ${u[i]}`;
}
function fmtUptime(s) {
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d}d ${hr}h ${m}m`;
}
function toast(msg) {
  if (typeof window.t === "function" && typeof msg === "string") {
    msg = msg.startsWith("Error: ") ? window.t("Error: ") + msg.slice(7) : window.t(msg);
  }
  const t = h("div", { class: "toast" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 3500);
}
async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast("Error: " + e.message); throw e; }
}
// side-nav + pane layout used by multi-section apps
function sectioned(sections, body) {
  const pane = h("div", { class: "pane" });
  const side = h("nav", { class: "side" });
  const show = (key) => {
    side.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.k === key));
    side.querySelector("button.on")?.scrollIntoView?.({ block: "nearest", inline: "nearest" });   // (phones: the tab strip)
    // fresh container per visit: a slow, earlier section that finishes loading late
    // writes into its own (now detached) box instead of the page being shown
    const box = h("div");
    pane.replaceChildren(box);
    sections[key].render(box);
  };
  for (const [k, s] of Object.entries(sections)) side.append(h("button", { "data-k": k, onclick: () => show(k) }, s.label));
  body.append(side, pane);
  show(Object.keys(sections)[0]);
  show.pane = pane;
  return show;
}

// A value shown so it can be copied (passwords, keys): selectable, monospace, with a Copy button.
function copyField(value) {
  const inp = h("input", { class: "mono", readonly: true, value, style: "flex:1;min-width:0;font-size:14px;letter-spacing:.5px", translate: "no",
    onfocus: (e) => e.target.select() });
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); } catch { inp.select(); document.execCommand("copy"); }
    toast(t_("Copied"));
  };
  return h("div", { style: "display:flex;gap:6px;align-items:center" }, inp, h("button", { class: "btn", type: "button", onclick: copy }, t_("Copy")));
}

// Install notes ("root password: abc123. Connect to ...") with every password/key in a copyable field.
function notesView(text) {
  const re = /((?:password|passwort|token|key|secret)[^:\n]{0,30}:\s*)([A-Za-z0-9_\-+/=]{8,})/gi;
  const out = h("div", { class: "form" });
  let last = 0, m;
  while ((m = re.exec(text))) {
    const before = text.slice(last, m.index) + m[1];
    if (before.trim()) out.append(h("p", { style: "margin:6px 0 4px" }, before.trim()));
    out.append(copyField(m[2]));
    last = m.index + m[0].length;
  }
  const rest = text.slice(last).replace(/^[.,;]\s*/, "").trim();
  if (rest) out.append(h("p", { style: "margin:8px 0 0" }, rest));
  return out;
}

// Small modal dialog; returns a function that closes it.
function modal(title, content, buttons = [["Close", null, "primary"]]) {
  const wrap = h("div", { class: "wiz", onclick: (e) => { if (e.target === wrap) close(); } });
  const close = () => wrap.remove();
  wrap.append(h("div", { class: "wiz-card", style: "max-width:520px" }, h("h2", {}, title), content,
    h("div", { class: "toolbar", style: "justify-content:flex-end;margin-top:14px" },
      buttons.map(([label, fn, cls]) => h("button", { class: "btn " + (cls || ""), onclick: fn || close }, label)))));
  document.body.append(wrap);
  return close;
}
