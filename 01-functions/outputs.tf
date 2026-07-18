output "function_url" {
  description = "Base URL of the MCP function."
  value       = google_cloudfunctions2_function.mcp.service_config[0].uri
}

output "mcp_url" {
  description = "The URL to paste into Claude when adding the connector."
  value       = "${google_cloudfunctions2_function.mcp.service_config[0].uri}/mcp"
}

output "oauth_redirect_uri" {
  description = "Sign-in redirect URI to register on the Okta OIDC app."
  value       = "${google_cloudfunctions2_function.mcp.service_config[0].uri}/oauth/callback"
}

output "project_id" {
  value = local.project_id
}

output "source_bucket_name" {
  value = google_storage_bucket.func_source.name
}
