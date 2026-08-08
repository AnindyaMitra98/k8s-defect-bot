terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    # >= 2.4 for glob patterns in `excludes` -- without it the .venv directory
    # (6000+ files) ends up in the uploaded archive.
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "k8s-defect-bot"
      ManagedBy = "terraform"
      Purpose   = "test-environment"
    }
  }
}
