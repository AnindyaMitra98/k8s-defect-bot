output "public_ip" {
  description = "Public IPv4 of the k3s node."
  value       = aws_instance.node.public_ip
}

output "allowed_cidr" {
  description = "The only CIDR that can reach SSH, the dashboard, and the Kubernetes API."
  value       = local.allowed_cidr
}

output "dashboard_url" {
  description = "Where the dashboard will be once you install the bot. Nothing serves this until then."
  value       = "http://${aws_instance.node.public_ip}.nip.io"
}

output "ssh_command" {
  description = "Shell on the node. On Windows, fix the key ACL first -- see fix_key_permissions."
  value       = "ssh -i ${local_sensitive_file.ssh_key.filename} ubuntu@${aws_instance.node.public_ip}"
}

output "fix_key_permissions" {
  description = "Run this once, in PowerShell, before the first ssh. Terraform's file_permission is a no-op on NTFS."
  # $${env:USERNAME} renders as ${env:USERNAME}: the doubled $ escapes Terraform's
  # own interpolation. The braces then matter to PowerShell -- unbraced,
  # $env:USERNAME:R parses as the variable "USERNAME:R" in the env: drive, which
  # does not exist, so icacls receives an empty principal and silently grants
  # nothing. The key stays unreadable and ssh keeps refusing it.
  value = "icacls ${replace(local_sensitive_file.ssh_key.filename, "/", "\\")} /inheritance:r /grant:r \"$${env:USERNAME}:R\""
}

output "kubeconfig_command" {
  description = "Fetch a kubeconfig that works from Windows. PowerShell syntax -- there is no sed here."
  value = var.expose_kubernetes_api ? join("\n", [
    "scp -i ${local_sensitive_file.ssh_key.filename} ubuntu@${aws_instance.node.public_ip}:/etc/rancher/k3s/k3s.yaml .\\kubeconfig.yaml",
    "(Get-Content .\\kubeconfig.yaml) -replace '127.0.0.1','${aws_instance.node.public_ip}' | Set-Content -Encoding utf8 .\\kubeconfig.yaml",
    "$env:KUBECONFIG = \"$PWD\\kubeconfig.yaml\"",
    "kubectl get nodes",
  ]) : "expose_kubernetes_api is false -- use ssh_command and run kubectl on the node instead"
}

output "install_commands" {
  description = "The manual install. Terraform does not run this; SSH in and do it yourself."
  value       = <<-EOT
    SSH in (see ssh_command), confirm the bootstrap finished, then:

      test -f /opt/bootstrap.done || tail -50 /var/log/cloud-init-output.log
      kubectl get nodes                      # expect one node, Ready

      cd ~/k8s-defect-bot && git pull

      sudo docker build -t k8s-defect-bot:0.3.0 .

      # Straight into k3s's containerd namespace -- no registry, no pull.
      sudo docker save k8s-defect-bot:0.3.0 -o /tmp/kdb.tar
      sudo k3s ctr images import /tmp/kdb.tar
      sudo rm -f /tmp/kdb.tar                # sudo: docker save wrote it as root,
                                             # and /tmp is sticky, so a plain rm
                                             # fails with "Operation not permitted"

      helm upgrade --install k8s-defect-bot ./helm/k8s-defect-bot \
        --namespace k8s-defect-bot --create-namespace \
        -f ~/values-test.yaml \
        --set ingress.host=${aws_instance.node.public_ip}.nip.io \
        --set config.dashboardUrl=http://${aws_instance.node.public_ip}.nip.io \
        --wait --timeout 10m

    values-test.yaml lives in this Terraform directory; copy it up with:

      scp -i ${local_sensitive_file.ssh_key.filename} values-test.yaml ubuntu@${aws_instance.node.public_ip}:~/

    Then read the generated admin password and sign in at
    http://${aws_instance.node.public_ip}.nip.io :

      kubectl -n k8s-defect-bot get secret k8s-defect-bot-users \
        -o jsonpath='{.data.generated-password}' | base64 -d; echo
  EOT
}

output "estimated_cost" {
  description = "Rough running cost. Destroy when you are done; nothing here holds state worth keeping."
  value = {
    instance    = "${var.instance_type} ${var.use_spot ? "spot" : "on-demand"} in ${var.region} -- t4g.small in us-east-1 is ~$0.007/hr spot, $0.0168/hr on-demand; verify current pricing for yours"
    public_ipv4 = "~$0.005/hr (~$3.60/mo) -- billed for every public IPv4 address, running or not"
    ebs         = "${var.root_volume_gb} GiB gp3 at ~$0.08/GiB/mo ${format("(~$%.2f/mo)", var.root_volume_gb * 0.08)}"
    nat_gateway = "none -- deliberately omitted; it alone would be ~$32/mo"
    total       = "~$0.015/hr, so ~$0.12 for an eight-hour session and ~$11/mo if left running"
    note        = "Stopping the instance still bills the EBS volume. `terraform destroy` bills nothing."
  }
}

output "next_steps" {
  description = "What to do once apply finishes."
  value       = <<-EOT
    This is a BARE cluster. Terraform installed k3s and cloned the source; it did
    not build or install the bot. That is the manual step you are here to test.

    1. terraform output fix_key_permissions   # run it, once, in PowerShell
    2. terraform output -raw install_commands # the runbook, with your IP filled in
    3. terraform output -raw kubeconfig_command

    Once the bot is up, break something on purpose and watch it land:

      kubectl create deployment crasher  --image=busybox -- /bin/false
      kubectl create deployment badimage --image=nginx:doesnotexist

    Those produce crashloopbackoff and imagepullbackoff findings within one
    120s scan. /api/nodes fills in after the agent's first 60s report.

    4. terraform destroy   # when you are done. It costs money while it exists.
  EOT
}
