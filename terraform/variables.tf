variable "region" {
  description = "AWS region. Spot prices vary by region; us-east-1 and us-east-2 are usually cheapest."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name, so everything is easy to find and delete."
  type        = string
  default     = "kdb-test"
}

variable "instance_type" {
  description = <<-EOT
    One instance runs k3s, the collector, and the node agent -- and builds the
    image. t4g.small (2 vCPU, 2 GiB, ARM) is the cheapest type that fits all of
    that. t4g.micro (1 GiB) runs the cluster but OOMs during `docker build`.
    x86 types (t3.small) work too; the AMI architecture follows automatically.
  EOT
  type        = string
  default     = "t4g.small"
}

variable "use_spot" {
  description = <<-EOT
    Spot pricing, roughly 60% off on-demand. The request is one-time, so an
    interruption terminates the instance and the next `terraform apply` builds a
    fresh one. That is fine here precisely because nothing on this node is worth
    keeping -- the cluster holds no state you cannot recreate in ten minutes.
  EOT
  type        = bool
  default     = true
}

variable "root_volume_gb" {
  description = "Root disk. 20 GiB fits Ubuntu, k3s, Docker, and the built image with room to spare."
  type        = number
  default     = 20
}

variable "allowed_cidr" {
  description = <<-EOT
    The only CIDR allowed to reach SSH, the dashboard, and the Kubernetes API.
    Leave null to auto-detect your current public IP as a /32. Set it explicitly
    if you are behind a changing address or a corporate NAT.
  EOT
  type        = string
  default     = null

  # The dashboard shows pod names, images, events, and container log tails from
  # the whole cluster, with no TLS in front of it. Opening it to the internet is
  # never what you meant.
  validation {
    condition     = var.allowed_cidr == null ? true : var.allowed_cidr != "0.0.0.0/0"
    error_message = "Refusing 0.0.0.0/0. Set a real CIDR, or leave null to auto-detect your IP."
  }
}

variable "expose_kubernetes_api" {
  description = "Open 6443 to allowed_cidr so kubectl works from your workstation. Off means SSH to the node instead."
  type        = bool
  default     = true
}

variable "k3s_version" {
  description = "Pin a k3s version (e.g. v1.30.6+k3s1). Empty installs the current stable channel."
  type        = string
  default     = ""
}

variable "clone_repo" {
  description = <<-EOT
    Have cloud-init clone the bot's source onto the node, ready for the manual
    build and Helm install. It is only cloned, never built or installed -- see
    README.md. Set false for a genuinely bare cluster.
  EOT
  type        = bool
  default     = true
}

variable "repo_url" {
  description = "Source cloned onto the node. Must be reachable without credentials, since cloud-init carries none."
  type        = string
  default     = "https://github.com/AnindyaMitra98/k8s-defect-bot.git"
}

variable "repo_branch" {
  description = "Branch to clone. The node builds whatever is pushed here, not your local working tree."
  type        = string
  default     = "main"
}
