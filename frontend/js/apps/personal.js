// Personal settings of the signed-in user: account (name, email, password), security (2-step
// verification, signed-in devices, recent sign-ins) and appearance.
WM.register("personal", {
  name: "Personal", icon: "👤", color: "#5b6b7d", w: 760, h: 620,
  open(body) {
    sectioned({
      account: { label: "Account", render: personalAccount },
      security: { label: "Security", render: personalSecurity },
      storage: { label: "Storage", render: personalStorage },
      appearance: { label: "Appearance", render: personalAppearance },
    }, body);
  },
});

async function personalAccount(p) {
  const a = await API.get("/api/account");
  const full = h("input", { value: a.full_name, maxlength: 100, disabled: !a.local });
  const email = h("input", { type: "email", value: a.email, placeholder: "you@example.com", maxlength: 254 });
  const alerts = h("input", { type: "checkbox", checked: a.alerts });
  p.append(h("h2", {}, "Account"),
    h("div", { class: "form" },
      h("label", {}, "User name", h("input", { value: a.user, disabled: true, translate: "no" })),
      h("label", {}, "Full name", full),
      h("label", {}, "Email address", email,
        h("span", { class: "muted", style: "font-size:12px" }, a.admin ? "Used to tell you when your password changes - and, if you like, for the NAS's alerts."
          : "Used to tell you when your password changes.")),
      a.admin ? h("label", { class: "check" }, alerts, "Email me the NAS's alerts (drive problems, failed backups, updates…)") : null,
      a.admin && !a.mail_ready ? h("div", { class: "note warn" }, "Emails need an email server: set it up in Control Panel → Notifications.") : null,
      h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: async () => {
        const body = { email: email.value.trim() };
        if (a.local) body.full_name = full.value;
        if (a.admin) body.alerts = alerts.checked;
        await act(() => API.put("/api/account", body), "Saved");
      } }, "Save"))));
  if (!a.local) {
    p.append(h("p", { class: "muted" }, "This is a domain account: its name and password are managed by your domain."));
    return;
  }
  const cur = h("input", { type: "password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", autocomplete: "new-password", minlength: 8 });
  const nw2 = h("input", { type: "password", autocomplete: "new-password" });
  const err = h("div", { class: "err" });
  p.append(h("h3", { class: "pc-group", style: "margin-top:22px" }, "Change password"),
    a.password_changed ? h("p", { class: "muted" }, `${t("Last changed")}: ${new Date(a.password_changed * 1000).toLocaleDateString()}`) : null,
    h("div", { class: "form" },
      h("label", {}, "Current password", cur),
      h("label", {}, "New password", nw, h("span", { class: "muted", style: "font-size:12px" }, "At least 8 characters. A few unrelated words make a strong, memorable password.")),
      h("label", {}, "New password again", nw2), err,
      h("div", { class: "toolbar" }, h("button", { class: "btn primary", onclick: async (e) => {
        err.textContent = "";
        if (nw.value !== nw2.value) { err.textContent = t("The new passwords don't match."); return; }
        if (nw.value.length < 8) { err.textContent = t("The new password needs at least 8 characters."); return; }
        e.target.disabled = true;
        try {
          const r = await API.post("/api/account/password", { current: cur.value, new: nw.value });
          API.token = r.token;
          try { sessionStorage.setItem("cyn-token", r.token); } catch {}
          cur.value = nw.value = nw2.value = "";
          toast(t("Password changed. Your other devices were signed out."));
        } catch (x) { err.textContent = x.message; }
        e.target.disabled = false;
      } }, "Change password")),
      h("p", { class: "muted", style: "font-size:12px" }, "Your new password works everywhere: this web interface, shared folders (SMB), WebDAV and SFTP.")));
}

