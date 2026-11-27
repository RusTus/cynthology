// Antivirus (ClamAV): scan shared folders, quarantine what's found.
WM.register("antivirus", {
  name: "Antivirus", icon: "🛡", color: "#16a34a", w: 900, h: 640,
  open(body, win) {
    const pane = h("div", { class: "pane" });
    body.append(pane);
    let timer = null;
    win.cleanup.push(() => clearTimeout(timer));
    const when = (s) => (s ? new Date(s * 1000).toLocaleString() : "-");
    const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

    async function draw() {
      clearTimeout(timer);
      const [d, shares] = await Promise.all([API.get("/api/antivirus"), API.get("/api/shares")]);
      if (!d.installed) {
        pane.replaceChildren(h("h2", {}, "Antivirus"),
          h("div", { class: "card", style: "max-width:620px" },
            h("p", {}, "Scan your shared folders for viruses and other malware with ClamAV, on a schedule or whenever you like. ",
              "Infected files are moved to a quarantine, where you can restore or delete them."),
            h("p", { class: "muted" }, "Installing downloads the scanner and its virus signatures (a few hundred MB); they update themselves after that. ",
              "A scan needs about 1.5 GB of memory."),
            d.memory_ok ? null : h("div", { class: "note warn" }, "This NAS has less than 2 GB of memory: scans may be slow or fail."),
            d.installing ? h("div", { class: "muted" }, "⏳ ", t("Installing… this takes a few minutes."))
              : h("button", { class: "btn primary", onclick: async () => { await act(() => API.post("/api/antivirus/install"), "Installing"); draw(); } }, "Install antivirus")));
        if (d.installing) timer = setTimeout(draw, 5000);
        return;
      }
      const st = d.status, sig = d.signatures;
      const [pill, label] = st.running ? ["warn", t("Scanning…")] : st.state === "infected" ? ["bad", `${st.found} ${t("infected file(s) found")}`]
        : st.state === "ok" ? ["ok", t("No threats found")] : st.state === "warnings" ? ["warn", t("Finished - some files couldn't be read")]
        : st.state === "stopped" ? ["", t("Stopped")] : ["", t("Not scanned yet")];
      const found = await API.get("/api/antivirus/found");
      const c = d.config;
      pane.replaceChildren(h("h2", {}, "Antivirus"),
        h("div", { class: "cards", style: "grid-template-columns:repeat(auto-fill,minmax(300px,1fr))" },
          h("div", { class: "card" },
            h("div", {}, h("b", {}, t("Last scan")), " ", h("span", { class: "pill " + pill }, label)),
            h("div", { class: "muted" }, `${t("Started")}: ${when(st.started)}` + (st.finished && !st.running ? ` · ${t("finished")} ${when(st.finished)}` : "")),
            st.shares ? h("div", { class: "muted", translate: "no" }, st.shares.join(", ")) : null,
            h("div", { class: "muted" }, `${t("Next scheduled scan")}: ${d.next ? when(d.next) : t("none")}`),
            h("div", { class: "toolbar", style: "margin:6px 0 0" },
              st.running ? h("button", { class: "btn", onclick: async () => { await act(() => API.post("/api/antivirus/stop"), "Stopped"); draw(); } }, "Stop")
                : h("button", { class: "btn primary", disabled: !sig.ready, onclick: async () => { await act(() => API.post("/api/antivirus/scan", {}), "Scan started"); draw(); } }, "Scan now"),
              h("button", { class: "btn", onclick: async () => {
                const r = await API.get("/api/antivirus/log");
                modal(t("Scan log"), h("pre", { class: "mono", style: "max-height:60vh;overflow:auto;font-size:12px;white-space:pre-wrap" }, r.log || t("(empty)")));
              } }, "Log"))),
          h("div", { class: "card" },
            h("b", {}, t("Virus signatures")),
            sig.ready ? h("div", { class: "muted" }, `ClamAV ${sig.engine || ""} · ${t("version")} ${sig.version} · ${sig.date}`)
              : h("div", { class: "muted" }, "⏳ ", t("Downloading the signatures - scans can start when they're ready.")),
            h("div", { class: "muted", style: "font-size:12px" }, t("They update automatically several times a day.")))),
        settingsBox(c, shares),
        h("h3", { class: "pc-group", style: "margin-top:22px" }, "Quarantine and findings"),
        found.length ? h("table", {}, h("tr", {}, ...["When", "File", "Threat", "", ""].map((x) => h("th", { style: "text-align:left" }, x))),
          found.map((f) => h("tr", {},
            h("td", { style: "white-space:nowrap" }, when(f.time)),
            h("td", { class: "mono", style: "font-size:12px;word-break:break-all", translate: "no" }, f.path),
            h("td", { translate: "no" }, f.signature),
            h("td", {}, h("span", { class: "pill " + (f.state === "quarantined" ? "warn" : f.state === "reported" ? "bad" : "") },
              t({ quarantined: "In quarantine", reported: "Reported (not moved)", restored: "Restored", deleted: "Deleted" }[f.state] || f.state))),
            h("td", { style: "text-align:right;white-space:nowrap" },
              f.state === "quarantined" ? h("button", { class: "btn", onclick: async () => {
                if (!confirm(t("Put this file back where it was? Only do this if you're sure it's a false alarm."))) return;
                await act(() => API.post(`/api/antivirus/found/${f.id}/restore`), "Restored"); draw();
              } }, "Restore") : null,
              h("button", { class: "btn danger", onclick: async () => {
                if ((f.state === "quarantined" || f.state === "reported") && !confirm(t("Delete this file for good?"))) return;
                await act(() => API.del(`/api/antivirus/found/${f.id}`)); draw();
              } }, f.state === "quarantined" || f.state === "reported" ? "Delete" : "Clear")))))
          : h("p", { class: "muted" }, "Nothing found so far."));
      if (st.running || !sig.ready) timer = setTimeout(draw, 5000);
    }

    function settingsBox(c, shares) {
      const v = { ...c, shares: [...c.shares] };
      const box = h("div");
      const render = () => box.replaceChildren(
        h("h3", { class: "pc-group", style: "margin-top:22px" }, "Settings"),
        h("div", { class: "form" },
          h("label", {}, "What to scan", h("select", { onchange: (e) => { v.all_shares = e.target.value === "all"; render(); } },
            h("option", { value: "all", selected: v.all_shares }, t("All shared folders")), h("option", { value: "some", selected: !v.all_shares }, t("Only the shared folders I choose")))),
          v.all_shares ? null : h("div", { style: "display:flex;flex-wrap:wrap;gap:4px 14px" }, shares.map((s) => h("label", { class: "check", translate: "no" },
            h("input", { type: "checkbox", checked: v.shares.includes(s.name), onchange: (e) => { v.shares = e.target.checked ? [...v.shares, s.name] : v.shares.filter((x) => x !== s.name); } }), s.name))),
          h("label", {}, "Scheduled scan", h("div", { class: "toolbar", style: "margin:0" },
            h("select", { onchange: (e) => { v.schedule = e.target.value; render(); } }, [["off", "Off"], ["daily", "Every day"], ["weekly", "Every week"]].map(([k, l]) => h("option", { value: k, selected: v.schedule === k }, l))),
            v.schedule === "weekly" ? h("select", { onchange: (e) => (v.day = +e.target.value) }, DAYS.map((d, i) => h("option", { value: i, selected: v.day === i }, d))) : null,
            v.schedule !== "off" ? h("input", { type: "time", value: v.time, onchange: (e) => (v.time = e.target.value) }) : null)),
          h("label", {}, "When something is found", h("select", { onchange: (e) => (v.action = e.target.value) },
            h("option", { value: "quarantine", selected: v.action === "quarantine" }, t("Move it to quarantine (recommended)")),
            h("option", { value: "report", selected: v.action === "report" }, t("Only report it")))),
          h("label", {}, "Skip files larger than (MB)", h("input", { type: "number", min: 1, max: 4000, value: v.max_mb, style: "max-width:140px", onchange: (e) => (v.max_mb = +e.target.value) })),
          h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: async () => { await act(() => API.put("/api/antivirus/config", v), "Saved"); draw(); } }, "Save"))));
      render();
      return box;
    }
    draw();
  },
});
