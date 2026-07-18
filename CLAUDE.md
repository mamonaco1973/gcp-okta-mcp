# CLAUDE.md — gcp-oauth-mcp

A GCP Cloud Asset Inventory API exposed as a **remote MCP connector** secured
with **Google OAuth 2.0**. Claude connects directly to a remote `/mcp` endpoint
over HTTPS, the user logs in with their Google account, and the tools run —
**no local proxy, no service account key file, nothing to configure but a URL.**

> This is the OAuth port of `gcp-serverless-mcp`, which kept the function private
> behind Cloud Run IAM and shipped a local proxy that signed OIDC assertions with
> a downloaded SA key. The OAuth broker pattern is adapted from `aws-cognito-mcp`
> (`01-lambdas/code/oauth.py` + `mcp.py`), with Google in place of Cognito.

---

## What This Project Does

One Cloud Function (2nd Gen) is the entire stack. It serves the OAuth
authorization-server endpoints **and** the MCP JSON-RPC endpoint, and calls the
ten Cloud Asset Inventory tools in-process on `tools/call`.

| Tool | Operation |
|---|---|
| list_compute_instances | All VMs with machine type, zone, status |
| list_storage_buckets | All GCS buckets with location and storage class |
| count_resources_by_type | Ranked inventory summary |
| find_resources_by_label | Resources matching a label key+value |
| list_static_ip_addresses | All static external IPs |
| find_resources_by_type | Resources of a specific asset type |
| find_resources_by_region | Resources in a region or zone |
| describe_resource | Full config detail for a named resource |
| list_cloud_functions_detail | Functions with runtime, memory, URL, SA, env vars |
| list_bucket_objects | Objects in a GCS bucket with size and last-modified |

---

## Architecture

```
Claude (claude.ai / Claude Desktop) — remote MCP client
     │  1. probe:     POST /mcp with no token → 401 + WWW-Authenticate
     │  2. discover:  GET  /.well-known/oauth-authorization-server   (RFC 8414)
     │  3. register:  POST /oauth/register                           (RFC 7591)
     │  4. login:     GET  /authorize → accounts.google.com → GET /oauth/callback
     │  5. token:     POST /oauth/token   (gcp_ code → Google access token)
     │  6. use:       POST /mcp  (Authorization: Bearer <google access token>)
     ▼
Cloud Function 2nd Gen — gcp-oauth-mcp-func   [PUBLIC; the code enforces auth]
     ├── main.py    route table
     ├── oauth.py   OAuth broker  ── Firestore (transient pending-auth / codes)
     ├── mcp.py     JSON-RPC; validates Bearer via Google tokeninfo
     └── tools.py   10 CAI tools, called in-process
                    │  ADC → function SA
                    ▼
     Cloud Asset Inventory API        Cloud Storage API
     scope: projects/{PROJECT_ID}     (list_bucket_objects only)
```

### The two gaps the broker exists to close

1. **claude.ai's `redirect_uri` is dynamic** — it embeds the org ID
   (`https://claude.ai/api/organizations/<id>/mcp/callback`). Google requires an
   exact allow-list match, so it can never be registered. We register only our
   own fixed `/oauth/callback` and carry Claude's URL through Firestore, keyed by
   the `state` we hand Google.
2. **Google does not implement RFC 7591** dynamic client registration. Without
   `/oauth/register`, Claude would have no `client_id` and the user would be
   pasting credentials by hand — which is exactly the experience
   `aws-agentcore-mcp` is stuck with.

The token handed to Claude is a **genuine Google access token**. We mint no JWTs
and hold no signing keys. There is no custom crypto in this repo.

---

## Auth model — read this before changing anything

**The function is public.** `google_cloud_run_v2_service_iam_member` grants
`roles/run.invoker` to `allUsers`. This is deliberate and it is the inversion at
the heart of the port:

- The OAuth endpoints **cannot** require a token — obtaining one is their job.
- Claude probes `/mcp` **unauthenticated on purpose**, to read the
  `WWW-Authenticate` header that tells it where to log in.

An IAM invoker check would reject both before Python ever runs. So auth moves
into the code: `mcp._get_auth_user` requires a `Bearer` token on every `/mcp`
call and 401s otherwise.

**Token validation pins the audience.** `mcp._resolve_user` calls Google's
`tokeninfo` endpoint and **rejects any token whose `aud` is not our
`client_id`**. This matters more than it looks: a plain `/userinfo` check would
accept a valid Google token minted for *any* application on the internet. The
audience check is what makes the token ours.

