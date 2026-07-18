# GCP OAuth MCP — a remote MCP server on Cloud Functions, secured with Google OAuth

Connect Claude directly to your GCP resource inventory. No local proxy. No
service account key file on your laptop. You paste a URL, you log in with
Google, and the tools work.

This is the OAuth port of `gcp-serverless-mcp`, which kept the Cloud Function
private behind Cloud Run IAM and shipped a local proxy that signed OIDC
assertions with a **downloaded service account private key**. That key was the
single credential for all MCP access — no expiry, no rotation, no per-user
identity, sitting in plaintext in a folder.

This version deletes it.

| | Proxy build | This build |
|---|---|---|
| Client setup | Install proxy, edit JSON config, hold a key file | Paste one URL |
| Credential on disk | SA private key, never expires | None |
| Who is the caller? | Always the same service account | The actual human, via Google |
| Function exposure | Private (Cloud Run IAM) | Public; the code enforces auth |
| Auth enforced by | The platform | `mcp.py`, on every call |

---

## Architecture

```
Claude (claude.ai / Claude Desktop)
     │  1. probe:     POST /mcp with no token → 401 + WWW-Authenticate
     │  2. discover:  GET  /.well-known/oauth-authorization-server   (RFC 8414)
     │  3. register:  POST /oauth/register                           (RFC 7591)
     │  4. login:     GET  /authorize → accounts.google.com → /oauth/callback
     │  5. token:     POST /oauth/token
     │  6. use:       POST /mcp  (Bearer <google access token>)
     ▼
Cloud Function 2nd Gen — one function, public, auth enforced in code
     ├── oauth.py   OAuth broker  ── Firestore (transient login state, 5-min TTL)
     ├── mcp.py     JSON-RPC; validates the Bearer token against Google
     └── tools.py   10 Cloud Asset Inventory tools, called in-process
                    ▼
     Cloud Asset Inventory API   ·   Cloud Storage API
```

The function plays **two roles at once**. To Claude it *is* the OAuth
authorization server. To Google it is an ordinary OAuth *client*.

### Why a broker, and not just "point Claude at Google"?

Two gaps, neither of them ours to fix upstream:

1. **claude.ai's redirect URI is dynamic.** It embeds the org ID —
   `https://claude.ai/api/organizations/<id>/mcp/callback`. Google requires an
   exact allow-list match, so you can never register it. The broker registers
   only its own fixed `/oauth/callback` and carries Claude's URL through
   Firestore.

2. **Google has no dynamic client registration** (RFC 7591). Without a
   `/oauth/register` endpoint, Claude has no `client_id` — and the user ends up
   pasting a client ID and secret by hand.

Roughly 300 lines of `oauth.py` is what stands between "paste a URL" and "paste
a client ID, a client secret, and hope."

---

## The tools

| Tool | Operation |
|---|---|
| `list_compute_instances` | All VMs with machine type, zone, status |
| `list_storage_buckets` | All GCS buckets with location and storage class |
| `count_resources_by_type` | Ranked inventory summary |
| `find_resources_by_label` | Resources matching a label key+value |
| `list_static_ip_addresses` | All static external IPs |
| `find_resources_by_type` | Resources of a specific asset type |
| `find_resources_by_region` | Resources in a region or zone |
| `describe_resource` | Full config detail for a named resource |
| `list_cloud_functions_detail` | Runtime, memory, URL, service account, env vars |
| `list_bucket_objects` | Objects in a bucket with size and last-modified |

Responses are pre-formatted plain text, not JSON — Cloud Asset Inventory returns
deeply nested proto structs, and the model narrates a text table far better than
it parses one.

---

## Prerequisites

- `gcloud`, `terraform`, `jq`, `curl` in PATH
- `credentials.json` (GCP service account key) in the repo root
- That service account needs: Cloud Functions Admin, Cloud Run Admin, Cloud
  Build Editor, Artifact Registry Admin, IAM Admin, Cloud Asset Viewer, Storage
  Admin, Service Account Admin, Project IAM Admin, Secret Manager Admin,
  Datastore Owner

---

## Deploy

### Step 1 — create the Google OAuth client (once, by hand)

Terraform cannot do this for you. `google_iap_client` requires an IAP brand, and
external brands can only be created in the console. This is the one manual step.

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Web application**
3. Leave the redirect URI blank for now — the function does not exist yet.
4. Copy the client ID and secret.

```bash
export MCP_GOOGLE_CLIENT_ID="123456789-abc.apps.googleusercontent.com"
export MCP_GOOGLE_CLIENT_SECRET="GOCSPX-..."
```

`check_env.sh` hard-fails if either is missing — Google sign-in is not an
optional extra here, it *is* the authentication.

### Step 2 — apply

```bash
./apply.sh
```

When it finishes it prints the **authorized redirect URI**. Paste that back onto
the OAuth client in the console.

The function name is **not randomised**, so that URI is stable — you do this
once, not after every rebuild.

### Step 3 — publish the consent screen

Confirm the OAuth consent screen is **Published**, not "Testing". In Testing mode
refresh tokens expire after 7 days and only allow-listed users can sign in.

Publishing needs **no Google verification review**: `openid`, `email`, and
`profile` are all non-sensitive scopes. It is a button, not an audit.

### Step 4 — connect Claude

**Settings → Connectors → Add custom connector**, and paste the `/mcp` URL that
`apply.sh` printed.

That is the entire configuration. Claude discovers the authorization server,
registers itself, and sends you to Google to log in.

```bash
./validate.sh   # smoke-test the handshake and the auth boundary
./destroy.sh    # tear it down
```

---

## Security — what this does and does not do

**It authenticates. It does not authorize.**

Every authenticated Google user is authorized. There is no allow-list. Anyone
with a Google account who reaches the endpoint can read this project's resource
inventory. That is an acceptable trade in a demo and a bad one anywhere else —
to lock it down, filter on the `email` or `hd` claim in `mcp._resolve_user`.

Three things this build does get right, and they are worth understanding:

**The token's audience is pinned.** `_resolve_user` rejects any token whose `aud`
is not our own `client_id`. This is not optional paranoia: a naive `/userinfo`
check accepts a valid Google token minted for *any* application on the internet,
which would let an unrelated app's token call these tools.

**The client secret lives in Secret Manager**, not a plain environment variable —
because `list_cloud_functions_detail` prints function environment variables. A
plaintext secret would be readable through the very tools it protects.

**The function is public, and that is correct.** `allUsers` holds
`roles/run.invoker`. The OAuth endpoints cannot require a token — obtaining one
is their job — and Claude probes `/mcp` unauthenticated on purpose to read the
`WWW-Authenticate` header. An IAM check would break the handshake before any code
ran. So the door opens, and `mcp.py` enforces the token instead.

---

## Gotchas

- **Google access tokens last one hour**, and that is not configurable. The
  `refresh_token` grant is mandatory here (the Cognito equivalent could skip it —
  Cognito allows a 24-hour access token).
- **`prompt=consent` in `/authorize` is load-bearing.** Without it Google omits the
  refresh token for a user who already granted access, and the connector dies
  silently after an hour.
- **`redirect_uri_mismatch` at login** means the URI `apply.sh` printed is not on
  the OAuth client. It is the single most likely thing to go wrong.
- **Don't randomise the function name.** The URL derives from it, and that URL is
  the registered redirect URI.
