// Sign-in page for the Nextcloud desktop / phone apps (backend: ncsync.py). Sign in as usual (password,
// 2-step code or passkey), then allow the app: it gets an app password of its own.
(async () => {
  const card = document.getElementById("nc-card");
  const v2 = location.pathname.match(/\/login\/v2\/flow\/([^/]+)$/);
  const flow = v2 ? v2[1] : "";
  let app = t("A Nextcloud app"), info = null;
  if (flow) {
    try {
      const r = await fetch(`/index.php/login/v2/info/${encodeURIComponent(flow)}`);
      if (!r.ok) throw new Error();
      info = await r.json(); app = info.app || app;
    } catch { return card.replaceChildren(h("h2", {}, t("This sign-in link expired")), h("p", { class: "muted" }, t("Start again in the app."))); }
  }
  // where the request came from: anybody can start one and send you the link ("please reconnect your app")
  const origin = () => !info ? null : h("div", { class: info.same_device ? "muted" : "err", style: "font-size:13px;margin:6px 0" },
    `${t("Requested from")} ${info.from}` + (info.started ? `, ${new Date(info.started * 1000).toLocaleTimeString()}` : "") + ". ",
    info.same_device ? "" : t("That isn't this device. Only allow it if you started the sign-in in your Nextcloud app yourself, just now - otherwise someone else would get access to your files."));
  const logo = h("div", { class: "logo" }, h("span", { class: "stones" }, h("i"), h("i"), h("i")), " Cynthology");
  const err = h("div", { class: "err" });
  const user = h("input", { placeholder: t("Username"), autocomplete: "username", required: true });
  const pass = h("input", { type: "password", placeholder: t("Password"), autocomplete: "current-password", required: true });
  const code = h("input", { inputmode: "numeric", autocomplete: "one-time-code", placeholder: "123456", maxlength: 9 });
  let ticket = null;

  const signedIn = (r) => {
    API.token = r.token;
    card.replaceChildren(logo,
      h("h3", { style: "margin:4px 0" }, t("Connect this app?")),
      h("p", {}, h("b", { translate: "no" }, app), " ", t("wants to sync the files you can open on this NAS, as"), " ", h("b", { translate: "no" }, r.user), "."),
      origin(),
      h("p", { class: "muted", style: "font-size:13px" }, t("It gets a password of its own; you can disconnect it any time in Personal > Security.")),
      err,
      h("button", { class: "btn primary", onclick: async (e) => {
        e.target.disabled = true; err.textContent = "";
        try {
          const g = await API.post(`/api/ncsync/grant?flow=${encodeURIComponent(flow)}`);
          await API.post("/api/auth/logout").catch(() => {});      // (this page's own session isn't needed any more)
          if (g.redirect) { location.href = g.redirect; return; }
          card.replaceChildren(logo, h("h3", {}, "✓ " + t("Connected")), h("p", {}, t("Go back to the app - it continues by itself. You can close this window.")));
        } catch (x) { err.textContent = x.message; e.target.disabled = false; }
      } }, t("Allow")),
      h("button", { class: "login-alt", onclick: async () => {
        await API.post("/api/auth/logout").catch(() => {});
        card.replaceChildren(logo, h("p", {}, t("Not connected. You can close this window.")));
      } }, t("Cancel")));
  };

  const form = h("form", { style: "display:grid;gap:12px", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    try {
      if (!ticket) {
        const r = await API.post("/api/auth/login", { username: user.value, password: pass.value });
        if (r.need_otp) { ticket = r.ticket; user.parentNode.replaceChildren(h("div", {}, t("Enter the 6-digit code from your authenticator app.")), code); code.focus(); return; }
        signedIn(r);
      } else {
        signedIn(await API.post("/api/auth/otp", { ticket, code: code.value }));
      }
    } catch (x) { err.textContent = x.message; }
  } }, h("div", { style: "display:grid;gap:12px" }, user, pass), h("button", { type: "submit" }, t("Sign in")));
  card.replaceChildren(logo, h("p", { class: "muted" }, t("Sign in to connect"), " ", h("b", { translate: "no" }, app), "."), origin(), form, err);
  if (window.PublicKeyCredential && window.isSecureContext && !/^[0-9.]+$|:/.test(location.hostname)) {
    card.append(h("button", { type: "button", class: "login-alt", onclick: async () => {
      err.textContent = "";
      try { signedIn(await passkeySignIn(ticket)); } catch (x) { if (x.name !== "NotAllowedError") err.textContent = x.message; }
    } }, t("Sign in with a passkey")));
  }
  user.focus();
})();
