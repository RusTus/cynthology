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
};

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
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
