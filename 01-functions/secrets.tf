# ==============================================================================
# Google OAuth client secret — Secret Manager
#
# The client secret is mounted into the function as a secret environment
# variable rather than a plain one. A plain env var is readable by anyone with
# cloudfunctions.get on the project — and one of this project's own MCP tools
# (list_cloud_functions_detail) prints function environment variables, so a
# plaintext secret here would be readable through the very tools it protects.
# ==============================================================================

resource "google_secret_manager_secret" "google_client_secret" {
  secret_id = "gcp-oauth-mcp-client-secret"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "google_client_secret" {
  secret      = google_secret_manager_secret.google_client_secret.id
  secret_data = var.google_client_secret
}

resource "google_secret_manager_secret_iam_member" "func_accessor" {
  secret_id = google_secret_manager_secret.google_client_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.func.email}"
}
