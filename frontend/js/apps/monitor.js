// Resource Monitor: live charts (2 s) and history (10 s samples for a day, 5-minute averages for a
// month, hourly for a year - backend metrics.py), plus how full each volume has been.

// Series colors (validated for CVD separation and 3:1 contrast on the dark surface; light-mode
// orange is under 3:1, so every chart with 2+ series has a visible legend).
const MON_COLORS = { light: ["#257de0", "#f47230", "#00b894"], dark: ["#338ced", "#e76624", "#00a888"] };
const monColors = () => MON_COLORS[document.documentElement.dataset.theme === "dark" ? "dark" : "light"];

// tsChart(series, opts) -> element.  series: [{ name, data: [number|null] }], opts.t: unix seconds,
// opts.max (fixed top, e.g. 100 for %), opts.fmt (value -> text). Nulls are gaps.
function tsChart(series, opts) {
  const cv = h("canvas", { class: "chart", style: "cursor:crosshair" });
  const tip = h("div", { class: "chart-tip", style: "display:none" });
  const legend = series.length > 1 ? h("div", { class: "chart-legend" }, series.map((s, i) =>
    h("span", {}, h("i", { style: `background:${monColors()[i]}` }), s.name))) : null;
  const wrap = h("div", { style: "position:relative" }, cv, tip);
  const box = h("div", {}, legend, wrap);
  const fmt = opts.fmt || ((v) => v.toFixed(0));
  let hover = null;
  const draw = () => {
    const dpr = devicePixelRatio || 1, w = cv.clientWidth, ht = cv.clientHeight;
    if (!w) return;
    cv.width = w * dpr; cv.height = ht * dpr;
    const c = cv.getContext("2d"); c.scale(dpr, dpr);
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue("--muted").trim(), grid = css.getPropertyValue("--line").trim();
    const L = 62, B = 18, T = 8, pw = w - L - 8, ph = ht - B - T;
    const vals = series.flatMap((s) => s.data).filter((v) => v != null);
    const top = opts.max || niceTop(Math.max(1, ...vals));
    const n = opts.t.length, X = (i) => L + (n > 1 ? (i / (n - 1)) * pw : 0), Y = (v) => T + ph - (v / top) * ph;
    c.font = "11px system-ui"; c.fillStyle = ink; c.strokeStyle = grid; c.lineWidth = 1;
    c.textAlign = "right"; c.textBaseline = "middle";
    for (let k = 0; k <= 4; k++) {           // recessive grid + y labels
      const v = (top * k) / 4, y = Math.round(Y(v)) + 0.5;
      c.beginPath(); c.moveTo(L, y); c.lineTo(L + pw, y); c.stroke();
      if (k % 2 === 0) c.fillText(fmt(v), L - 6, y);
    }
    c.textBaseline = "alphabetic";
    if (n > 1) {
      const span = (opts.t[n - 1] - opts.t[0]) || 1;
      const K = Math.max(1, Math.min(4, n - 1, Math.floor(pw / 95)));   // as many time labels as fit
      for (let k = 0, prev = -1; k <= K; k++) {
        const i = Math.round(((n - 1) * k) / K), d = new Date(opts.t[i] * 1000);
        if (i === prev) continue;
        prev = i;
        const txt = span > 2 * 86400 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
          : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", ...(span < 600 ? { second: "2-digit" } : {}) });
        c.textAlign = k === 0 ? "left" : k === K ? "right" : "center";
        c.fillText(txt, Math.round(X(i)), ht - 4);
      }
    }
    series.forEach((s, si) => {             // 2px lines, gaps where there is no data
      c.strokeStyle = monColors()[si]; c.lineWidth = 2; c.lineJoin = "round"; c.beginPath();
      let pen = false;
      s.data.forEach((v, i) => { if (v == null) { pen = false; return; } pen ? c.lineTo(X(i), Y(v)) : c.moveTo(X(i), Y(v)); pen = true; });
      c.stroke();
    });
    if (hover != null && hover < n) {        // crosshair + markers with a surface ring
      const x = Math.round(X(hover)) + 0.5;
      c.strokeStyle = ink; c.lineWidth = 1; c.beginPath(); c.moveTo(x, T); c.lineTo(x, T + ph); c.stroke();
      series.forEach((s, si) => {
        const v = s.data[hover]; if (v == null) return;
        c.fillStyle = css.getPropertyValue("--panel").trim(); c.beginPath(); c.arc(X(hover), Y(v), 6, 0, 7); c.fill();
        c.fillStyle = monColors()[si]; c.beginPath(); c.arc(X(hover), Y(v), 4, 0, 7); c.fill();
      });
    }
    box._geom = { L, pw, n };
  };
  cv.addEventListener("pointermove", (e) => {
    const g = box._geom; if (!g || g.n < 1) return;
    const r = cv.getBoundingClientRect(), x = e.clientX - r.left;
    hover = Math.max(0, Math.min(g.n - 1, Math.round(((x - g.L) / g.pw) * (g.n - 1))));
    const d = new Date(opts.t[hover] * 1000);
    tip.replaceChildren(h("div", { class: "muted" }, d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" })),
      ...series.map((s, i) => h("div", {}, h("i", { style: `background:${monColors()[i]}` }),
        h("b", {}, s.data[hover] == null ? "–" : fmt(s.data[hover])), series.length > 1 ? " " + s.name : "")));
    tip.style.display = "block";
    tip.style.left = Math.max(0, Math.min(x + 14, r.width - tip.offsetWidth - 4)) + "px";
    draw();
  });
  cv.addEventListener("pointerleave", () => { hover = null; tip.style.display = "none"; draw(); });
  new ResizeObserver(() => draw()).observe(cv);
  box.redraw = draw;
  requestAnimationFrame(draw);
  return box;
}
function niceTop(v) { const p = 10 ** Math.floor(Math.log10(v)), m = v / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; }
const fmtRate = (v) => fmtBytes(v) + "/s";
const fmtPct = (v) => v.toFixed(0) + "%";

WM.register("monitor", {
  name: "Resource Monitor", icon: "📈", color: "#7a5ea8", w: 900, h: 640,
  open(body, win) {
    let timer = null;
    const stop = () => { if (timer) clearInterval(timer); timer = null; };
    win.cleanup.push(stop);
    const card = (title, lbl, chart) => h("div", { class: "card" }, h("div", { style: "margin-bottom:6px" }, h("b", {}, title), " ", lbl || ""), chart);
    const muted = (txt) => h("span", { class: "muted" }, txt);

    const live = (pane) => {
      const N = 60, ts = [], hist = { cpu: [], mem: [], rx: [], tx: [], rd: [], wr: [] };
      const lbl = { cpu: muted(""), mem: muted(""), net: muted(""), dsk: muted("") };
      const ch = {
        cpu: tsChart([{ name: "CPU", data: hist.cpu }], { t: ts, max: 100, fmt: fmtPct }),
        mem: tsChart([{ name: "Memory", data: hist.mem }], { t: ts, max: 100, fmt: fmtPct }),
        net: tsChart([{ name: t("Received"), data: hist.rx }, { name: t("Sent"), data: hist.tx }], { t: ts, fmt: fmtRate }),
        dsk: tsChart([{ name: t("Read"), data: hist.rd }, { name: t("Write"), data: hist.wr }], { t: ts, fmt: fmtRate }),
      };
      pane.append(h("div", { class: "grid2" }, card("CPU", lbl.cpu, ch.cpu), card("Memory", lbl.mem, ch.mem),
        card("Network", lbl.net, ch.net), card("Disk", lbl.dsk, ch.dsk)));
      let last = null;
      const push = (arr, v) => { arr.push(v); if (arr.length > N) arr.shift(); };
      const tick = async () => {
        const s = await API.get("/api/system/stats");
        if (last) {
          const dt = s.t - last.t || 1;
          push(ts, Math.round(s.t)); push(hist.cpu, s.cpu); push(hist.mem, s.mem.percent);
          push(hist.rx, (s.net.recv - last.net.recv) / dt); push(hist.tx, (s.net.sent - last.net.sent) / dt);
          push(hist.rd, (s.disk.read - last.disk.read) / dt); push(hist.wr, (s.disk.write - last.disk.write) / dt);
          Object.values(ch).forEach((c) => c.redraw());
        }
        last = s;
        lbl.cpu.textContent = `${s.cpu.toFixed(0)}% · load ${s.load.map((x) => x.toFixed(2)).join(" ")}`;
        lbl.mem.textContent = `${s.mem.percent.toFixed(0)}% ${t("of")} ${fmtBytes(s.mem.total)}`;
        lbl.net.textContent = `↓ ${fmtRate(hist.rx.at(-1) || 0)} ↑ ${fmtRate(hist.tx.at(-1) || 0)}`;
        lbl.dsk.textContent = `R ${fmtRate(hist.rd.at(-1) || 0)} W ${fmtRate(hist.wr.at(-1) || 0)}`;
      };
      tick(); timer = setInterval(tick, 2000);
    };

    const history = (pane) => {
      const ranges = [["1h", "1 hour"], ["24h", "24 hours"], ["7d", "7 days"], ["30d", "30 days"], ["1y", "1 year"]];
      let rng = "24h";
      try { rng = localStorage.getItem("cyn-mon-range") || "24h"; } catch { /* private window */ }
      const bar = h("div", { class: "seg", style: "margin-bottom:12px" });
      const out = h("div", {});
      pane.append(bar, out);
      const load = async () => {
        bar.replaceChildren(...ranges.map(([k, l]) => h("button", { class: rng === k ? "on" : "", onclick: () => {
          rng = k; try { localStorage.setItem("cyn-mon-range", k); } catch { /* ignore */ } load();
        } }, l)));
        out.replaceChildren(h("p", { class: "muted" }, "Loading…"));
        const vr = rng === "1y" ? "1y" : rng === "30d" ? "30d" : "7d";
        const [d, vols] = await Promise.all([API.get(`/api/system/history?range=${rng}`), API.get(`/api/system/history/volumes?range=${vr}`)]);
        if (!d.cpu.some((v) => v != null)) {
          out.replaceChildren(h("p", { class: "muted" }, "No history yet - the NAS takes a sample every 10 seconds and saves them once a minute."));
          return;
        }
        const avg = (a) => { const v = a.filter((x) => x != null); return v.length ? v.reduce((p, x) => p + x, 0) / v.length : 0; };
        const peak = (a) => Math.max(0, ...a.filter((x) => x != null));
        const A = t("average"), P = t("peak");
        out.replaceChildren(h("div", { class: "grid2" },
          card("CPU", muted(`${A} ${fmtPct(avg(d.cpu))} · ${P} ${fmtPct(peak(d.cpu))}`), tsChart([{ name: "CPU", data: d.cpu }], { t: d.t, max: 100, fmt: fmtPct })),
          card("Memory", muted(`${A} ${fmtPct(avg(d.mem))} · ${P} ${fmtPct(peak(d.mem))}`), tsChart([{ name: "Memory", data: d.mem }], { t: d.t, max: 100, fmt: fmtPct })),
          card("Network", muted(`${P} ↓ ${fmtRate(peak(d.rx))} ↑ ${fmtRate(peak(d.tx))}`),
            tsChart([{ name: t("Received"), data: d.rx }, { name: t("Sent"), data: d.tx }], { t: d.t, fmt: fmtRate })),
          card("Disk", muted(`${P} R ${fmtRate(peak(d.rd))} W ${fmtRate(peak(d.wr))}`),
            tsChart([{ name: t("Read"), data: d.rd }, { name: t("Write"), data: d.wr }], { t: d.t, fmt: fmtRate })),
          card("Load average", muted(`${P} ${peak(d.load).toFixed(2)}`), tsChart([{ name: "Load", data: d.load }], { t: d.t, fmt: (v) => v.toFixed(1) })),
          d.temp.some((v) => v != null)
            ? card("Temperature", muted(`${P} ${peak(d.temp).toFixed(0)} °C`), tsChart([{ name: "°C", data: d.temp }], { t: d.t, fmt: (v) => v.toFixed(0) + " °C" }))
            : null));
        if (vols.length) {
          const all = [...new Set(vols.flatMap((v) => v.t))].sort((a, b) => a - b), shown = vols.slice(0, 3);
          out.append(h("h3", { class: "pc-group", style: "margin-top:18px" }, "Volume usage"),
            h("div", { class: "card" }, muted(shown.map((v) => `${v.name}: ${fmtBytes(v.used.at(-1))} ${t("of")} ${fmtBytes(v.size)}`).join(" · ")),
              tsChart(shown.map((v) => ({ name: v.name, data: all.map((x) => { const i = v.t.indexOf(x); return i < 0 ? null : (100 * v.used[i]) / v.size; }) })),
                { t: all, max: 100, fmt: (x) => x.toFixed(1) + "%" })));
        }
      };
      load();
    };

    const tabs = { live: ["Live", live], history: ["History", history] };
    const pane = h("div", { class: "pane" });
    const show = (k) => {
      stop(); pane.replaceChildren();
      pane.append(h("div", { class: "seg", style: "margin-bottom:14px" },
        Object.entries(tabs).map(([key, [label]]) => h("button", { class: key === k ? "on" : "", onclick: () => show(key) }, label))));
      tabs[k][1](pane);
    };
    body.append(pane);
    show("live");
  },
});
