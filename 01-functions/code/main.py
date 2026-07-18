# ==============================================================================
# main.py
#
# Cloud Function entry point. Dispatches the public HTTP surface to oauth.py
# (the OAuth broker) and mcp.py (the MCP JSON-RPC endpoint).
#
# Every route here is reachable without credentials, because the Cloud Run
# service grants run.invoker to allUsers. That is deliberate and it is the
# central design decision of this project:
#
#   * The OAuth routes ARE the authentication — they cannot require a token,
#     because their whole job is to get the user one.
#   * /mcp is probed by the client with no token, on purpose, to read the
#     WWW-Authenticate header that tells it where to log in.
#
# So auth moves into the code: mcp.handle() validates the Bearer token on every
# call and 401s otherwise. Nothing reaches Cloud Asset Inventory without a
# Google identity behind it.
#
# HTTP surface:
#   GET  /.well-known/oauth-authorization-server  — RFC 8414 discovery
#   GET  /.well-known/oauth-protected-resource    — RFC 9728 resource metadata
#   POST /oauth/register                          — RFC 7591 registration
#   GET  /authorize                               — redirect to Google login
#   GET  /oauth/callback                          — Google returns here
#   POST /oauth/token                             — code / refresh → tokens
#   POST /mcp                                     — MCP JSON-RPC (auth required)
# ==============================================================================

import functions_framework

import mcp
import oauth

# Path → handler. Paths are matched after stripping surrounding slashes.
ROUTES = {
    ("GET",  ".well-known/oauth-authorization-server"):
        oauth.authorization_server_metadata,
    ("GET",  ".well-known/oauth-protected-resource"):
        oauth.protected_resource_metadata,
    ("POST", "oauth/register"): oauth.register,
    ("GET",  "authorize"):      oauth.authorize,
    ("GET",  "oauth/callback"): oauth.callback,
    ("POST", "oauth/token"):    oauth.token,
    ("POST", "mcp"):            mcp.handle,
}


@functions_framework.http
def gcp_oauth_mcp(request):
    """Route an incoming request to the OAuth broker or the MCP endpoint.

    Args:
        request: Flask-style HTTP request object.

    Returns:
        Flask-style (body, status_code, headers) tuple.
    """
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Max-Age":       "3600",
        })

    path    = request.path.strip("/")
    handler = ROUTES.get((request.method, path))

    if handler is None:
        # A GET on /mcp is not a route we serve, but clients do try it. Answer
        # with the same 401 + WWW-Authenticate pointer so discovery still works
        # rather than dead-ending them on a 404.
        if path == "mcp":
            return mcp._unauthorized(request)
        return ("Not Found", 404, {"Content-Type": "text/plain"})

    response = handler(request)

    # Claude calls /mcp and the OAuth endpoints cross-origin from the browser
    # during the connector handshake, so CORS has to be on every response.
    body, status, headers = response
    headers = dict(headers)
    headers.setdefault("Access-Control-Allow-Origin", "*")
    headers.setdefault(
        "Access-Control-Expose-Headers", "WWW-Authenticate"
    )
    return (body, status, headers)
