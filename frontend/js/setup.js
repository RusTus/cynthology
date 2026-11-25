// First-run setup wizard (shown to an administrator until it has been completed once).
async function runSetupWizard() {
  const g = await API.get("/api/setup/general");
  const disks = await API.get("/api/storage/disks");
  const freeDisks = disks.filter((d) => !d.in_use && !d.system);
  const st = { hostname: g.hostname, timezone: guessTimezone(g), ntp: true, https_redirect: true, storage: freeDisks.length ? "now" : "later" };
  let step = 0;

  const card = h("div", { class: "wiz-card" });
  const overlay = h("div", { class: "wiz" }, card);
  document.body.append(overlay);

  const steps = [
    {
      title: "Welcome to Cynthology",
      body: () => [
        h("p", {}, "A few quick settings and your NAS is ready. You can change all of them later in Control Panel."),
        h("ol", { class: "wiz-list" }, h("li", {}, "Server name & time"), h("li", {}, "Secure connection (HTTPS)"), h("li", {}, "Storage")),
      ],
    },
    {
      title: "Server name & time",
      body: () => {
        const list = h("datalist", { id: "tz-list" }, g.timezones.map((z) => h("option", { value: z })));
        return [
          h("label", {}, "Server name (how it appears on your network)",
            h("input", { value: st.hostname, maxlength: 63, oninput: (e) => (st.hostname = e.target.value.trim()) })),
          h("label", {}, "Time zone", h("input", { list: "tz-list", value: st.timezone, oninput: (e) => (st.timezone = e.target.value.trim()) }), list),
          h("label", { class: "check" }, h("input", { type: "checkbox", checked: st.ntp, onchange: (e) => (st.ntp = e.target.checked) }),
            "Keep the clock in sync with internet time servers (NTP)"),
        ];
      },
      validate: () => {
        if (!/^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(st.hostname)) return "Server name: letters, digits and '-' only.";
        if (!g.timezones.includes(st.timezone)) return "Pick a time zone from the list (e.g. America/Chicago).";
      },
    },
    {
      title: "Secure connection",
      body: () => [
        h("p", {}, "Cynthology always offers an encrypted connection on port ", h("b", {}, "5001"), "."),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: st.https_redirect, onchange: (e) => (st.https_redirect = e.target.checked) }),
          "Always use HTTPS (send visitors of http://…:5000 to https://…:5001)"),
        h("div", { class: "note" }, "The certificate is self-signed, so the first time your browser shows a warning. ",
          "That is expected on a home network: choose “Advanced” → “Proceed”. You can install your own certificate in Control Panel → Security."),
      ],
    },
    {
      title: "Storage",
      body: () => freeDisks.length ? [
        h("p", {}, `Found ${freeDisks.length} unused drive(s): `, freeDisks.map((d) => `${d.name} (${fmtBytes(d.size)})`).join(", "), "."),
        h("label", { class: "check" }, h("input", { type: "radio", name: "wst", checked: st.storage === "now", onchange: () => (st.storage = "now") }),
          "Create a storage pool now (recommended: SHR)"),
        h("label", { class: "check" }, h("input", { type: "radio", name: "wst", checked: st.storage === "later", onchange: () => (st.storage = "later") }),
          "Later, in Storage Manager"),
      ] : [h("p", {}, "No unused drives found. Add drives and set them up later in Storage Manager.")],
    },
  ];

  function draw(err) {
    const s = steps[step];
    card.replaceChildren(
      h("div", { class: "wiz-steps" }, steps.map((_, i) => h("span", { class: i === step ? "on" : i < step ? "done" : "" }))),
      h("h2", {}, s.title),
      h("div", { class: "form" }, s.body()),
      h("div", { class: "err" }, err || ""),
      h("div", { class: "wiz-nav" },
        step > 0 ? h("button", { class: "btn", onclick: () => { step--; draw(); } }, "Back") : h("span"),
        h("button", { class: "btn primary", onclick: next }, step === steps.length - 1 ? "Finish" : "Next")));
  }

  async function next() {
    const err = steps[step].validate?.();
    if (err) return draw(err);
    if (step < steps.length - 1) { step++; return draw(); }
    card.replaceChildren(h("h2", {}, "Applying settings…"), h("p", { class: "muted" }, "This takes a few seconds."));
    try {
      const r = await API.put("/api/setup/general", { hostname: st.hostname, timezone: st.timezone, ntp: st.ntp });
      if (r.restarting) await waitForServer();
      // last: once the redirect is on, API calls over plain HTTP would be bounced to HTTPS
      await API.post("/api/setup/finish", { https_redirect: st.https_redirect });
    } catch (e) { step = 1; return draw("Could not apply: " + e.message); }
    overlay.remove();
    if (st.https_redirect && location.protocol === "http:") return switchToHttps(st.storage === "now");
    if (st.storage === "now") openCreatePool();
    toast("Setup complete");
  }

  draw();
}

function guessTimezone(g) {
  const browser = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return g.timezone && g.timezone !== "Etc/UTC" && g.timezone !== "UTC" ? g.timezone : g.timezones.includes(browser) ? browser : "UTC";
}

async function waitForServer() {
  await new Promise((r) => setTimeout(r, 4000));
  for (let i = 0; i < 30; i++) {
    try { await API.get("/api/auth/me"); return; } catch { await new Promise((r) => setTimeout(r, 1000)); }
  }
}

// sessionStorage is per-origin, so hand the session over to the HTTPS origin in the URL fragment
// (fragments are never sent to the server or logged).
function switchToHttps(thenStorage) {
  const frag = `token=${encodeURIComponent(API.token)}${thenStorage ? "&open=create-pool" : ""}`;
  location.href = `https://${location.hostname}:5001/#${frag}`;
}
