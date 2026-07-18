terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "google" {
  credentials = file("../credentials.json")
  project     = local.project_id
  region      = var.region
}

locals {
  project_id = jsondecode(file("../credentials.json")).project_id
}
