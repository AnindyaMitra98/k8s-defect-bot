output "dashboard_url" {
  description = "The bot's dashboard. Sign in with admin_email / admin_password."
  value       = var.deploy_bot ? "http://${local.dashboard_host}" : null
}

output "admin_email" {
  description = "Dashboard sign-in identity."
  value       = var.admin_email
}

output "admin_password" {
  description = "Dashboard password. Read it with: terraform output -raw admin_password"
  value       = random_password.admin.result
  sensitive   = true
}

output "public_ip" {
  description = "Public IPv4 of the k3s node."
  value       = aws_instance.node.public_ip
}

output "ssh_command" {
  description = "Shell on the node. The private key is written into this directory by apply."
  value       = "ssh -i ${local_sensitive_file.ssh_key.filename} ubuntu@${aws_instance.node.public_ip}"
}

output "kubeconfig_command" {
  description = "Fetch a kubeconfig that works from your workstation (needs expose_kubernetes_api = true)."
  value = var.expose_kubernetes_api ? join(" ", [
    "scp -i ${local_sensitive_file.ssh_key.filename}",
    "ubuntu@${aws_instance.node.public_ip}:/etc/rancher/k3s/k3s.yaml ./kubeconfig.yaml",
    "&& sed -i 's/127.0.0.1/${aws_instance.node.public_ip}/' ./kubeconfig.yaml",
    "&& export KUBECONFIG=$PWD/kubeconfig.yaml",
  ]) : "expose_kubernetes_api is false -- use the ssh_command and run kubectl on the node"
}

output "allowed_cidr" {
  description = "The only CIDR that can reach SSH, the dashboard, and the API."
  value       = local.allowed_cidr
}

output "estimated_cost" {
  description = "Rough running cost. Destroy when you are done; nothing here holds state worth keeping."
  value = {
    instance    = "${var.instance_type}${var.use_spot ? " (spot, ~$0.007/hr for t4g.small in us-east-1)" : " (on-demand, $0.0168/hr for t4g.small)"} -- verify current pricing in ${var.region}"
    public_ipv4 = "~$0.005/hr (~$3.60/mo) -- charged for every public IPv4 address, running or not"
    ebs         = "${var.root_volume_gb} GiB gp3 -- ~$0.08/GiB/mo"
    nat_gateway = "none -- deliberately omitted, it would cost more than everything else combined"
    note        = "Stopping the instance still bills the EBS volume and the IP. `terraform destroy` bills nothing."
  }
}

output "next_steps" {
  description = "What to do once apply finishes."
  value       = <<-EOT
    1. Open the dashboard:  terraform output dashboard_url
       Password:            terraform output -raw admin_password

    2. Give it two minutes. The first scan is immediate, but the node agent
       reports on its own interval (${var.agent_interval_seconds}s) -- /api/nodes
       stays empty until the first report lands.

    3. Break something on purpose and watch it show up:

         kubectl create deployment crasher --image=busybox -- /bin/false
         kubectl create deployment badimage --image=nginx:doesnotexist
         kubectl run hog --image=polinux/stress --restart=Never --limits=memory=32Mi -- \
           stress --vm 1 --vm-bytes 128M --vm-hang 0

       Those produce crashloopbackoff, imagepullbackoff, and oomkilled findings.

    4. Destroy it when you are done:  terraform destroy
  EOT
}