async function personalStorage(p) {
  p.append(h("h2", {}, "Storage"), h("p", { class: "muted" }, "The shared folders you can open, with their size limit and how much they hold."));
  const rows = await API.get("/api/account/storage");
  const size = (b) => (b == null ? "-" : fmtBytes(b));
  p.append(rows.length ? h("table", {},
    h("tr", {}, ...["Shared folder", "Size limit", "Used", "Free on its pool"].map((x) => h("th", { style: "text-align:left" }, x))),
    rows.map((r) => {
      const pct = r.limit && r.used != null ? Math.min(100, Math.round(100 * r.used / r.limit)) : null;
      return h("tr", {},
        h("td", { translate: r.kind === "home" ? null : "no" }, r.kind === "home" ? h("b", {}, t("Your home folder")) : r.name),
        h("td", {}, r.limit ? fmtBytes(r.limit) : t("No limit")),
        h("td", {}, size(r.used), pct != null ? h("div", { class: "bar", style: "width:120px;margin-top:4px" },
          h("div", { style: `width:${pct}%;background:${pct > 90 ? "var(--bad)" : pct > 75 ? "var(--warn)" : "var(--accent)"}` })) : null),
        h("td", {}, size(r.free)));
    })) : h("p", { class: "muted" }, "You don't have access to any shared folders yet."),
    h("p", { class: "muted", style: "font-size:12px;margin-top:10px" }, "Usage is shown where size limits (quotas) are turned on for the folder."));
}

function describeAgent(ua) {
  if (!ua) return t("Unknown device");
  const b = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "";
  const os = /iPhone|iPad/.test(ua) ? "iOS" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  return [b, os].filter(Boolean).join(" · ") || ua.slice(0, 60);
}

const fmtWhen = (sec) => sec ? new Date(sec * 1000).toLocaleString() : "-";

