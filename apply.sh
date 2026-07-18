#!/bin/bash
# ==============================================================================
# File: apply.sh
#
# Purpose:
#   Deploys the GCP OAuth MCP stack: environment validation -> Terraform ->
#   validation -> connector instructions.
#
#   There is no key export and no Claude Desktop config generation here. That is
#   the point of this build: users authenticate as themselves through Google, so
#   there is nothing to hand them but a URL.
# ==============================================================================

set -euo pipefail

echo "NOTE: Running environment validation..."
./check_env.sh

# ==============================================================================
# Deploy infrastructure and function
# ==============================================================================

echo "NOTE: Deploying GCP infrastructure..."

cd 01-functions
terraform init -upgrade
terraform apply -auto-approve \
    -var="google_client_id=${MCP_GOOGLE_CLIENT_ID}" \
    -var="google_client_secret=${MCP_GOOGLE_CLIENT_SECRET}"

MCP_URL=$(terraform output -raw mcp_url)
REDIRECT_URI=$(terraform output -raw oauth_redirect_uri)
cd ..

# ==============================================================================
# Post-deployment validation
# ==============================================================================

echo "NOTE: Running post-deployment validation..."
./validate.sh

# ==============================================================================
# Connector instructions
#
# The redirect URI is printed every run, not just the first. It is the one
# manual step in the whole deploy, and a stale or missing entry on the OAuth
# client produces a redirect_uri_mismatch at login — an error whose cause is
# nowhere near where it surfaces.
# ==============================================================================

cat <<EOF

================================================================================
  Deployment complete.
================================================================================

  STEP 1 — one time, in the Cloud Console
  ---------------------------------------
  APIs & Services -> Credentials -> your OAuth 2.0 client
  Add this under "Authorized redirect URIs":

      ${REDIRECT_URI}

  STEP 2 — connect Claude
  -----------------------
  Settings -> Connectors -> Add custom connector, and paste:

      ${MCP_URL}

  That is the whole configuration. No client ID, no secret, no key file, and
  no local proxy. Claude discovers the authorization server, registers itself,
  and sends you to Google to log in.

================================================================================
EOF
