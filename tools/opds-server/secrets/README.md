# secrets/

The OPDS server's Basic-auth password lives here and is **gitignored**. Nothing
in this directory is committed except this README.

Authentication is **optional** — the server runs open on your LAN by default,
which is usually what you want at home. Set it up when the network is shared:

1. Pick a username and put it in `config.json`:

   ```json
   { "auth": { "username": "reader" } }
   ```

2. Save the password to `opds.password` in this directory — the password only,
   no quotes, no newline noise:

   ```
   printf '%s' 'your-password-here' > opds.password
   ```

Both are required. The device sends credentials only when it has a username
*and* a password, so half a pair means no authentication at all — which is why
the server ignores an incomplete one rather than half-enforcing it.

Alternatively, skip the file and export `X3_OPDS_PASSWORD` in your shell; the
server falls back to that environment variable.

To make the server *refuse to start* without credentials — worth it if an open
library would be a real problem — set `"require_auth": true` in `config.json`.

One thing to be clear-eyed about: Basic auth over plain HTTP puts the password
on the wire in the clear, and HTTPS is not an option here. CrossPoint verifies
certificates against a bundled CA store with insecure mode compiled out, so a
self-signed certificate cannot complete a handshake at all. This is a
home-network tool; the password keeps the library private, not secret.
