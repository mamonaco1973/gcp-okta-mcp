#GCP #MCP #CloudFunctions #OAuth #Okta #ClaudeAI

*Build a Claude Connector on Google Cloud with Okta OIDC*

Connect Claude directly to your Google Cloud project. No local proxy. No service account key file on your laptop. You paste one URL, sign in through Okta, and the tools work.

In this project we put ten Cloud Asset Inventory tools behind a single Cloud Function, and secure it with Okta OIDC. The function is public, and it enforces the token itself — the login routes have to be reachable before a token exists, so authentication moves into the code. Claude discovers the login, registers itself, and sends you to your Okta org. Nothing to configure but the URL.

This is the Okta sibling of the Google-login build. Same GCP host — one public Cloud Function, Firestore, Secret Manager. The one thing that changes is the identity provider: Okta instead of Google. It's the bring-your-own-IdP entry — the broker pattern works against any compliant OIDC provider, and Okta is the demonstration.

This version also validates the token differently. Because Okta's custom authorization server issues a real JWT access token, the function verifies it locally against Okta's signing keys — pinning the issuer, the audience, and the client — instead of calling an introspection endpoint on every request.

And here is the honest twist. Every cloud identity provider — AWS, Google, Azure — skips dynamic client registration, so their MCP builds shim it by hand. Okta actually implements it. But the broker is still the authorization server Claude talks to, so we answer registration ourselves. The gap the whole series chased turns out to be a product choice, not a technical wall.

The one thing you set up by hand is an OIDC app in Okta — Terraform does not manage Okta. You create the app, hand its client ID, secret, and issuer to the deploy, and paste the callback URL back onto the app once. Everything else is a single apply.

We use Cloud Asset Inventory as the example tool set, but the pattern works for any Cloud Function-backed MCP server.

WHAT YOU'LL LEARN
• Exposing Cloud Functions as MCP tools over a remote endpoint Claude connects to directly
• Why the function is public, and how authentication is enforced in code instead of by the platform
• Brokering Okta OIDC for an MCP client — discovery, dynamic client registration, authorize, callback, token, and refresh
• Validating an Okta access-token JWT locally against the JWKS, pinning issuer, audience, and client
• Why the cloud IdPs all skip dynamic client registration — and why Okta is the one that doesn't
• Wiring an Okta OIDC app to a Cloud Function: the client credentials in, the callback URL out

INFRASTRUCTURE DEPLOYED
• One Cloud Function 2nd Gen (Python 3.11) — the OAuth broker, the MCP endpoint, and ten tools, public with auth enforced in code
• Cloud Asset Inventory access via Application Default Credentials — no credentials in code
• Firestore for transient OAuth login state, swept on a short TTL
• The Okta client secret held in Secret Manager, never a plaintext environment variable
• An Okta OIDC app plus a custom authorization server you create once by hand — the piece Terraform cannot provision
• Everything else provisioned with Terraform in a single apply, torn down with a single command

GitHub
https://github.com/mamonaco1973/gcp-okta-mcp

README
https://github.com/mamonaco1973/gcp-okta-mcp/blob/main/README.md

TIMESTAMPS
00:00 Introduction
00:44 Architecture
01:45 Securing MCP
02:52 Deploy It Yourself
