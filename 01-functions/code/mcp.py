# ==============================================================================
# mcp.py
#
# The MCP endpoint: JSON-RPC 2.0 over streamable HTTP at POST /mcp.
#
# Auth is enforced here, in code — not by the platform. The Cloud Run service is
# deployed with allUsers as invoker, because the OAuth handshake and Claude's
# first unauthenticated /mcp probe both have to reach us before any token
# exists. An IAM invoker check would reject them before the code ever runs.
# (This is the same trade the Cognito build made with API Gateway.)
#
# Methods handled:
#   initialize                 — capability handshake
#   notifications/initialized  — client ack, no response body
#   tools/list                 — TOOL_REGISTRY from tools.py
#   tools/call                 — dispatch to a Python callable in TOOL_FUNCTIONS
# ==============================================================================

import json
import logging
import os
import urllib.parse
import urllib.request

import tools
from oauth import _api_base

logger = logging.getLogger(__name__)

# Returns the token's audience and the user's identity in one call. We use this
# rather than /userinfo because /userinfo happily accepts a token minted for
# *any* Google OAuth client — see _resolve_user.
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

SERVER_NAME     = "gcp-resource-mcp"
SERVER_VERSION  = "2.0.0"
DEFAULT_PROTOCOL = "2025-06-18"


# ==============================================================================
# Authentication
# ==============================================================================

def _resolve_user(token: str) -> dict:
    """Validate a Google access token and return its claims.

    Args:
        token: The raw Bearer token from the Authorization header.

    Returns:
        The tokeninfo claims dict, or {} if the token is invalid, expired, or
        was issued to a different OAuth client.
    """
    url = f"{GOOGLE_TOKENINFO_URL}?" + urllib.parse.urlencode(
        {"access_token": token}
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec B310
            claims = json.loads(resp.read())
    except Exception:
        logger.info("Token validation failed")
        return {}

    # Critical: a valid Google token is not automatically a token for US. Any
    # app on the internet can mint one, and it would sail through a plain
    # /userinfo check. Pinning the audience to our own client_id is what stops
    # a token issued to some other application from calling these tools.
    expected = os.environ.get("MCP_GOOGLE_CLIENT_ID", "")
    if claims.get("aud") != expected:
        logger.warning("Token audience mismatch — rejecting")
        return {}

    return claims


def _get_auth_user(request) -> dict:
    """Extract and validate the Bearer token from the request.

    Returns:
        The token claims, or {} when the header is missing or the token is bad.
    """
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return {}
    return _resolve_user(header[7:].strip())


def _unauthorized(request):
    """401 with the RFC 9728 pointer to our protected-resource metadata.

    This header is how an MCP client discovers where to log in. Claude probes
    /mcp with no token precisely to read it, so this response is part of the
    happy path, not just an error case.
    """
    resource_metadata = (
        f"{_api_base(request)}/.well-known/oauth-protected-resource"
    )
    return (
        json.dumps({"error": "unauthorized"}),
        401,
        {
            "Content-Type":     "application/json",
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{resource_metadata}"'
            ),
        },
    )


# ==============================================================================
# JSON-RPC helpers
# ==============================================================================

def _result(req_id, result):
    return (
        json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}),
        200,
        {"Content-Type": "application/json"},
    )


def _rpc_error(req_id, code, message):
    return (
        json.dumps({
            "jsonrpc": "2.0",
            "id":      req_id,
            "error":   {"code": code, "message": message},
        }),
        200,
        {"Content-Type": "application/json"},
    )


# ==============================================================================
# Entry point — POST /mcp
# ==============================================================================

def handle(request):
    """Handle one MCP JSON-RPC request.

    Args:
        request: Flask-style request; body is a JSON-RPC 2.0 message.

    Returns:
        Flask-style (body, status, headers) tuple. 401 when unauthenticated.
    """
    claims = _get_auth_user(request)
    if not claims:
        return _unauthorized(request)

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    method = payload.get("method", "")
    req_id = payload.get("id")
    params = payload.get("params") or {}

    logger.info("mcp: user=%s method=%s", claims.get("email", "unknown"), method)

    # Notifications carry no id and expect no response body.
    if req_id is None and method.startswith("notifications/"):
        return ("", 202, {})

    if method == "initialize":
        # Echo the client's protocol version back. Asserting our own would make
        # a client on a different revision of the spec give up on us.
        protocol = params.get("protocolVersion", DEFAULT_PROTOCOL)
        return _result(req_id, {
            "protocolVersion": protocol,
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name":    SERVER_NAME,
                                "version": SERVER_VERSION},
        })

    if method == "tools/list":
        return _result(req_id, {"tools": tools.TOOL_REGISTRY})

    if method == "tools/call":
        return _call_tool(req_id, params)

    return _rpc_error(req_id, -32601, f"Method not found: {method}")


def _call_tool(req_id, params: dict):
    """Dispatch tools/call to the matching Python callable.

    The tool runs in this same process — there is no second hop to a per-tool
    function, so a bad argument surfaces as a JSON-RPC error rather than an
    opaque 500 from a downstream service.
    """
    name = params.get("name", "")
    args = params.get("arguments") or {}

    handler = tools.TOOL_FUNCTIONS.get(name)
    if handler is None:
        return _rpc_error(req_id, -32602, f"Unknown tool: {name}")

    try:
        text = handler(args)
    except tools.ToolInputError as exc:
        return _rpc_error(req_id, -32602, str(exc))
    except Exception:
        logger.exception("Tool %s failed", name)
        # Deliberately generic: exception text from the GCP client libraries can
        # carry resource names and IAM detail we don't want to hand back.
        return _rpc_error(req_id, -32603, f"Tool {name} failed")

    return _result(req_id, {"content": [{"type": "text", "text": text}]})
