// Minimal window manager: open/focus/minimise/maximise/close, drag by title bar, taskbar entries.
const WM = {
  apps: {},          // id -> {name, icon, color, open(body, win)}
  wins: {},          // id -> {el, task}
  z: 100,
  register(id, app) { this.apps[id] = app; },
  focus(id) {
    const w = this.wins[id];
    if (!w) return;
    w.el.style.zIndex = ++this.z;
    w.el.classList.remove("hidden");
    document.querySelectorAll(".task").forEach((t) => t.classList.remove("active"));
    w.task.classList.add("active");
  },
  open(id) {
    if (this.wins[id]) return this.focus(id);
    const app = this.apps[id];
    const n = Object.keys(this.wins).length;
    const body = h("div", { class: "win-body" });
    const el = h("div", { class: "win", style: `left:${140 + n * 28}px;top:${70 + n * 28}px;width:${app.w || 880}px;height:${app.h || 560}px` },
      h("div", { class: "win-title" },
        appTile(id, 20), h("span", {}, t(app.name)), h("span", { class: "sp" }),
        h("button", { title: "Minimise", onclick: () => el.classList.add("hidden") }, "–"),
        h("button", { title: "Maximise", onclick: () => el.classList.toggle("max") }, "□"),
        h("button", { class: "close", title: "Close", onclick: () => this.close(id) }, "✕")),
      body);
    el.addEventListener("mousedown", () => this.focus(id));
    this.drag(el);
    const task = h("button", { class: "task", onclick: () => {
      const hidden = el.classList.contains("hidden");
      if (!hidden && el.style.zIndex == this.z) el.classList.add("hidden"); else this.focus(id);
    } }, appTile(id, 18), h("span", {}, t(app.name)));
    document.getElementById("tasks").append(task);
    document.getElementById("windows").append(el);
    this.wins[id] = { el, task, cleanup: [] };
    this.focus(id);
    app.open(body, this.wins[id]);
  },
  close(id) {
    const w = this.wins[id];
    if (!w) return;
    w.cleanup.forEach((f) => f());
    w.el.remove(); w.task.remove();
    delete this.wins[id];
  },
  drag(el) {
    const bar = el.querySelector(".win-title");
    bar.addEventListener("mousedown", (e) => {
      if (e.target.tagName === "BUTTON" || el.classList.contains("max")) return;
      const sx = e.clientX - el.offsetLeft, sy = e.clientY - el.offsetTop;
      const move = (ev) => {
        el.style.left = Math.max(-el.offsetWidth + 80, ev.clientX - sx) + "px";
        el.style.top = Math.max(40, ev.clientY - sy) + "px";
      };
      const up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); };
      addEventListener("mousemove", move); addEventListener("mouseup", up);
    });
    bar.addEventListener("dblclick", () => el.classList.toggle("max"));
  },
};
