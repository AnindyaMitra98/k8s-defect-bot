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
    One instance runs k3s, the collector, and the node agent. t4g.small (2 vCPU,
    2 GiB, ARM) is the cheapest type that comfortably fits all three. t4g.micro
    (1 GiB) works but leaves little headroom for the image build; if you use it,
    build the image elsewhere. x86 types (t3.small) also work -- the AMI
    architecture follows the instance type automatically.
  EOT
  type        = string
  default     = "t4g.small"
}

variable "use_spot" {
  description = <<-EOT
    Spot pricing, roughly 60-70% off on-demand. The request is one-time, so an
    interruption terminates the instance and the next `terraform apply` builds a
    fresh one -- fine for a test cluster that holds no state, and the reason this
    config keeps nothing on the instance that a rebuild cannot recreate.
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
    CIDR allowed to reach SSH, the dashboard, and the Kubernetes API. Leave null
    to auto-detect your current public IP as a /32. Never widen this to
    0.0.0.0/0 -- the dashboard shows pod names, images, events, and container
    log tails.
  EOT
  type        = string
  default     = null

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

variable "admin_email" {
  description = "Sign-in identity for the dashboard. Any address; no mail is ever sent (notifications are off)."
  type        = string
  default     = "admin@example.com"
}

variable "cluster_name" {
  description = "Shown in the dashboard header. Cosmetic."
  type        = string
  default     = "kdb-test-k3s"
}

variable "image_tag" {
  description = "Tag for the image built on the instance. Must match what the Helm values reference."
  type        = string
  default     = "0.3.0"
}

variable "k3s_version" {
  description = "Pin a k3s version (e.g. v1.30.6+k3s1). Empty installs the current stable channel."
  type        = string
  default     = ""
}

variable "scan_interval_seconds" {
  description = "Cluster scan interval. 120 rather than the 300 default so a test cluster reacts while you watch it."
  type        = number
  default     = 120
}

variable "agent_interval_seconds" {
  description = "Node-agent reporting interval, for the same reason."
  type        = number
  default     = 60
}

variable "project_source_dir" {
  description = "Path to the k8s-defect-bot source that gets built on the instance. Defaults to the parent directory."
  type        = string
  default     = null
}

variable "deploy_bot" {
  description = <<-EOT
    Build the image and install the Helm chart as part of apply. Set false to get
    a bare k3s cluster and follow usage.md by hand instead -- which is a good way
    to check that runbook end to end.
  EOT
  type        = bool
  default     = true
}
