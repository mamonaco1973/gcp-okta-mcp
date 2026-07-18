# CLAUDE.md — gcp-okta-mcp

A GCP Cloud Asset Inventory API exposed as a **remote MCP connector** secured
with **Okta OIDC**. Claude connects directly to a remote `/mcp` endpoint over
HTTPS, the user logs in through your Okta org, and the tools run — **no local
proxy, no service account key file, nothing to configure but a URL.**

> This is the Okta sibling of `gcp-oauth-mcp`. Same GCP host (Cloud Functions
> 2nd Gen, Firestore, Secret Manager, public + in-code auth); the **only** thing
> that changes is the upstream IdP: **Okta instead of Google**. It is the
> "bring-your-own-IdP" entry in the set — the broker pattern works against any
> compliant OIDC provider, and Okta is the demonstration.

---

## Google build vs. this one

| | `gcp-oauth-mcp` | `gcp-okta-mcp` (this) |
|---|---|---|
| Upstream IdP | Google (a cloud IdP) | Okta (a vendor-neutral OIDC provider) |
| Token validation | Google `tokeninfo` **introspection** hop, pins `aud` | **Local JWT** validation against Okta's JWKS, pins `iss` + `aud` + `cid` |
| Bearer | Google access token (opaque) | Okta access token (**JWT** from the custom AS) |
| RFC 7591 DCR | Google doesn't implement it | **Okta does** — we still shim it (broker is the AS to Claude) |

The JWT-against-JWKS validation is the real upgrade: no network hop on every
call, and one Okta org means one issuer, so we pin the issuer cleanly (the
multitenant Azure build could not).

---

## What This Project Does

