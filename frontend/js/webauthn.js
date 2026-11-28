// WebAuthn (passkeys) helpers - used by the desktop sign-in, Personal settings and the Nextcloud app sign-in page.
const b64u = {
  dec: (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0)).buffer,
  enc: (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
};
function pkOptions(o, kind) {
  const pk = { ...o.publicKey, challenge: b64u.dec(o.publicKey.challenge) };
  if (kind === "create") {
    pk.user = { ...pk.user, id: b64u.dec(pk.user.id) };
    pk.excludeCredentials = (pk.excludeCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));
  } else {
    pk.allowCredentials = (pk.allowCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));
  }
  return { publicKey: pk };
}
function pkJSON(c) {
  if (typeof c.toJSON === "function") { try { return c.toJSON(); } catch { /* older browsers: below */ } }
  const r = c.response, out = { id: c.id, rawId: b64u.enc(c.rawId), type: c.type, clientExtensionResults: c.getClientExtensionResults?.() || {}, response: {} };
  for (const k of ["clientDataJSON", "attestationObject", "authenticatorData", "signature", "userHandle"]) if (r[k]) out.response[k] = b64u.enc(r[k]);
  if (c.authenticatorAttachment) out.authenticatorAttachment = c.authenticatorAttachment;
  return out;
}
async function passkeySignIn(ticket) {
  const b = await API.post("/api/auth/passkey/begin", { ticket: ticket || "" });
  const cred = await navigator.credentials.get(pkOptions(b.options, "get"));
  return API.post("/api/auth/passkey/complete", { token: b.token, credential: pkJSON(cred) });
}
async function passkeyAdd(name) {
  const b = await API.post("/api/passkeys/register/begin");
  const cred = await navigator.credentials.create(pkOptions(b.options, "create"));
  return API.post("/api/passkeys/register/complete", { token: b.token, credential: pkJSON(cred), name });
}
