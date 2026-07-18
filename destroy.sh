#!/bin/bash
# ==============================================================================
# File: destroy.sh
#
# Purpose:
#   Tears down the GCP OAuth MCP stack deployed by apply.sh.
#
#   Nothing is generated locally by this build — no SA key, no Claude Desktop
#   config — so there is nothing to clean up on disk. Terraform owns everything.
# ==============================================================================

set -euo pipefail

./check_env.sh

echo "NOTE: Destroying GCP infrastructure..."

cd 01-functions
terraform init -upgrade
terraform destroy -auto-approve \
    -var="okta_client_id=${MCP_OKTA_CLIENT_ID}" \
    -var="okta_client_secret=${MCP_OKTA_CLIENT_SECRET}" \
    -var="okta_issuer=${MCP_OKTA_ISSUER}" \
    -var="okta_audience=${MCP_OKTA_AUDIENCE:-api://default}"
cd ..

cat <<'EOF'

NOTE: Infrastructure teardown complete.

EOF