One Cloud Function (2nd Gen) is the entire stack. It serves the OAuth
authorization-server endpoints **and** the MCP JSON-RPC endpoint, and calls the
ten Cloud Asset Inventory tools in-process on `tools/call`. (Tool set unchanged
from the Google build — see that project's table.)

---

## Architecture

```
Claude (claude.ai / Claude Desktop) — remote MCP client
     │  1. probe:     POST /mcp with no token → 401 + WWW-Authenticate
     │  2. discover:  GET  /.well-known/oauth-authorization-server   (RFC 8414)
     │  3. register:  POST /oauth/register                           (RFC 7591)
     │  4. login:     GET  /authorize → <org>.okta.com/oauth2/default → /oauth/callback
     │  5. token:     POST /oauth/token   (gcp_ code → Okta access token JWT)
     │  6. use:       POST /mcp  (Authorization: Bearer <okta access token>)
     ▼
Cloud Function 2nd Gen — gcp-okta-mcp-func   [PUBLIC; the code enforces auth]
     ├── main.py    route table
     ├── oauth.py   OAuth broker  ── Firestore (transient pending-auth / codes)
     ├── mcp.py     JSON-RPC; validates Bearer JWT via Okta JWKS (iss+aud+cid)
     └── tools.py   10 CAI tools, called in-process
                    │  ADC → function SA
                    ▼
     Cloud Asset Inventory API        Cloud Storage API
```

### The two gaps the broker exists to close

1. **claude.ai's `redirect_uri` is dynamic** (embeds the org ID). Okta requires
   an exact allow-list match, so it can never be registered. We register only
   our own fixed `/oauth/callback` and carry Claude's URL through Firestore,
   keyed by the `state` we hand Okta.
2. **Claude has no `client_id` until something registers it.** Okta *does*
   implement RFC 7591 — but the broker is the authorization server **to Claude**,
   so we answer `/oauth/register` ourselves rather than expose Okta's endpoint
   and leak the upstream. The shim hands back our one shared client_id.

The token handed to Claude is a **genuine Okta access token**. We hold no
signing keys; validation uses Okta's public JWKS.

---

## Auth model — read this before changing anything

**The function is public** (`allUsers` has `roles/run.invoker`). Same inversion
as the Google build: the OAuth routes *are* the authentication and cannot
require a token, and Claude probes `/mcp` unauthenticated on purpose to read the
`WWW-Authenticate` pointer. Auth lives in `mcp.py`.

**Validation pins issuer, audience, and cid.** `mcp._resolve_user`:
- verifies the RS256 signature against Okta's JWKS (`{issuer}/v1/keys`),
- requires `iss == MCP_OKTA_ISSUER` and `aud == MCP_OKTA_AUDIENCE`,
- and requires `cid == MCP_OKTA_CLIENT_ID`.

Why `cid` too: `aud = api://default` is shared by every client hitting this
authorization server, so audience alone doesn't prove the token was minted for
*us*. The `cid` claim (the client that requested the token) is what pins it to
our own app — the equivalent of the Google build's `aud == client_id`.

**AuthN only, no authZ.** Every authenticated Okta user is authorized. To lock
it down, filter on `sub` (or a group/custom claim) in `_resolve_user`.

---

## The one manual step

Terraform does **not** manage Okta here (no Okta provider). Create the app once
in the Okta admin console:

1. **Applications → Create App Integration → OIDC – Web Application**, grant
   types Authorization Code + Refresh Token.
2. Use the **custom "default" authorization server** (Security → API):
   issuer `https://<org>.okta.com/oauth2/default`, audience `api://default`.
3. Export and apply:

```bash
export MCP_OKTA_CLIENT_ID="0oa..."
export MCP_OKTA_CLIENT_SECRET="..."
export MCP_OKTA_ISSUER="https://<org>.okta.com/oauth2/default"
export MCP_OKTA_AUDIENCE="api://default"   # optional; this is the default
./apply.sh
```

`check_env.sh` hard-fails if any of the first three are missing and also
confirms the issuer resolves. `apply.sh` prints the sign-in redirect URI to add
to the Okta app when it finishes. **Developer step, done once — not a user step.**

---

## Environment variables (function)

| Var | Source | Used by |
|-----|--------|---------|
| `GOOGLE_CLOUD_PROJECT` | `local.project_id` | tools.py |
| `MCP_OKTA_CLIENT_ID` | `var.okta_client_id` | oauth.py + mcp.py (`cid` check) |
| `MCP_OKTA_ISSUER` | `var.okta_issuer` | oauth.py (login URLs) + mcp.py (JWKS + iss) |
| `MCP_OKTA_AUDIENCE` | `var.okta_audience` | mcp.py (`aud` check) |
| `MCP_OKTA_CLIENT_SECRET` | Secret Manager | oauth.py |

The secret is mounted via `secret_environment_variables`, not a plain env var —
`list_cloud_functions_detail` prints function env vars, so a plaintext secret
would be readable through the very tools it protects.

---

## Gotchas that have bitten

- **Use the custom AS, not the org AS.** Only `.../oauth2/default` (or another
  custom AS) issues **JWT** access tokens with `aud = api://default`. The org
  authorization server (`https://<org>.okta.com`, no `/oauth2/...`) issues opaque
  tokens that don't validate against JWKS. `variables.tf` enforces the shape.
- **`offline_access` is what yields the refresh token** — it replaces Google's
  `access_type=offline` + `prompt=consent`. Without it the connector dies when
  the access token expires.
- **Okta rotates refresh tokens by default.** `_refresh` echoes back whichever
  token the response carries (new if rotated, else the client's) so Claude never
  drops the only valid copy.
- **Don't randomise the function name.** The URL derives from it, and that URL is
  the sign-in redirect URI registered on the Okta app.
- **Don't put an authorizer in front of `/mcp`.** The flow breaks before a token
  exists.
- The `.drawio` / `.png` diagrams and `00-resources/` are inherited from
  `gcp-oauth-mcp` and still depict the Google design — regenerate before reusing.

## Code Commenting Standards

See the workspace-root `.claude/CLAUDE.md`: comment the *why*, not the *what*;
`# ===` section headers; inline comments only for non-obvious intent.
