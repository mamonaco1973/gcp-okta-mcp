# GCP Okta MCP — a remote MCP server on Cloud Functions, secured with Okta OIDC

Connect Claude directly to your GCP resource inventory. No local proxy. No
service account key file on your laptop. You paste a URL, you log in through your
Okta org, and the tools work.

This is the **Okta sibling of [`gcp-oauth-mcp`](https://github.com/mamonaco1973/gcp-oauth-mcp)**. Same GCP
host — one public Cloud Function, Firestore, Secret Manager, auth enforced in
code. The one thing that changes is the upstream identity provider: **Okta
instead of Google.** It's the bring-your-own-IdP entry in the set — the broker
pattern works against any compliant OIDC provider, and Okta is the demonstration.

| | `gcp-oauth-mcp` | `gcp-okta-mcp` (this) |
|---|---|---|
| Upstream IdP | Google (a cloud IdP) | Okta (a vendor-neutral OIDC provider) |
| Token validation | `tokeninfo` introspection hop, pins `aud` | **Local JWT** vs Okta JWKS, pins `iss` + `aud` + `cid` |
| Bearer | Google access token (opaque) | Okta access token (**JWT** from the custom AS) |
| RFC 7591 DCR | Google doesn't implement it | **Okta does** — we still shim it |

The local JWT validation is the real upgrade: no network round-trip on every
`/mcp` call, and one Okta org means one issuer, so we pin the issuer cleanly.

---

## Architecture

```
Claude (claude.ai / Claude Desktop)
     │  1. probe:     POST /mcp with no token → 401 + WWW-Authenticate
     │  2. discover:  GET  /.well-known/oauth-authorization-server   (RFC 8414)
     │  3. register:  POST /oauth/register                           (RFC 7591)
     │  4. login:     GET  /authorize → <org>.okta.com/oauth2/default → /oauth/callback
     │  5. token:     POST /oauth/token
     │  6. use:       POST /mcp  (Bearer <okta access token JWT>)
     ▼
Cloud Function 2nd Gen — one function, public, auth enforced in code
     ├── oauth.py   OAuth broker  ── Firestore (transient login state, 5-min TTL)
     ├── mcp.py     JSON-RPC; validates the Bearer JWT against Okta's JWKS
     └── tools.py   10 Cloud Asset Inventory tools, called in-process
                    ▼
     Cloud Asset Inventory API   ·   Cloud Storage API
```

The function plays **two roles at once**. To Claude it *is* the OAuth
authorization server. To Okta it is an ordinary OIDC *client*.

### Why a broker, and not just "point Claude at Okta"?

1. **claude.ai's redirect URI is dynamic** — it embeds the org ID. Okta requires
   an exact allow-list match, so it can never be registered. The broker registers
   only its own fixed `/oauth/callback` and carries Claude's URL through
   Firestore.
2. **Claude has no `client_id` until something registers it.** Okta *does*
   implement RFC 7591 — but the broker is the authorization server **to Claude**,
   so it answers `/oauth/register` itself rather than expose Okta's endpoint and
   leak the upstream.

That second point is the honest twist versus the AWS/GCP/Azure builds: the cloud
IdPs all skip DCR; the dedicated identity vendors like Okta implement it. The
gap is a product choice, not a technical impossibility.

---

## The tools

Unchanged from the Google build — ten Cloud Asset Inventory tools
(`list_compute_instances`, `list_storage_buckets`, `count_resources_by_type`,
`find_resources_by_label`, `list_static_ip_addresses`, `find_resources_by_type`,
`find_resources_by_region`, `describe_resource`, `list_cloud_functions_detail`,
`list_bucket_objects`). Responses are pre-formatted plain text.

---

## Prerequisites

- `gcloud`, `terraform`, `jq`, `curl` in PATH
- `credentials.json` (GCP service account key) in the repo root, with the same
  roles the Google build needs (Cloud Functions/Run/Build, Artifact Registry,
  IAM, Cloud Asset Viewer, Storage, Secret Manager, Datastore Owner)
- **An Okta org**, and an OIDC app + custom authorization server (below)

---

## Deploy

### Step 1 — create the Okta OIDC app (once, by hand)

Terraform does not manage Okta here. In the Okta admin console:

1. **Applications → Create App Integration → OIDC – Web Application.** Grant
   types: **Authorization Code + Refresh Token**.
2. Use the custom **"default" authorization server** (Security → API →
   Authorization Servers). Its issuer is `https://<org>.okta.com/oauth2/default`
   and its audience is `api://default`. This matters: only a *custom* AS issues
   **JWT** access tokens we can validate against JWKS — the org AS issues opaque
   tokens.
3. Copy the client ID and secret, then export:

```bash
export MCP_OKTA_CLIENT_ID="0oa..."
export MCP_OKTA_CLIENT_SECRET="..."
export MCP_OKTA_ISSUER="https://<org>.okta.com/oauth2/default"
export MCP_OKTA_AUDIENCE="api://default"   # optional; this is the default
```

`check_env.sh` hard-fails if the first three are missing and also confirms the
issuer resolves.

### Step 2 — apply

```bash
./apply.sh
```

When it finishes it prints the **sign-in redirect URI**. Add that to the Okta
app under *Sign-in redirect URIs*. The function name is **not randomised**, so
the URI is stable — you do this once, not after every rebuild.

### Step 3 — connect Claude

**Settings → Connectors → Add custom connector**, and paste the `/mcp` URL that
`apply.sh` printed. Claude discovers the authorization server, registers itself,
and sends you to Okta to log in. That is the entire configuration.

```bash
./validate.sh   # smoke-test the handshake and the auth boundary
./destroy.sh    # tear it down
```

---

## Security — what this does and does not do

**It authenticates. It does not authorize.** Every authenticated Okta user is
authorized; there is no allow-list. To lock it down, filter on `sub` (or a group
/ custom claim) in `mcp._resolve_user`.

Three things this build gets right:

**The token is validated locally, and pinned three ways.** `_resolve_user`
verifies the RS256 signature against Okta's JWKS and requires `iss`, `aud`, and
`cid` to match. `aud = api://default` is shared by every client on that
authorization server, so the `cid` (requesting client) pin is what makes the
token *ours*, not just any token for the API.

**The client secret lives in Secret Manager**, not a plain env var — because
`list_cloud_functions_detail` prints function environment variables, so a
plaintext secret would be readable through the very tools it protects.

**The function is public, and that is correct.** `allUsers` holds
`roles/run.invoker`. The OAuth endpoints can't require a token, and Claude probes
`/mcp` unauthenticated on purpose to read the `WWW-Authenticate` header. The door
opens, and `mcp.py` enforces the token instead.

---

## Gotchas

- **Use the custom AS, not the org AS.** Only `.../oauth2/default` (or another
  custom AS) issues JWT access tokens with `aud = api://default`. The org AS
  (`https://<org>.okta.com`) issues opaque tokens that won't validate against
  JWKS. `variables.tf` enforces the issuer shape.
- **`offline_access` yields the refresh token** — it's the Okta equivalent of
  Google's `access_type=offline` + `prompt=consent`. Without it the connector
  dies when the access token expires.
- **Okta rotates refresh tokens by default**, so the token endpoint returns a new
  one on refresh; `_refresh` echoes back whichever it gets.
- **`redirect_uri` mismatch at login** means the URI `apply.sh` printed isn't on
  the Okta app's sign-in redirect URIs. Most likely thing to go wrong.
- **Don't randomise the function name.** The URL derives from it, and that URL is
  the registered redirect URI.
