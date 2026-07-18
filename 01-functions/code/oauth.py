# ==============================================================================
# oauth.py
#
# OAuth 2.0 authorization-server broker for the MCP connector.
#
# This function plays two roles at once:
#   * To Claude, it IS the authorization server — it serves discovery, dynamic
#     client registration, /authorize and /oauth/token.
#   * To Okta, it is an ordinary OIDC *client* — it redirects to Okta's login,
#     receives the code at a fixed callback, and exchanges it.
#
# Why broker at all? Two gaps:
#   1. claude.ai's redirect_uri embeds the org ID
#      (https://claude.ai/api/organizations/<id>/mcp/callback). Okta requires an
#      exact allow-list match, so we cannot register it. We register only our own
#      fixed /oauth/callback and carry Claude's URL through in `state`.
#   2. Claude has no client_id until something registers it. Okta actually DOES
#      implement RFC 7591 dynamic client registration (unlike the cloud IdPs) —
#      but the broker is still the authorization server *to Claude*, so we keep
#      the /oauth/register shim rather than exposing Okta's registration endpoint
#      and leaking the upstream. The shim hands back our one shared client_id.
#
# We talk to Okta's custom "default" authorization server
# (https://<org>.okta.com/oauth2/default), whose access tokens are real JWTs with
# aud = api://default and iss = the issuer. That is what lets mcp.py validate the
# token locally against Okta's JWKS instead of calling an introspection endpoint.
#
# Flow:
#   1. GET  /.well-known/oauth-authorization-server — we are the auth server
#   2. POST /oauth/register  — hand back our shared client_id (RFC 7591)
#   3. GET  /authorize      — stash Claude's redirect_uri + state, 302 to Okta
#   4. GET  /oauth/callback — Okta returns here; swap code for tokens, mint a
#                             one-time gcp_ code, 302 back to Claude
#   5. POST /oauth/token    — gcp_ code → Okta access token (+ refresh token)
#   6. POST /mcp            — Bearer is a real Okta access-token JWT, validated in
#                             mcp.py against Okta's JWKS (issuer + audience pinned)
#
# The token handed to Claude is a genuine Okta access token. We mint no JWTs and
# hold no signing keys — there is no custom crypto in this file.
#
# Firestore records (5-min TTL, swept by a Firestore TTL policy on expires_at):
#   mcp_pending_auth/<session>  — Claude's redirect_uri + state, pre-login
#   mcp_auth_codes/<gcp_ code>  — Okta tokens, one-time use, post-login
# ==============================================================================

import json
import logging
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

logger = logging.getLogger(__name__)

# Okta's OIDC endpoints, built from the custom-AS issuer (e.g.
# https://<org>.okta.com/oauth2/default). Fixed once the issuer is set, public,
# and not user-controlled — the urlopen calls below are safe by construction.
OKTA_ISSUER    = os.environ.get("MCP_OKTA_ISSUER", "").rstrip("/")
OKTA_AUTH_URL  = f"{OKTA_ISSUER}/v1/authorize"
OKTA_TOKEN_URL = f"{OKTA_ISSUER}/v1/token"

# Scopes we ask Okta for. openid/email/profile identify the user; offline_access
# is what yields a refresh token (Okta's equivalent of Google's access_type
# =offline + prompt=consent).
OKTA_SCOPES = "openid email profile offline_access"

PENDING_TTL_SECONDS = 300   # 5 minutes, for both pending-auth and auth-code docs

COLLECTION_PENDING  = "mcp_pending_auth"
COLLECTION_CODES    = "mcp_auth_codes"

