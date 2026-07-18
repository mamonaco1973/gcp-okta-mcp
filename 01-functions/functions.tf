# ==============================================================================
# Random suffix
# Only the source bucket needs one — GCS bucket names are globally unique. The
# function name is deliberately NOT randomised (see below).
# ==============================================================================

resource "random_id" "suffix" {
  byte_length = 4
}

# ==============================================================================
# Function service account
# One SA now, not two. The proxy SA and its downloadable JSON key are gone —
# users authenticate as themselves through Google OAuth, so there is no longer a
# long-lived private key sitting on anyone's laptop.
# ==============================================================================

resource "google_service_account" "func" {
  account_id   = "gcp-okta-mcp-sa"
  display_name = "GCP Okta MCP Function SA"
}

# Viewer on Cloud Asset Inventory — lets the function query all project assets.
resource "google_project_iam_member" "func_asset_viewer" {
  project = local.project_id
  role    = "roles/cloudasset.viewer"
  member  = "serviceAccount:${google_service_account.func.email}"
}

# Object viewer — lets the function list and read GCS bucket contents.
resource "google_project_iam_member" "func_storage_viewer" {
  project = local.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.func.email}"
}

# Firestore access — the OAuth broker stores transient pending-auth and
# auth-code documents while a login is in flight.
resource "google_project_iam_member" "func_firestore_user" {
  project = local.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.func.email}"
}

# ==============================================================================
# Function source bucket and archive
# ==============================================================================

resource "google_storage_bucket" "func_source" {
  name                        = "gcp-okta-mcp-src-${random_id.suffix.hex}"
  location                    = "US"
  force_destroy               = true
  uniform_bucket_level_access = true
}

data "archive_file" "func_source" {
  type        = "zip"
  source_dir  = "${path.module}/code"
  output_path = "${path.module}/func_source.zip"
  excludes    = ["__pycache__", "*.pyc"]
}

resource "google_storage_bucket_object" "func_source" {
  # Content hash in the object name triggers a redeploy on any source change.
  name   = "func-${data.archive_file.func_source.output_md5}.zip"
  bucket = google_storage_bucket.func_source.name
  source = data.archive_file.func_source.output_path
}

# ==============================================================================
# Cloud Function (2nd Gen)
#
# The name has no random suffix, and that is load-bearing. The function's URL is
# derived from its name, and that URL is the OAuth redirect URI registered by
# hand on the Google client. Randomising the name would change the redirect URI
# on every rebuild and force a trip back to the console each time.
# ==============================================================================

resource "google_cloudfunctions2_function" "mcp" {
  name     = "gcp-okta-mcp-func"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "gcp_okta_mcp"
    source {
      storage_source {
        bucket = google_storage_bucket.func_source.name
        object = google_storage_bucket_object.func_source.name
      }
    }
  }

  service_config {
    service_account_email = google_service_account.func.email
    min_instance_count    = 0
    max_instance_count    = 10
    available_memory      = "256M"
    timeout_seconds       = 60

    environment_variables = {
      GOOGLE_CLOUD_PROJECT = local.project_id
      # The function acts as an OIDC *client* of Okta with client_id; issuer and
      # audience drive the login URLs (oauth.py) and JWT validation (mcp.py).
      # These are public; only the client secret below is held server-side.
      MCP_OKTA_CLIENT_ID = var.okta_client_id
      MCP_OKTA_ISSUER    = var.okta_issuer
      MCP_OKTA_AUDIENCE  = var.okta_audience
    }

    secret_environment_variables {
      key        = "MCP_OKTA_CLIENT_SECRET"
      project_id = local.project_id
      secret     = google_secret_manager_secret.okta_client_secret.secret_id
      version    = "latest"
    }
  }

  depends_on = [
    google_secret_manager_secret_version.okta_client_secret,
    google_secret_manager_secret_iam_member.func_accessor,
  ]
}

# ==============================================================================
# Public invocation
#
# This is the inversion at the heart of the project. The proxy build kept the
# function private and let Cloud Run's IAM check be the entire auth layer. That
# cannot work for a remote MCP connector:
#
#   * The OAuth endpoints must be reachable without a token — getting a token is
#     what they are for.
#   * Claude probes /mcp unauthenticated on purpose, to read the
#     WWW-Authenticate header that tells it where to log in.
#
# So the door opens and mcp.py enforces the Bearer token in code instead. An IAM
# invoker check here would reject the handshake before Python ever runs.
# ==============================================================================

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = local.project_id
  location = google_cloudfunctions2_function.mcp.location
  name     = google_cloudfunctions2_function.mcp.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
