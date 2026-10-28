# Remote MCP Server on GCP: Cloud Functions with Okta Login

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

| Piece | Where | Role |
|---|---|---|
| **gcp-okta-mcp-func** | Cloud Functions, `us-central1` | 2nd gen, Python 3.11, 256 MB, up to 10 instances; public (`allUsers` invoker); `oauth.py` (the broker), `mcp.py` (JSON-RPC and JWT checks) and `tools.py`; the name is fixed so its redirect URI stays the same |
| **gcp-okta-mcp-sa** | IAM | the function's identity: `cloudasset.viewer`, `storage.objectViewer` and `datastore.user`, and access to the one secret |
| **Firestore** | `(default)`, native mode | in-flight logins and authorization codes, each with a TTL policy on `expires_at` |
| **gcp-okta-mcp-client-secret** | Secret Manager | your Okta client secret |
| **Okta OIDC app** | your Okta org | a **Web Application** with Authorization Code and Refresh Token grants, using the custom `default` authorization server; created by hand |

## Architecture

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="architecture-dark.svg">
  <img alt="Claude reaches gcp-okta-mcp-func, a public second-generation Cloud Function that is both an OAuth authorization server and the MCP server. Claude probes /mcp, discovers the authorization server, registers itself, and sends the user's browser through /authorize to your Okta org's custom authorization server; the function exchanges the code for tokens and returns the Okta access token, a JWT, to Claude. Login state lives in Firestore with TTL policies, and the Okta client secret comes from Secret Manager. MCP calls on /mcp carry the access token, which the function validates locally against Okta's JWKS, pinning the issuer, audience and client ID, and the ten tools query Cloud Asset Inventory and Cloud Storage as the gcp-okta-mcp-sa service account." src="architecture-light.svg">
</picture>

Regenerate with `python3 make_diagram.py`.

The function plays two roles at once: to Claude it *is* the OAuth
authorization server, and to Okta it is an ordinary OIDC *client*.
Connecting goes like this:

1. **Probe:** `POST /mcp` with no token returns `401` and a
   `WWW-Authenticate` header pointing at the metadata.
2. **Discover:** `GET /.well-known/oauth-authorization-server` (RFC 8414).
3. **Register:** `POST /oauth/register` (RFC 7591) gives Claude a client ID.
4. **Log in:** `GET /authorize` sends the browser to your Okta org, which
   returns to `/oauth/callback`.
5. **Token:** `POST /oauth/token` returns the Okta access token (a JWT).
6. **Use:** `POST /mcp` with `Authorization: Bearer <access token>`.

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

## The tools

Unchanged from the Google build — ten Cloud Asset Inventory tools
(`list_compute_instances`, `list_storage_buckets`, `count_resources_by_type`,
`find_resources_by_label`, `list_static_ip_addresses`, `find_resources_by_type`,
`find_resources_by_region`, `describe_resource`, `list_cloud_functions_detail`,
`list_bucket_objects`). Responses are pre-formatted plain text.

## Prerequisites

- `gcloud`, `terraform`, `jq`, `curl` in PATH
- `credentials.json` (GCP service account key) in the repo root, with the same
  roles the Google build needs (Cloud Functions/Run/Build, Artifact Registry,
  IAM, Cloud Asset Viewer, Storage, Secret Manager, Datastore Owner)
- **An Okta org**, and an OIDC app + custom authorization server (below)

If this is your first time following along, we recommend starting with this
video: [GCP + Terraform: Easy Setup](https://youtu.be/3spJpYX4f7I).

## Deploy

```bash
git clone https://github.com/mamonaco1973/gcp-okta-mcp.git
cd gcp-okta-mcp
```

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

`apply.sh` ends by running `validate.sh`, which checks the OAuth handshake
and that the tools are protected, then prints the connector URL.

## Try it

1. In Claude, open **Settings > Connectors > Add custom connector** and
   paste the `/mcp` URL `apply.sh` printed.
2. Claude discovers the authorization server, registers itself, and sends
   you to Okta to sign in.
3. Ask, for example, "What's running in us-central1?" or "Count my
   resources by type."

## Teardown

```bash
./destroy.sh
```

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
