# ==============================================================================
# Okta OIDC client credentials + custom authorization server
#
# The OIDC app is created by hand once in the Okta admin console (Terraform does
# not manage Okta here — no Okta provider). These values come from that app and
# are passed in by apply.sh from the MCP_OKTA_* environment variables.
#
# There are no defaults on the required three: Okta sign-in IS the
# authentication, so a missing value must fail the apply rather than deploy a
# stack that cannot authenticate anyone.
# ==============================================================================

variable "okta_client_id" {
  description = "Okta OIDC app client ID — the function is an OIDC client of Okta"
  type        = string

  validation {
    condition     = length(var.okta_client_id) > 0
    error_message = "okta_client_id is required. Export MCP_OKTA_CLIENT_ID."
  }
}

variable "okta_client_secret" {
  description = "Okta OIDC app client secret — server-side only, never sent to the MCP client"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.okta_client_secret) > 0
    error_message = "okta_client_secret is required. Export MCP_OKTA_CLIENT_SECRET."
  }
}

variable "okta_issuer" {
  description = "Okta custom authorization-server issuer, e.g. https://<org>.okta.com/oauth2/default"
  type        = string

  validation {
    condition     = can(regex("^https://.+/oauth2/.+$", var.okta_issuer))
    error_message = "okta_issuer must be a custom AS issuer like https://<org>.okta.com/oauth2/default. Export MCP_OKTA_ISSUER."
  }
}

variable "okta_audience" {
  description = "Audience of the Okta custom AS access tokens (the API the token is for)"
  type        = string
  default     = "api://default"
}

variable "region" {
  description = "Region for the Cloud Function and Firestore database"
  type        = string
  default     = "us-central1"
}