async function personalSecurity(p) {
  const [s, a] = await Promise.all([API.get("/api/2fa"), API.get("/api/account")]);
  p.append(h("h2", {}, "Security"), h("h3", { class: "pc-group" }, "2-step verification"));
  const tf = h("div");
  p.append(tf);
  const render2fa = async () => {
    const st = await API.get("/api/2fa");
    if (!st.enabled) {
      tf.replaceChildren(h("p", {}, "Protect your account with a code from an authenticator app on your phone, in addition to your password."),
        st.required ? h("div", { class: "note warn" }, "Required for administrators on this NAS.") : null,
        h("button", { class: "btn primary", onclick: () => twoFactorSetup(tf, render2fa) }, "Turn on 2-step verification"));
      return;
    }
    const askCode = (why) => prompt(`${why}\n\nEnter a current code from your authenticator app (or a backup code):`);
    tf.replaceChildren(
      h("div", { class: "note" }, "✓ 2-step verification is on. Signing in needs your password and a code from your authenticator app."),
      h("p", { class: "muted" }, `${st.backup_codes_left} unused backup code(s) left.`),
      h("div", { class: "toolbar" },
        h("button", { class: "btn", onclick: async () => {
          const code = askCode("Create a new set of backup codes (the old ones stop working)."); if (!code) return;
          const r = await act(() => API.post("/api/2fa/backup-codes", { code }));
          tf.replaceChildren(h("h3", {}, "New backup codes"), backupCodesBox(r.backup_codes), h("button", { class: "btn primary", onclick: render2fa }, "Done"));
        } }, "New backup codes"),
        st.required ? null : h("button", { class: "btn danger", onclick: async () => {
          const code = askCode("Turn off 2-step verification?"); if (!code) return;
          await act(() => API.post("/api/2fa/disable", { code }), "2-step verification turned off"); render2fa();
        } }, "Turn off")));
  };
  void s; render2fa();

  // passkeys / security keys
  const pkBox = h("div");
  const renderPasskeys = async () => {
    const pk = await API.get("/api/passkeys");
    const here = pk.keys.filter((k) => k.rp_id === pk.host);
    pkBox.replaceChildren(
      h("p", { class: "muted" }, "Sign in with Face ID, Touch ID, Windows Hello, your phone or a security key (YubiKey…) instead of a password. ",
        "It also replaces the 6-digit code."),
      pk.keys.length ? h("table", { class: "kv" }, pk.keys.map((k) => h("tr", {},
        h("td", {}, h("b", {}, k.name), h("div", { class: "muted", style: "font-size:12px", translate: "no" },
          `${k.rp_id} · ${t("added")} ${fmtWhen(k.created)}` + (k.last_used ? ` · ${t("last used")} ${fmtWhen(k.last_used)}` : ""))),
        h("td", { style: "text-align:right;white-space:nowrap" }, h("button", { class: "btn danger", onclick: async () => {
          if (!confirm(t("Remove this passkey? You can't sign in with it afterwards."))) return;
          await act(() => API.del(`/api/passkeys/${k.id}`), "Removed"); renderPasskeys();
        } }, "Remove"))))) : null,
      pk.usable_here && window.PublicKeyCredential ? h("div", { class: "toolbar", style: "margin-top:8px" }, h("button", { class: "btn primary", onclick: async () => {
        const name = prompt(t("A name for this passkey (e.g. iPhone, YubiKey):"), "");
        if (name === null) return;
        try { await passkeyAdd(name); toast(t("Passkey added")); renderPasskeys(); }
        catch (e) { if (e.name !== "NotAllowedError") toast(e.message); }
      } }, "Add a passkey"))
        : h("div", { class: "note warn" }, t("Passkeys work when the NAS is opened by its name over HTTPS with a trusted certificate (e.g. your domain from Control Panel > Login Portal) - not by an IP address. You're on"), ` ${pk.host}.`),
      here.length || !pk.keys.length ? null : h("p", { class: "muted", style: "font-size:12px" }, t("Your passkeys belong to other addresses: they work when you open the NAS there.")));
  };
  p.append(h("h3", { class: "pc-group", style: "margin-top:22px" }, "Passkeys and security keys"), pkBox);
  renderPasskeys();

  const sess = h("table", { class: "kv" });
  const renderSessions = async () => {
    const x = await API.get("/api/account");
    sess.replaceChildren(...x.sessions.map((v) => h("tr", {},
      h("td", {}, h("b", {}, v.created ? describeAgent(v.agent) : t("Earlier sign-in (no details recorded)")), v.current ? h("span", { class: "pill ok", style: "margin-left:8px" }, t("this device")) : null,
        v.created ? h("div", { class: "muted", style: "font-size:12px", translate: "no" }, `${v.ip || "?"} · ${t("signed in")} ${fmtWhen(v.created)} · ${t("last active")} ${fmtWhen(v.seen)}`) : null),
      h("td", { style: "text-align:right;white-space:nowrap" }, v.current ? null : h("button", { class: "btn", onclick: async () => {
        await act(() => API.del(`/api/account/sessions/${v.id}`), "Signed out"); renderSessions();
      } }, "Sign out")))));
  };
  p.append(h("h3", { class: "pc-group", style: "margin-top:22px" }, "Signed-in devices"),
    h("p", { class: "muted" }, "Browsers where you're signed in right now. Don't recognise one? Sign it out and change your password."),
    sess, h("div", { class: "toolbar", style: "margin-top:8px" }, h("button", { class: "btn", onclick: async () => {
      const r = await act(() => API.post("/api/account/sessions/sign-out-others"));
      toast(`${t("Signed out")}: ${r.ended}`); renderSessions();
    } }, "Sign out everywhere else")));
  renderSessions();

  const label = { login: "Signed in", failed: "Failed sign-in", "2fa-failed": "Wrong verification code", locked: "Blocked: too many attempts",
                  blocked: "Blocked", logout: "Signed out", password: "Password changed", "passkey-added": "Passkey added", "passkey-removed": "Passkey removed" };
  p.append(h("h3", { class: "pc-group", style: "margin-top:22px" }, "Recent sign-ins"),
    a.history.length ? h("table", { class: "kv" }, a.history.map((e) => h("tr", {},
      h("td", { style: "width:190px" }, fmtWhen(e.time)),
      h("td", {}, h("span", { class: "pill " + (e.event === "login" || e.event === "password" || e.event === "passkey-added" ? "ok" : e.event === "logout" ? "" : "warn") }, t(label[e.event] || e.event)),
        h("span", { class: "muted", translate: "no" }, `  ${e.ip}`))))) : h("p", { class: "muted" }, "Nothing yet."));
}

