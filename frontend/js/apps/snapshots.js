// Snapshots: schedules, retention and on-demand snapshots of shared folders (Btrfs).
WM.register("snapshots", {
  name: "Snapshots", icon: "🕒", color: "#3a7bd5", w: 900, h: 560,
  open(body) {
    const pane = h("div", { class: "pane" });
    body.append(pane);
    const when = (n) => n ? new Date(n.replace(/^@GMT-(\d+)\.(\d+)\.(\d+)-(\d+)\.(\d+)\.(\d+)$/, "$1-$2-$3T$4:$5:$6Z")).toLocaleString() : "never";

    async function render() {
      const rows = await API.get("/api/snapshots");
      pane.replaceChildren(h("h2", {}, "Snapshots"),
        h("div", { class: "note" }, "Snapshots are instant, space-efficient, read-only copies of a shared folder. ",
          "On Windows, right-click a file or folder on the share → Properties → ", h("b", {}, "Previous Versions"),
          " to restore. In File Station, tick “Hidden files” and open .snapshots."),
        h("br"),
        rows.length ? h("table", {},
          h("tr", {}, h("th", {}, "Shared folder"), h("th", {}, "Schedule"), h("th", {}, "Keep"), h("th", {}, "Snapshots"), h("th", {}, "Latest"), h("th", {})),
          rows.map((r) => {
            if (!r.supported) return h("tr", {}, h("td", {}, h("b", {}, r.share)), h("td", { colspan: 5, class: "muted" }, "Not on a Btrfs volume - snapshots unavailable"));
            const pol = { schedule: r.schedule, keep: r.keep };
            const save = () => act(() => API.put(`/api/snapshots/${r.share}`, pol), "Snapshot schedule saved");
            return h("tr", {},
              h("td", {}, h("b", {}, r.share)),
              h("td", {}, h("select", { onchange: (e) => { pol.schedule = e.target.value; save(); } },
                ["off", "hourly", "daily", "weekly"].map((v) => h("option", { value: v, selected: v === r.schedule }, v[0].toUpperCase() + v.slice(1))))),
              h("td", {}, h("input", { type: "number", min: 0, max: 1000, value: r.keep, style: "width:80px", title: "0 = keep all",
                onchange: (e) => { pol.keep = +e.target.value; save().then(render); } })),
              h("td", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); showList(r.share); } }, String(r.count))),
              h("td", { class: "muted" }, when(r.latest)),
              h("td", {}, h("button", { class: "btn", onclick: async () => { await act(() => API.post(`/api/snapshots/${r.share}`), "Snapshot taken"); render(); } }, "Take snapshot")));
          }))
          : h("p", { class: "muted" }, "No shared folders yet."));
    }

    async function showList(share) {
      const snaps = await API.get(`/api/snapshots/${share}`);
      pane.replaceChildren(h("h2", {}, `Snapshots of ${share}`),
        h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: render }, "← Back")),
        snaps.length ? h("table", {}, h("tr", {}, h("th", {}, "Taken"), h("th", {}, "Name"), h("th", {})),
          snaps.map((s) => h("tr", {}, h("td", {}, new Date(s.time).toLocaleString()), h("td", { class: "mono muted" }, s.name),
            h("td", {}, h("button", { class: "btn danger", onclick: async () => {
              if (!confirm(`Delete snapshot ${s.name}?`)) return;
              await act(() => API.del(`/api/snapshots/${share}/${encodeURIComponent(s.name)}`), "Snapshot deleted"); showList(share);
            } }, "Delete")))))
          : h("p", { class: "muted" }, "No snapshots yet."));
    }
    render();
  },
});
