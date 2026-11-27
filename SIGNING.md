# Release signing

Every NAS checks that an update was signed with the Cynthology release key before installing
it. A tampered, unsigned, wrong-version or older download is refused and nothing is installed.
Only the person holding the secret key can make releases that NAS boxes accept.

How it fits together:

- `get.sh` holds the **public** key (`CYN_RELEASE_KEY`). It is installed on every NAS as
  `/usr/lib/cynthology/get.sh` and is what `cynthology update` and Control Panel → Update run.
- The **secret** key lives only in the GitHub secret `MINISIGN_SECRET_KEY` (and in your own
  offline backup). The release workflow signs `cynthology-install.tar.gz` and the ISO with it
  and uploads the `.minisig` files next to them.
- The workflow refuses to publish when `get.sh` has a public key but the secret is missing or
  doesn't match, so a release that the installed base would reject is never published.

## One-time setup (about 10 minutes)

1. On your own computer (not the NAS), install minisign: `sudo apt install minisign`
   (macOS: `brew install minisign`, Windows: from https://jedisct1.github.io/minisign/).
2. Create the key pair **without** a password (the GitHub secret store protects it):

   ```
   minisign -G -W -p cynthology-release.pub -s cynthology-release.key
   ```

3. GitHub → the repository → **Settings → Secrets and variables → Actions → New repository
   secret**. Name: `MINISIGN_SECRET_KEY`. Value: the whole content of
   `cynthology-release.key` (both lines).
4. Put the public key into `get.sh`: the **second line** of `cynthology-release.pub` (starts
   with `RW`) goes between the quotes of `CYN_RELEASE_KEY="..."`. Commit, or send the public
   key line to whoever maintains the repository - it is not secret.
5. Make the next release. It is signed, and from then on every NAS that installed it
   checks signatures on all later updates.
6. Keep `cynthology-release.key` somewhere safe and offline (password manager, USB stick in a
   drawer). If it is lost, make a new pair and repeat steps 3-5 (NAS boxes pick up the new
   public key with the next release, which must still be signed with the old key - so don't
   lose it). If it leaks, do the same at once.

## Checking a download by hand

```
minisign -V -P RW...your-public-key... -m cynthology-install.tar.gz
```

To go back to an older version on purpose: `sudo CYN_ALLOW_DOWNGRADE=1 cynthology update v0.30.0-alpha`.
