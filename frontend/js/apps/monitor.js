// Resource Monitor: live CPU / memory / network / disk charts drawn on canvas.
function lineChart(canvas, series, max) {
  const dpr = devicePixelRatio || 1, w = canvas.clientWidth, ht = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = ht * dpr;
  const c = canvas.getContext("2d");
  c.scale(dpr, dpr);
  c.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--line");
  for (let i = 1; i < 4; i++) { c.beginPath(); c.moveTo(0, (ht * i) / 4); c.lineTo(w, (ht * i) / 4); c.stroke(); }
  const top = max || Math.max(1, ...series.flatMap((s) => s.data));
  for (const s of series) {
    c.beginPath(); c.strokeStyle = s.color; c.lineWidth = 2;
    s.data.forEach((v, i) => { const x = (i / 59) * w, y = ht - (v / top) * (ht - 4); i ? c.lineTo(x, y) : c.moveTo(x, y); });
    c.stroke();
  }
}

WM.register("monitor", {
  name: "Resource Monitor", icon: "📈", color: "#7a5ea8", w: 820, h: 600,
  open(body, win) {
    const pane = h("div", { class: "pane" });
    const mk = (title) => { const cv = h("canvas", { class: "chart" }), lbl = h("span", { class: "muted" }); pane.querySelector(".grid2").append(h("div", { class: "card" }, h("b", {}, title, " "), lbl, cv)); return { cv, lbl }; };
    pane.append(h("h2", {}, "Performance"), h("div", { class: "grid2" }));
    const cpu = mk("CPU"), mem = mk("Memory"), net = mk("Network"), dsk = mk("Disk");
    body.append(pane);
    const hist = { cpu: [], mem: [], rx: [], tx: [], rd: [], wr: [] };
    let last = null;
    const push = (k, v) => { hist[k].push(v); if (hist[k].length > 60) hist[k].shift(); };
    const tick = async () => {
      const s = await API.get("/api/system/stats");
      push("cpu", s.cpu); push("mem", s.mem.percent);
      if (last) {
        const dt = s.t - last.t || 1;
        push("rx", (s.net.recv - last.net.recv) / dt); push("tx", (s.net.sent - last.net.sent) / dt);
        push("rd", (s.disk.read - last.disk.read) / dt); push("wr", (s.disk.write - last.disk.write) / dt);
      }
      last = s;
      cpu.lbl.textContent = `${s.cpu.toFixed(0)}% · load ${s.load.map((x) => x.toFixed(2)).join(" ")}`;
      mem.lbl.textContent = `${s.mem.percent.toFixed(0)}% of ${fmtBytes(s.mem.total)}`;
      net.lbl.textContent = `↓ ${fmtBytes(hist.rx.at(-1) || 0)}/s ↑ ${fmtBytes(hist.tx.at(-1) || 0)}/s`;
      dsk.lbl.textContent = `R ${fmtBytes(hist.rd.at(-1) || 0)}/s W ${fmtBytes(hist.wr.at(-1) || 0)}/s`;
      lineChart(cpu.cv, [{ data: hist.cpu, color: "#1f7a8c" }], 100);
      lineChart(mem.cv, [{ data: hist.mem, color: "#7a5ea8" }], 100);
      lineChart(net.cv, [{ data: hist.rx, color: "#2e9d5b" }, { data: hist.tx, color: "#d9902a" }]);
      lineChart(dsk.cv, [{ data: hist.rd, color: "#3a7bd5" }, { data: hist.wr, color: "#d24b4b" }]);
    };
    tick();
    const t = setInterval(tick, 2000);
    win.cleanup.push(() => clearInterval(t));
  },
});