// ---------- desktop preferences (stored on the NAS: they follow the user to every browser) ----------
const WALLPAPERS = {
  default: null,
  ocean: "linear-gradient(135deg, #0f2f4a, #1b6f8f)",
  dusk: "linear-gradient(135deg, #2b1840, #8a3d5c 60%, #d0844f)",
  forest: "linear-gradient(135deg, #0f2a22, #2d6a4f)",
  graphite: "linear-gradient(135deg, #15181d, #3a414c)",
  aurora: "radial-gradient(ellipse at 20% 10%, #2dd4bf55, transparent 55%), radial-gradient(ellipse at 80% 30%, #7c3aed66, transparent 50%), linear-gradient(160deg, #0b1220, #12253a)",
};
const PREFS = { date: "auto", time: "auto", icons: "normal", wall: "default", resume: false };
let _wallUrl = null;

async function applyPrefs(prefs) {
  Object.assign(PREFS, prefs || {});
  document.body.classList.remove("icons-small", "icons-large");
  if (PREFS.icons !== "normal") document.body.classList.add("icons-" + PREFS.icons);
  let bg = WALLPAPERS[PREFS.wall] || "";
  if (PREFS.wall === "custom") {
    try {
      const r = await fetch("/api/account/wallpaper", { headers: { Authorization: `Bearer ${API.token}` } });
      if (r.ok) { if (_wallUrl) URL.revokeObjectURL(_wallUrl); _wallUrl = URL.createObjectURL(await r.blob()); bg = `center / cover no-repeat url("${_wallUrl}")`; }
    } catch {}
  }
  document.body.style.background = bg;
}

// Date & time format: every date the interface shows goes through these three functions.
(() => {
  const D = Date.prototype, orig = { s: D.toLocaleString, d: D.toLocaleDateString, t: D.toLocaleTimeString };
  const loc = (l) => ({ ymd: "sv-SE", dmy: "en-GB", mdy: "en-US" }[PREFS.date] || l);
  const opt = (o) => (PREFS.time === "auto" || (o && "hour12" in o) ? o : { ...(o || {}), hour12: PREFS.time === "12" });
  D.toLocaleString = function (l, o) { return orig.s.call(this, loc(l), opt(o)); };
  D.toLocaleDateString = function (l, o) { return orig.d.call(this, loc(l), o); };
  D.toLocaleTimeString = function (l, o) { return orig.t.call(this, l, opt(o)); };
})();

// "Reopen my windows": the open apps are remembered (in this browser) and reopened at sign-in.
function rememberWindows() {
  if (!PREFS.resume) return;
  try { localStorage.setItem("cyn-open-wins", JSON.stringify(Object.keys(WM.wins))); } catch {}
}
function reopenWindows() {
  if (!PREFS.resume) return;
  let ids = [];
  try { ids = JSON.parse(localStorage.getItem("cyn-open-wins") || "[]"); } catch {}
  ids.filter((id) => WM.apps[id]).forEach((id) => WM.open(id));
}

function personalAppearance(p) {
  let theme = "dark", lang = "auto";
  try { theme = localStorage.getItem("cyn-theme") || "dark"; lang = localStorage.getItem("cyn-lang") || "auto"; } catch {}
  p.append(h("h2", {}, "Appearance"),
    h("div", { class: "form" },
      h("label", {}, "Theme", h("select", { onchange: (e) => {
        try { localStorage.setItem("cyn-theme", e.target.value); } catch {}
        const v = e.target.value === "auto" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : e.target.value;
        document.documentElement.dataset.theme = v;
      } }, [["dark", "Dark"], ["light", "Light"], ["auto", "Same as this computer"]].map(([v, l]) => h("option", { value: v, selected: theme === v }, l)))),
      h("label", {}, "Language", h("select", { onchange: (e) => {
        try { localStorage.setItem("cyn-lang", e.target.value); } catch {}
        location.reload();
      } }, [["auto", "Same as this computer"], ...Object.entries(LANGS)].map(([v, l]) =>
        h("option", { value: v, selected: lang === v, translate: v === "auto" ? null : "no" }, l)))),
      h("p", { class: "muted", style: "font-size:12px" }, "Theme and language are saved in this browser.")));
  personalDesktop(p);
}