_db = None


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def _client_id() -> str:
    return os.environ.get("MCP_OKTA_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("MCP_OKTA_CLIENT_SECRET", "")


# ==============================================================================
# Response helpers — Flask-style (body, status, headers) tuples
# ==============================================================================

def _json(body: dict, status: int = 200):
    return (json.dumps(body), status, {"Content-Type": "application/json"})


def _error(msg: str, status: int = 400):
    return _json({"error": msg}, status)


def _redirect(location: str):
    return ("", 302, {"Location": location})


def _api_base(request) -> str:
    """Public base URL of this function, taken from the incoming request.

    Cloud Run terminates TLS upstream, so request.scheme can read as http. The
    external URL is always https, so hard-code the scheme rather than trust it.
    """
    return f"https://{request.host}"


def _expiry():
    """TTL timestamp for Firestore state docs.

    Stored as a real timestamp (not an epoch int) so the Firestore TTL policy in
    firestore.tf can sweep expired docs automatically.
    """
    return datetime.now(timezone.utc) + timedelta(seconds=PENDING_TTL_SECONDS)


def _is_expired(doc: dict) -> bool:
    expires_at = doc.get("expires_at")
    if expires_at is None:
        return True
    return datetime.now(timezone.utc) > expires_at


def _post_form(url: str, fields: dict) -> dict:
    """POST a form-encoded body and parse the JSON response.

    Args:
        url:    Target endpoint (always a fixed Okta URL — see module header).
        fields: Form fields to send.

    Returns:
        Parsed JSON response, or {} on any failure.
    """
    data = urllib.parse.urlencode(fields).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            return json.loads(resp.read())
    except Exception:
        logger.exception("Token request to %s failed", url)
        return {}


def parse_form_body(request) -> dict:
    """Parse an OAuth request body as form-encoded, falling back to JSON.

    Token requests are form-encoded per RFC 6749, but some clients send JSON, so
    try both rather than trusting the Content-Type header.
    """
    raw = request.get_data(as_text=True) or ""
    if raw.lstrip().startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}


# ==============================================================================
# Discovery — GET /.well-known/oauth-authorization-server  (RFC 8414)
# ==============================================================================

