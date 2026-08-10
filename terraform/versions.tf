terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    # The node's SSH key is generated on apply rather than reusing a key from
    # your account, so a destroy leaves nothing behind and two people running
    # this config never share a key.
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    # Auto-detects your public IP for the security group. Only used when
    # allowed_cidr is left null.
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }
}

# This config provisions infrastructure only. There are deliberately no
# provisioners anywhere in it -- no remote-exec, no local-exec, no file. The
# node bootstraps itself from cloud-init (user_data), which is a native EC2
# mechanism: it runs on the instance whether or not Terraform is still watching,
# and a failure inside it cannot leave the apply half-finished. Installing the
# bot is a separate manual step -- see README.md.

provider "aws" {
  region = var.region

  # Everything this config creates carries these, so a stray resource after a
  # failed destroy is trivial to find in the console or the bill.
  default_tags {
    tags = {
      Project   = "k8s-defect-bot"
      ManagedBy = "terraform"
      Purpose   = "test-environment"
    }
  }
}