async function personalDesktop(p) {
  const a = await API.get("/api/account");
  Object.assign(PREFS, a.prefs);
  const save = async (k, v) => {
    await act(() => API.put("/api/account", { prefs: { [k]: v } }));
    PREFS[k] = v; applyPrefs(PREFS); render();
  };
  const sel = (k, opts) => h("select", { onchange: (e) => save(k, e.target.value) }, opts.map(([v, l]) => h("option", { value: v, selected: PREFS[k] === v }, l)));
  const now = new Date();
  const sample = (d) => now.toLocaleDateString({ ymd: "sv-SE", dmy: "en-GB", mdy: "en-US" }[d]);
  const box = h("div");
  p.append(box);
  const fileIn = h("input", { type: "file", accept: "image/*", style: "display:none", onchange: async () => {
    const f = fileIn.files[0]; if (!f) return;
    try {
      const blob = await shrinkImage(f, 2560, 1600);
      await API.raw("PUT", "/api/account/wallpaper", blob);
      await save("wall", "custom");
    } catch (e) { toast(e.message); }
  } });
  const render = () => box.replaceChildren(
    h("h3", { class: "pc-group", style: "margin-top:22px" }, "Date & time"),
    h("div", { class: "form" },
      h("label", {}, "Date format", sel("date", [["auto", t("Same as the language")], ["ymd", `${t("Year-month-day")} (${sample("ymd")})`],
        ["dmy", `${t("Day/month/year")} (${sample("dmy")})`], ["mdy", `${t("Month/day/year")} (${sample("mdy")})`]])),
      h("label", {}, "Time format", sel("time", [["auto", t("Same as the language")], ["24", t("24-hour (17:30)")], ["12", t("12-hour (5:30 PM)")]]))),
    h("h3", { class: "pc-group", style: "margin-top:22px" }, "Desktop"),
    h("div", { class: "form" },
      h("label", {}, "Desktop icon size", sel("icons", [["small", t("Small")], ["normal", t("Normal")], ["large", t("Large")]])),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: PREFS.resume, onchange: (e) => {
        save("resume", e.target.checked); if (e.target.checked) { PREFS.resume = true; rememberWindows(); }
      } }), "Reopen the windows I had open when I sign in again")),
    h("div", { style: "font-size:13px;color:var(--muted);margin:14px 0 6px" }, t("Background")),
    h("div", { class: "wall-grid" },
      ...Object.entries(WALLPAPERS).map(([k, bg]) => h("button", { class: "wall" + (PREFS.wall === k ? " on" : ""), title: t(k[0].toUpperCase() + k.slice(1)),
        style: `background:${bg || "linear-gradient(135deg, var(--bg1), var(--bg2))"}`, onclick: () => save("wall", k) })),
      h("button", { class: "wall add" + (PREFS.wall === "custom" ? " on" : ""), title: t("Your own picture"),
        style: PREFS.wall === "custom" && _wallUrl ? `background:center/cover url("${_wallUrl}")` : "", onclick: () => fileIn.click() },
        PREFS.wall === "custom" ? "" : "+")),
    h("div", { class: "muted", style: "font-size:12px;margin-top:6px" }, t("Click + to use your own picture (it's scaled down and kept on the NAS).")),
    fileIn);
  render();
}

// Scale a picture down in the browser (the NAS never has to decode it) and return a JPEG.
function shrinkImage(file, maxW, maxH) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, maxW / img.width, maxH / img.height);
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(img.src);
      c.toBlob((b) => (b ? resolve(b) : reject(new Error(t("Couldn't read that picture")))), "image/jpeg", 0.86);
    };
    img.onerror = () => reject(new Error(t("Couldn't read that picture")));
    img.src = URL.createObjectURL(file);
  });
}

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
