// Personal settings: 2-step verification (authenticator app + backup codes).
WM.register("personal", {
  name: "Personal", icon: "👤", color: "#5b6b7d", w: 720, h: 600,
  open(body) {
    const pane = h("div", { class: "pane" });
    body.append(pane);
    const render = async () => {
      const s = await API.get("/api/2fa");
      let theme = "dark";
      try { theme = localStorage.getItem("cyn-theme") || "dark"; } catch {}
      pane.replaceChildren(h("h2", {}, "Personal settings"),
        h("h3", { class: "pc-group" }, "Appearance"),
        h("label", {}, "Theme ", h("select", { onchange: (e) => {
          try { localStorage.setItem("cyn-theme", e.target.value); } catch {}
          const v = e.target.value === "auto" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : e.target.value;
          document.documentElement.dataset.theme = v;
        } }, [["dark", "Dark"], ["light", "Light"], ["auto", "Same as this computer"]].map(([v, l]) => h("option", { value: v, selected: theme === v }, l)))),
        h("h3", { class: "pc-group", style: "margin-top:18px" }, "2-step verification"));
      if (!s.enabled) {
        pane.append(h("p", {}, "Protect your account with a code from an authenticator app on your phone, in addition to your password."),
          s.required ? h("div", { class: "note warn" }, "Required for administrators on this NAS.") : null,
          h("button", { class: "btn primary", onclick: () => twoFactorSetup(pane, render) }, "Turn on 2-step verification"));
        return;
      }
      const askCode = (why) => prompt(`${why}\n\nEnter a current code from your authenticator app (or a backup code):`);
      pane.append(
        h("div", { class: "note" }, "✓ 2-step verification is on. Signing in needs your password and a code from your authenticator app."),
        h("p", { class: "muted" }, `${s.backup_codes_left} unused backup code(s) left.`),
        h("div", { class: "toolbar" },
          h("button", { class: "btn", onclick: async () => {
            const code = askCode("Create a new set of backup codes (the old ones stop working)."); if (!code) return;
            const r = await act(() => API.post("/api/2fa/backup-codes", { code }));
            pane.replaceChildren(h("h2", {}, "New backup codes"), backupCodesBox(r.backup_codes), h("button", { class: "btn primary", onclick: render }, "Done"));
          } }, "New backup codes"),
          s.required ? null : h("button", { class: "btn danger", onclick: async () => {
            const code = askCode("Turn off 2-step verification?"); if (!code) return;
            await act(() => API.post("/api/2fa/disable", { code }), "2-step verification turned off"); render();
          } }, "Turn off")));
    };
    render();
  },
});

function backupCodesBox(codes) {
  const text = codes.join("\n");
  return h("div", { class: "form" },
    h("div", { class: "note warn" }, "Save these backup codes somewhere safe (printed, or in a password manager). ",
      "Each one works once if you lose your phone. They won't be shown again."),
    h("pre", { class: "mono", style: "font-size:15px;background:var(--panel-2);padding:14px;border-radius:8px;columns:2;margin:0" }, text),
    h("div", { class: "toolbar" },
      h("button", { class: "btn", onclick: () => navigator.clipboard?.writeText(text).then(() => toast("Copied")) }, "Copy"),
      h("button", { class: "btn", onclick: () => {
        const a = h("a", { href: URL.createObjectURL(new Blob([`Cynthology backup codes for ${location.hostname}\n\n${text}\n`], { type: "text/plain" })), download: `cynthology-backup-codes-${location.hostname}.txt` });
        document.body.append(a); a.click(); a.remove();
      } }, "Download")));
}

// Pair an authenticator app: QR code -> confirm with a code -> show backup codes.
async function twoFactorSetup(container, onDone) {
  const s = await API.post("/api/2fa/setup");
  const code = h("input", { inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, placeholder: "123456", style: "font-size:20px;letter-spacing:4px;width:160px" });
  const err = h("div", { class: "err" });
  const confirm = async () => {
    err.textContent = "";
    try {
      const r = await API.post("/api/2fa/enable", { code: code.value });
      container.replaceChildren(h("h2", {}, "2-step verification is on"), backupCodesBox(r.backup_codes),
        h("label", { class: "check", style: "margin:10px 0" }, h("input", { type: "checkbox", id: "codes-saved" }), "I have saved my backup codes"),
        h("button", { class: "btn primary", onclick: () => {
          if (!document.getElementById("codes-saved").checked) return toast("Please save your backup codes first");
          onDone();
        } }, "Done"));
    } catch (e) { err.textContent = e.message; }
  };
  code.addEventListener("keydown", (e) => e.key === "Enter" && confirm());
  container.replaceChildren(h("h2", {}, "Turn on 2-step verification"),
    h("ol", { class: "wiz-list" },
      h("li", {}, "Install an authenticator app on your phone (Google or Microsoft Authenticator, Authy, 1Password, Bitwarden, Aegis…)."),
      h("li", {}, "In the app, add an account and scan this QR code:")),
    s.qr_svg ? h("div", { class: "qr", html: s.qr_svg }) : null,
    h("details", {}, h("summary", { class: "muted" }, "Can't scan? Enter this key manually"),
      h("div", { class: "mono", style: "font-size:15px;margin:8px 0;word-break:break-all" }, s.secret.replace(/(.{4})/g, "$1 ").trim()),
      h("div", { class: "muted" }, "Time-based, 6 digits, 30 seconds.")),
    h("ol", { start: 3, class: "wiz-list" }, h("li", {}, "Enter the 6-digit code the app shows:")),
    h("div", { class: "toolbar" }, code, h("button", { class: "btn primary", onclick: confirm }, "Verify")), err);
  code.focus();
}

// Administrators on a NAS that requires 2-step verification must set it up before anything else.
function requiredTwoFactor(me) {
  const card = h("div", { class: "wiz-card" });
  document.body.append(h("div", { class: "wiz" }, card));
  card.append(h("h2", {}, "Set up 2-step verification"),
    h("p", {}, `Hi ${me.user}. This NAS requires 2-step verification for administrators. It takes a minute.`),
    h("div", { class: "toolbar" },
      h("button", { class: "btn primary", onclick: () => twoFactorSetup(card, () => location.reload()) }, "Start"),
      h("button", { class: "btn", onclick: async () => { await API.post("/api/auth/logout"); sessionStorage.removeItem("cyn-token"); location.reload(); } }, "Sign out")));
}
