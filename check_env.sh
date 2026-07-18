#!/bin/bash
# ==============================================================================
# File: check_env.sh
#
# Purpose:
#   Pre-flight validation: verifies required tools are in PATH, that the Okta
#   OIDC app credentials + issuer are exported, that the issuer is reachable,
#   that credentials.json exists, authenticates the gcloud SA, and enables the
#   required GCP APIs via api_setup.sh.
# ==============================================================================

set -euo pipefail

# ==============================================================================
# Tool check
# ==============================================================================

echo "NOTE: Validating required commands..."

commands=("gcloud" "terraform" "jq" "curl")
all_found=true

for cmd in "${commands[@]}"; do
    if command -v "$cmd" &> /dev/null; then
        echo "NOTE: $cmd found."
    else
        echo "ERROR: $cmd not found in PATH."
        all_found=false
    fi
done

[ "$all_found" = true ] || exit 1

# ==============================================================================
# Okta OIDC app check
#
# Terraform does not manage Okta here, so the app is created by hand once and its
# values exported. Fail loudly rather than deploy a stack that cannot
# authenticate anyone. MCP_OKTA_AUDIENCE is optional (defaults to api://default).
# ==============================================================================

missing=0
for var in MCP_OKTA_CLIENT_ID MCP_OKTA_CLIENT_SECRET MCP_OKTA_ISSUER; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: ${var} is not set."
        missing=1
    fi
done

if [[ "$missing" -eq 1 ]]; then
    cat <<'EOF'

--------------------------------------------------------------------------------
This project needs an Okta OIDC application and a custom authorization server.
Create them once in the Okta admin console, then export the values:

  1. Applications -> Create App Integration -> OIDC - Web Application
     Grant types: Authorization Code + Refresh Token.

  2. Security -> API -> Authorization Servers -> use "default"
     (issuer https://<org>.okta.com/oauth2/default, audience api://default).

  3. Export, then re-run:

       export MCP_OKTA_CLIENT_ID="0oa..."
       export MCP_OKTA_CLIENT_SECRET="..."
       export MCP_OKTA_ISSUER="https://<org>.okta.com/oauth2/default"
       export MCP_OKTA_AUDIENCE="api://default"   # optional
       ./apply.sh

  4. On the FIRST apply the function URL does not exist yet, so leave the
     sign-in redirect URI blank for now. apply.sh prints the exact URI to add to
     the Okta app when it finishes. The function name is not randomised, so that
     URI is stable — you only ever do this once.
--------------------------------------------------------------------------------

EOF
    exit 1
fi

# Values intentionally not echoed — the client ID and issuer carry the org name.
echo "NOTE: Okta client ID is set."
echo "NOTE: Okta issuer is set."

# ------------------------------------------------------------------------------
# Issuer reachability — confirm the custom AS metadata resolves. Catches typos
# in the issuer and connectivity problems before the apply, the way the Azure
# build validated its tenant/user flow up front. Retried for transient blips.
# ------------------------------------------------------------------------------

_validate_issuer() {
    local meta
    meta=$(curl -s "${MCP_OKTA_ISSUER}/.well-known/oauth-authorization-server")
    local auth_ep
    auth_ep=$(echo "$meta" | jq -r '.authorization_endpoint // empty')
    if [[ -z "$auth_ep" ]]; then
        echo "WARNING: Could not read authorization metadata from the issuer."
        return 1
    fi
    # Endpoint URL not echoed — it embeds the org name.
    echo "NOTE: Okta authorization server reachable."
}

for _attempt in 1 2 3; do
    if _validate_issuer; then
        break
    fi
    if [[ "$_attempt" -lt 3 ]]; then
        echo "NOTE: Retrying issuer check in 5s (${_attempt}/3)..."
        sleep 5
    else
        echo "ERROR: Okta issuer is not reachable or not a valid custom AS. Check MCP_OKTA_ISSUER."
        exit 1
    fi
done

# ==============================================================================
# Credentials check
# ==============================================================================

if [[ ! -f "credentials.json" ]]; then
    echo "ERROR: credentials.json not found in $(pwd)."
    echo "       Place your GCP service account key file at credentials.json."
    exit 1
fi

PROJECT_ID=$(jq -r '.project_id'    credentials.json)
SA_EMAIL=$(jq   -r '.client_email'  credentials.json)

echo "NOTE: Project ID:       ${PROJECT_ID}"
echo "NOTE: Service account:  ${SA_EMAIL}"

# Activate the service account so gcloud commands use its identity.
gcloud auth activate-service-account \
    --key-file=credentials.json \
    --quiet

echo "NOTE: gcloud authenticated as ${SA_EMAIL}."

# ==============================================================================
# API enablement
#
# Runs BEFORE `gcloud config set project`. That command validates the project
# through the Cloud Resource Manager API, which is not enabled on a fresh
# project — so setting it first emits an alarming SERVICE_DISABLED warning for
# an operation that actually succeeded. api_setup.sh passes --project explicitly
# and does not depend on core/project being set.
# ==============================================================================

./api_setup.sh

gcloud config set project "$PROJECT_ID" --quiet
