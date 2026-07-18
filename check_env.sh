#!/bin/bash
# ==============================================================================
# File: check_env.sh
#
# Purpose:
#   Pre-flight validation: verifies required tools are in PATH, that the Google
#   OAuth client credentials are exported, that credentials.json exists,
#   authenticates the gcloud SA, and enables required APIs via api_setup.sh.
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
# Google OAuth client check
#
# Terraform cannot create an external OAuth client, so these must be supplied.
# Fail loudly here rather than letting the apply get halfway and produce a
# stack that deploys cleanly but cannot authenticate anyone.
# ==============================================================================

missing=0
for var in MCP_GOOGLE_CLIENT_ID MCP_GOOGLE_CLIENT_SECRET; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: ${var} is not set."
        missing=1
    fi
done

if [[ "$missing" -eq 1 ]]; then
    cat <<'EOF'

--------------------------------------------------------------------------------
This project needs a Google OAuth 2.0 client. Terraform cannot create one:
google_iap_client requires an IAP brand, and external brands are console-only.
So you create it once, by hand, and export it.

  1. APIs & Services -> Credentials -> Create Credentials
     -> OAuth client ID -> Application type: Web application

  2. Export both values, then re-run:

       export MCP_GOOGLE_CLIENT_ID="123456789-abc.apps.googleusercontent.com"
       export MCP_GOOGLE_CLIENT_SECRET="GOCSPX-..."
       ./apply.sh

  3. On the FIRST apply the function URL does not exist yet, so leave the
     redirect URI blank for now. apply.sh prints the exact URI to paste back
     onto the client when it finishes. The function name is not randomised, so
     that URI is stable — you only ever do this once.
--------------------------------------------------------------------------------

EOF
    exit 1
fi

echo "NOTE: Google OAuth client ID: ${MCP_GOOGLE_CLIENT_ID}"

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
