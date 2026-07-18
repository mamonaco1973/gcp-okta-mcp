# ==============================================================================
# Firestore — transient OAuth state
#
# This replaces the DynamoDB table in the AWS/Cognito build. It holds nothing
# durable: only the two short-lived documents an in-flight login needs.
#
#   mcp_pending_auth/<session>  Claude's redirect_uri + state, before login
#   mcp_auth_codes/<gcp_ code>  Google tokens, one-time use, after login
#
# Both are deleted the moment they are consumed. The TTL policies below are the
# backstop for logins that are abandoned halfway through.
# ==============================================================================

resource "google_firestore_database" "default" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Keep the database if the stack is torn down while documents still exist —
  # deleting a Firestore database is not something a `destroy` should do
  # silently, and it is free when empty.
  deletion_policy = "DELETE"
}

# ------------------------------------------------------------------------------
# TTL policies — Firestore sweeps documents once expires_at passes.
# Requires a real Timestamp field, which is why oauth.py stores a datetime
# rather than an epoch int.
# ------------------------------------------------------------------------------

resource "google_firestore_field" "pending_auth_ttl" {
  database   = google_firestore_database.default.name
  collection = "mcp_pending_auth"
  field      = "expires_at"

  ttl_config {}
}

resource "google_firestore_field" "auth_codes_ttl" {
  database   = google_firestore_database.default.name
  collection = "mcp_auth_codes"
  field      = "expires_at"

  ttl_config {}
}
