# Builds the image on the node and installs the chart.
#
# All of it runs on the instance rather than through the Helm/Kubernetes
# providers, for two reasons: those providers need a reachable cluster at plan
# time, which does not exist on the first apply; and building on the node means
# the image is natively the right architecture with no registry involved.

locals {
  project_source_dir = coalesce(var.project_source_dir, "${path.module}/..")
  dashboard_host     = "${aws_instance.node.public_ip}.nip.io"

  helm_values = templatefile("${path.module}/templates/values.yaml.tftpl", {
    image_tag      = var.image_tag
    cluster_name   = var.cluster_name
    dashboard_host = local.dashboard_host
    scan_interval  = var.scan_interval_seconds
    agent_interval = var.agent_interval_seconds
    admin_email    = var.admin_email
    admin_password = random_password.admin.result
  })
}

# Alphanumeric only, so it needs no escaping in YAML, JSON, or a shell.
resource "random_password" "admin" {
  length  = 24
  special = false
}

data "archive_file" "source" {
  count = var.deploy_bot ? 1 : 0

  type        = "zip"
  source_dir  = local.project_source_dir
  output_path = "${path.module}/.build/k8s-defect-bot-src.zip"

  excludes = [
    ".venv/**",
    ".git/**",
    "terraform/**",
    ".pytest_cache/**",
    "**/__pycache__/**",
    "**/*.pyc",
  ]
}

resource "terraform_data" "deploy" {
  count = var.deploy_bot ? 1 : 0

  # Re-runs when the node is replaced, the source changes, or the values change.
  triggers_replace = {
    instance = aws_instance.node.id
    source   = data.archive_file.source[0].output_base64sha256
    values   = sha256(local.helm_values)
  }

  connection {
    type        = "ssh"
    host        = aws_instance.node.public_ip
    user        = "ubuntu"
    private_key = tls_private_key.ssh.private_key_pem
    timeout     = "10m"
  }

  provisioner "remote-exec" {
    inline = [
      "cloud-init status --wait > /dev/null 2>&1 || true",
      "test -f /opt/bootstrap.done || { echo 'bootstrap did not finish; see /var/log/cloud-init-output.log'; exit 1; }",
    ]
  }

  provisioner "file" {
    source      = data.archive_file.source[0].output_path
    destination = "/home/ubuntu/src.zip"
  }

  provisioner "file" {
    content     = local.helm_values
    destination = "/home/ubuntu/values.yaml"
  }

  provisioner "remote-exec" {
    inline = [
      "set -e",
      "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml",

      "rm -rf /home/ubuntu/src && mkdir -p /home/ubuntu/src",
      "unzip -q -o /home/ubuntu/src.zip -d /home/ubuntu/src",

      "cd /home/ubuntu/src && sudo docker build -t k8s-defect-bot:${var.image_tag} .",

      # Straight into k3s's containerd namespace -- no registry, no pull.
      "sudo docker save k8s-defect-bot:${var.image_tag} -o /tmp/kdb.tar",
      "sudo k3s ctr images import /tmp/kdb.tar",
      "rm -f /tmp/kdb.tar",

      "cd /home/ubuntu/src && helm upgrade --install k8s-defect-bot ./helm/k8s-defect-bot --namespace k8s-defect-bot --create-namespace -f /home/ubuntu/values.yaml --wait --timeout 10m",

      "kubectl -n k8s-defect-bot get pods -o wide",
      "kubectl -n k8s-defect-bot get ingress",
    ]
  }
}