**AuthN only, no authZ.** Every authenticated Google user is authorized. Anyone
with a Google account who reaches the endpoint can read this project's resource
inventory. That is fine for a demo; it is not fine for anything real. To lock it
down, filter on the `email` / `hd` claim in `_resolve_user`.

---

## Repository Layout

```
01-functions/
  code/
    main.py          Entry point; route table (OAuth + MCP)
    oauth.py         OAuth broker: discovery, DCR, authorize, callback, token
    mcp.py           MCP JSON-RPC; Bearer validation; tools/call dispatch
    tools.py         TOOL_REGISTRY + 10 CAI handlers + TOOL_FUNCTIONS map
    requirements.txt functions-framework, cloud-asset, storage, firestore
  main.tf            Providers; project locals from credentials.json
  variables.tf       google_client_id / google_client_secret (no defaults) + region
  functions.tf       Function SA + IAM, source bucket, CF2 function, allUsers invoker
  secrets.tf         Client secret in Secret Manager + accessor binding
  firestore.tf       Firestore database + TTL policies on the two state collections
  outputs.tf         function_url, mcp_url, oauth_redirect_uri, project_id
api_setup.sh         Enable required GCP APIs
check_env.sh         Pre-flight: tools + MCP_GOOGLE_* vars + credentials.json
apply.sh             Deploy + validate + print connector instructions
destroy.sh           Teardown
validate.sh          Unauthenticated smoke test of the handshake + auth boundary
credentials.json     GCP service account key (gitignored — place in repo root)
```

---

## The one manual step

Terraform **cannot** create the Google OAuth client. `google_iap_client`
requires an IAP brand, and external brands are console-only. So:

```bash
export MCP_GOOGLE_CLIENT_ID="123456789-abc.apps.googleusercontent.com"
export MCP_GOOGLE_CLIENT_SECRET="GOCSPX-..."
./apply.sh
```

`check_env.sh` hard-fails if either is missing. `apply.sh` prints the redirect
URI to paste onto the client when it finishes.

**This is a developer step, done once — not a user step.** End users still
connect with nothing but a URL. That distinction is the whole point of the
project, and it is what separates this from `aws-agentcore-mcp`, where *every
user* has to paste a client ID and secret.

---

## Environment variables (function)

| Var | Source | Used by |
|-----|--------|---------|
| `GOOGLE_CLOUD_PROJECT` | `local.project_id` | tools.py |
| `MCP_GOOGLE_CLIENT_ID` | `var.google_client_id` | oauth.py + mcp.py (audience check) |
| `MCP_GOOGLE_CLIENT_SECRET` | Secret Manager | oauth.py |

The secret is mounted via `secret_environment_variables`, not a plain env var —
because `list_cloud_functions_detail` **prints function environment variables**,
so a plaintext secret would be readable through the very tools it protects.

---

## Adding a tool

1. Write the handler in `tools.py` — takes an `args` dict, returns a string.
2. Add its entry to `TOOL_REGISTRY`.
3. Add the name → callable mapping to `TOOL_FUNCTIONS`.
4. `./apply.sh`. `tools/list` picks it up automatically.

---

## Gotchas that have bitten

- **Refresh is mandatory here.** Google access tokens last one hour and that is
  not configurable. The Cognito build could skip the `refresh_token` grant
  because Cognito allows a 24-hour access token; this one cannot.
- **`prompt=consent` is not decoration.** Without it Google omits the
  `refresh_token` for a user who has already granted access, and the connector
  dies silently after an hour.
- **Don't randomise the function name.** The URL derives from it, and that URL is
  the registered OAuth redirect URI. A random suffix means a console trip after
  every rebuild.
- **Consent screen must be published.** In "Testing" mode refresh tokens expire
  after 7 days and only allow-listed users can sign in. Publishing requires no
  Google review — `openid`, `email`, and `profile` are all non-sensitive scopes.
- **Don't put an authorizer in front of `/mcp`.** Same lesson as `aws-cognito-mcp`:
  the flow breaks before a token exists.
- The `.drawio` / `.png` diagrams and `00-resources/` still depict the old
  proxy design — regenerate before reusing them.

## Code Commenting Standards

See the workspace-root `.claude/CLAUDE.md`: comment the *why*, not the *what*;
`# ===` section headers; inline comments only for non-obvious intent.