def authorization_server_metadata(request):
    """Advertise this function as the OAuth authorization server.

    Every endpoint here points at us, not at Okta. Claude never learns that Okta
    is behind the curtain — which is what lets the broker stay the single
    authorization server the connector talks to.
    """
    base = _api_base(request)
    return _json({
        "issuer":                                base,
        "authorization_endpoint":                f"{base}/authorize",
        "token_endpoint":                        f"{base}/oauth/token",
        "registration_endpoint":                 f"{base}/oauth/register",
        "grant_types_supported":                 ["authorization_code",
                                                  "refresh_token"],
        "response_types_supported":              ["code"],
        "scopes_supported":                      ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


# ==============================================================================
# Protected-resource metadata — GET /.well-known/oauth-protected-resource
# The MCP spec has the client read this after an unauthenticated probe of /mcp
# to learn which authorization server guards the resource. Here, that's us.
# ==============================================================================

def protected_resource_metadata(request):
    """Point the MCP client at the authorization server guarding /mcp."""
    base = _api_base(request)
    return _json({
        "resource":              base,
        "authorization_servers": [base],
        "scopes_supported":      ["openid", "email", "profile"],
    })


# ==============================================================================
# Dynamic client registration — POST /oauth/register  (RFC 7591)
# ==============================================================================

def register(request):
    """Hand back the shared client_id so Claude can self-register.

    Okta does implement RFC 7591, but the broker is the authorization server to
    Claude, so we answer registration ourselves rather than proxy Okta's
    endpoint. We return auth method "none": the real Okta client secret stays
    server-side and is never sent to the client.
    """
    base = _api_base(request)
    return _json({
        "client_id":                  _client_id(),
        "token_endpoint_auth_method": "none",
        "grant_types":                ["authorization_code", "refresh_token"],
        "response_types":             ["code"],
        "redirect_uris":              [f"{base}/oauth/callback"],
    }, status=201)


# ==============================================================================
# Authorization — GET /authorize
# ==============================================================================

def authorize(request):
    """Stash Claude's callback details, then send the browser to Okta.

    Okta only ever sees our own fixed /oauth/callback as the redirect_uri.
    Claude's dynamic URL rides along in Firestore, keyed by the session id we
    pass to Okta as `state`.
    """
    redirect_uri  = (request.args.get("redirect_uri")  or "").strip()
    state         = request.args.get("state")          or ""
    response_type = request.args.get("response_type")  or ""

    if response_type != "code":
        return _error("unsupported_response_type", 400)
    if not redirect_uri:
        return _error("invalid_request", 400)

    # PKCE params from Claude are accepted and ignored: the code we hand back is
    # single-use and consumed server-side, so there is no interception window
    # for PKCE to close. Okta's own leg of the flow is protected by the client
    # secret instead.
    session_id = secrets.token_urlsafe(16)
    _firestore().collection(COLLECTION_PENDING).document(session_id).set({
        "redirect_uri": redirect_uri,
        "state":        state,
        "expires_at":   _expiry(),
    })

    okta_auth = f"{OKTA_AUTH_URL}?" + urllib.parse.urlencode({
        "client_id":     _client_id(),
        "response_type": "code",
        "scope":         OKTA_SCOPES,
        "redirect_uri":  f"{_api_base(request)}/oauth/callback",
        "state":         session_id,
    })

    logger.info("authorize: session=%s", session_id)
    return _redirect(okta_auth)


# ==============================================================================
# Callback — GET /oauth/callback
# ==============================================================================

def callback(request):
    """Exchange Okta's code for tokens, then hand Claude a one-time code."""
    okta_code  = (request.args.get("code")  or "").strip()
    session_id = (request.args.get("state") or "").strip()

    if not okta_code or not session_id:
        return _error("invalid_request", 400)

    db          = _firestore()
    pending_ref = db.collection(COLLECTION_PENDING).document(session_id)
    pending     = pending_ref.get()

    if not pending.exists:
        return _error("invalid_state", 400)

    pending_doc = pending.to_dict()
    if _is_expired(pending_doc):
        pending_ref.delete()
        return _error("invalid_state", 400)

    tokens = _post_form(OKTA_TOKEN_URL, {
        "grant_type":    "authorization_code",
        "code":          okta_code,
        "client_id":     _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri":  f"{_api_base(request)}/oauth/callback",
    })

    if "access_token" not in tokens:
        logger.error("Okta token exchange returned no access_token")
        return _error("okta_exchange_failed", 502)

    # Mint a one-time code and stash the Okta tokens behind it. Claude will
    # trade this for the real tokens at /oauth/token in the next request.
    auth_code = "gcp_" + secrets.token_urlsafe(32)
    db.collection(COLLECTION_CODES).document(auth_code).set({
        "access_token":  tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_in":    tokens.get("expires_in", 3600),
        "expires_at":    _expiry(),
    })
    pending_ref.delete()

    dest     = pending_doc["redirect_uri"]
    sep      = "&" if "?" in dest else "?"
    location = (
        f"{dest}{sep}code={auth_code}"
        f"&state={urllib.parse.quote(pending_doc.get('state', ''), safe='')}"
    )

    logger.info("callback: issued auth code for session=%s", session_id)
    return _redirect(location)


# ==============================================================================
# Token — POST /oauth/token
# ==============================================================================

def token(request):
    """Issue tokens to Claude.

    Two grants:
      authorization_code — trade the one-time gcp_ code for the Okta tokens
      refresh_token      — Okta access tokens are short-lived, so refresh is
                           required. offline_access (requested in authorize) is
                           what makes the refresh_token available.
    """
    params     = parse_form_body(request)
    grant_type = params.get("grant_type", "")

    if grant_type == "refresh_token":
        return _refresh(params)
    if grant_type != "authorization_code":
        return _error("unsupported_grant_type", 400)

    code = (params.get("code") or "").strip()
    if not code:
        return _error("invalid_request", 400)

    db       = _firestore()
    code_ref = db.collection(COLLECTION_CODES).document(code)
    snapshot = code_ref.get()

    if not snapshot.exists:
        return _error("invalid_grant", 400)

    doc = snapshot.to_dict()
    # One-time use — burn the code before returning, valid or not.
    code_ref.delete()

    if _is_expired(doc):
        return _error("invalid_grant", 400)

    # No client authentication check: clients registered via /oauth/register use
    # auth method "none". The security boundary is the single-use code above.
    body = {
        "access_token": doc["access_token"],
        "token_type":   "Bearer",
        "expires_in":   doc.get("expires_in", 3600),
    }
    if doc.get("refresh_token"):
        body["refresh_token"] = doc["refresh_token"]

    return _json(body)


def _refresh(params: dict):
    """Exchange an Okta refresh token for a fresh access token.

    Okta rotates refresh tokens by default, so echo back whichever token the
    response carries — the new one if Okta rotated, else the client's own —
    otherwise Claude would drop the only valid copy and be unable to refresh.
    """
    refresh_token = (params.get("refresh_token") or "").strip()
    if not refresh_token:
        return _error("invalid_request", 400)

    tokens = _post_form(OKTA_TOKEN_URL, {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     _client_id(),
        "client_secret": _client_secret(),
        "scope":         OKTA_SCOPES,
    })

    if "access_token" not in tokens:
        return _error("invalid_grant", 400)

    return _json({
        "access_token":  tokens["access_token"],
        "token_type":    "Bearer",
        "expires_in":    tokens.get("expires_in", 3600),
        "refresh_token": tokens.get("refresh_token", refresh_token),
    })
