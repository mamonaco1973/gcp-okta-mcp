#!/bin/bash
# ==============================================================================
# File: validate.sh
#
# Purpose:
#   Smoke-tests the public HTTP surface without any credentials.
#
#   The proxy build could call every tool from a script, because the script held
#   a service account key. That key is gone — tools now require a real Okta user
#   token, which only a browser login can produce. So this script validates
#   what a script legitimately can, and that turns out to be the part that
#   matters most: that the OAuth handshake works and that the auth boundary
#   actually holds.
# ==============================================================================

set -euo pipefail

echo "NOTE: Reading deployment outputs..."

cd 01-functions
FUNCTION_URL=$(terraform output -raw function_url)
cd ..

echo "NOTE: Function URL: ${FUNCTION_URL}"

# ==============================================================================
# Wait for the endpoint to come up
# The allUsers IAM binding can take up to ~60s to propagate after apply.
# ==============================================================================

wait_for_ready() {
    local max_attempts=24 attempt=0 http_code
    echo "NOTE: Waiting for endpoint to become accessible..."
    while (( attempt < max_attempts )); do
        http_code=$(curl -s -o /dev/null -w "%{http_code}" \
            "${FUNCTION_URL}/.well-known/oauth-authorization-server" \
            < /dev/null)
        if [[ "$http_code" == "200" ]]; then
            echo "NOTE: Endpoint ready after $(( attempt * 5 ))s."
            return 0
        fi
        attempt=$(( attempt + 1 ))
        echo "NOTE: HTTP ${http_code} — retrying in 5s... (${attempt}/${max_attempts})"
        sleep 5
    done
    echo "ERROR: Endpoint not ready after $(( max_attempts * 5 ))s."
    exit 1
}

wait_for_ready

# ==============================================================================
# Helper: assert an expected status code
# ==============================================================================

expect() {
    local label="$1" expected="$2" method="$3" route="$4" ; shift 4
    local tmp_file http_code

    tmp_file=$(mktemp)
    http_code=$(curl -s -w "%{http_code}" -o "$tmp_file" \
        -X "$method" "${FUNCTION_URL}${route}" "$@" < /dev/null)

    if [[ "$http_code" == "$expected" ]]; then
        echo "NOTE: OK   ${label} (HTTP ${http_code})"
    else
        echo "ERROR: FAIL ${label} — expected ${expected}, got ${http_code}"
        cat "$tmp_file"
        rm -f "$tmp_file"
        exit 1
    fi
    rm -f "$tmp_file"
}

echo ""
echo "NOTE: Validating the OAuth handshake..."
echo ""

# ------------------------------------------------------------------------------
# The three requests Claude makes before a token exists. All must work while
# completely unauthenticated, or the connector can never bootstrap.
# ------------------------------------------------------------------------------

expect "discovery (RFC 8414)"      200 GET  "/.well-known/oauth-authorization-server"
expect "resource metadata"         200 GET  "/.well-known/oauth-protected-resource"
expect "registration (RFC 7591)"   201 POST "/oauth/register" \
    -H "Content-Type: application/json" -d '{}'

echo ""
echo "NOTE: Validating the auth boundary..."
echo ""

# ------------------------------------------------------------------------------
# The tools must be unreachable without a valid Okta token. A 200 from either
# of these would mean the whole thing is wide open.
# ------------------------------------------------------------------------------

expect "/mcp rejects no token"     401 POST "/mcp" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

expect "/mcp rejects bad token"    401 POST "/mcp" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer not-a-real-token" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# ==============================================================================
# Show the discovery document and the WWW-Authenticate header — these are what
# Claude actually reads to find its way to the login.
# ==============================================================================

echo ""
echo "NOTE: Authorization server metadata:"
curl -s "${FUNCTION_URL}/.well-known/oauth-authorization-server" \
    | jq . | sed 's/^/       /'

echo ""
echo "NOTE: Unauthenticated /mcp probe returns the login pointer:"
curl -s -D - -o /dev/null -X POST "${FUNCTION_URL}/mcp" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
    | grep -i "^www-authenticate" | sed 's/^/       /'

echo ""
echo "========================================================================"
echo "  Validation complete — handshake works, tools are protected."
echo "========================================================================"
echo "  Connector URL: ${FUNCTION_URL}/mcp"
echo "========================================================================"
