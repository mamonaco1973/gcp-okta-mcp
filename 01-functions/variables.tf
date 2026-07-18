# ==============================================================================
# Google OAuth client credentials
#
# Terraform cannot create an external OAuth 2.0 client — google_iap_client
# requires an IAP brand, and external brands can only be created in the console.
# So the client is made by hand once and passed in here.
#
# There is no default. Google sign-in is not an optional add-on in this build,
# it IS the authentication — a missing value must fail the apply, not quietly
# deploy a stack that cannot authenticate anyone.
#
# Supplied by apply.sh from MCP_GOOGLE_CLIENT_ID / MCP_GOOGLE_CLIENT_SECRET.
# ==============================================================================

variable "google_client_id" {
  description = "Google OAuth 2.0 client ID — the function is an OAuth client of Google"
  type        = string

  validation {
    condition     = length(var.google_client_id) > 0
    error_message = "google_client_id is required. Export MCP_GOOGLE_CLIENT_ID."
  }
}

variable "google_client_secret" {
  description = "Google OAuth 2.0 client secret — server-side only, never sent to the MCP client"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.google_client_secret) > 0
    error_message = "google_client_secret is required. Export MCP_GOOGLE_CLIENT_SECRET."
  }
}

variable "region" {
  description = "Region for the Cloud Function and Firestore database"
  type        = string
  default     = "us-central1"
}
