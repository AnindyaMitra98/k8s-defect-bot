# Test environment for k8s-defect-bot

One EC2 spot instance running k3s, on the cheapest infrastructure that can carry
a real cluster. **Terraform provisions the cluster and stops there** — building
the image and installing the chart is a manual step you run over SSH.

That split is deliberate. The previous version of this config drove the install
through a `remote-exec` provisioner and died mid-apply on a one-line permissions
bug, after the build had already succeeded — leaving infrastructure up, the app
half-installed, and Terraform reporting failure. There are now no provisioners
here at all. The node bootstraps itself from cloud-init (`user_data`), which is
a native EC2 mechanism: it runs on the instance whether or not Terraform is
still watching, and it cannot fail an apply.

The manual install is also the point. Running it by hand is how you find out
whether [`../usage.md`](../usage.md) is still correct.

**To run it, follow [usage.md](usage.md)** — a step-by-step walkthrough from
`terraform apply` through installing the bot, breaking things on purpose to
confirm it detects them, and tearing the whole thing down. This file is what the
environment *is* and why it's built this way.

---

## What it creates

```
VPC 10.0.0.0/16
└── public subnet ──── internet gateway        (no NAT gateway, on purpose)
    └── 1x EC2 spot instance (t4g.small, ARM)
        ├── k3s server + agent, single node, Traefik bundled
        ├── docker, helm, git      (installed by cloud-init)
        └── ~/k8s-defect-bot       (cloned, NOT built, NOT installed)
```

The security group allows SSH, HTTP, and the Kubernetes API **from your IP
only**, auto-detected at plan time. The dashboard shows pod names, images,
events, and container log tails over plain HTTP, so it is not something to leave
open — `allowed_cidr` refuses `0.0.0.0/0` outright.

## Cost

| Item | Roughly |
|---|---|
| t4g.small **spot** | ~$0.007/hr → ~$5.10/mo (us-east-1; on-demand is $0.0168) |
| Public IPv4 address | $0.005/hr → ~$3.60/mo (billed whether or not the instance runs) |
| 20 GiB gp3 root volume | ~$1.60/mo |
| VPC, IGW, security groups, data transfer at this scale | $0 |
| NAT gateway | **not created** — it alone would be ~$32/mo |
| **Total, left running** | **~$0.015/hr** → ~$0.12 for a working session, ~$11/mo if forgotten |

There is no auto-shutdown and no budget alarm. `terraform destroy` is the
discipline; destroyed, this costs nothing.

Spot price varies by AZ, and the subnet takes the first AZ in the region.
Set `use_spot = false` if you would rather not be interrupted — an interrupted
node is terminated, and the next `terraform apply` builds a fresh one.

## Prerequisites

- Terraform >= 1.6, AWS credentials with EC2/VPC permissions
- An SSH client (`ssh`, `scp`) — Windows 10+ ships OpenSSH
- `kubectl` on your workstation, if you want to drive the cluster from there

## Bring it up

```powershell
cd terraform
terraform init
terraform plan          # check allowed_cidr resolved to YOUR /32
terraform apply
```

Apply takes about a minute; cloud-init needs another three or four after that
before the node is Ready.

### Windows: fix the key ACL first

Terraform writes the private key to `.ssh/kdb-test.pem` with `file_permission =
"0600"`, which is a **no-op on NTFS**. OpenSSH will refuse it as
`UNPROTECTED PRIVATE KEY FILE` until you fix the ACL. Once, in PowerShell:

```powershell
terraform output fix_key_permissions    # prints the exact command
icacls .\.ssh\kdb-test.pem /inheritance:r /grant:r "$env:USERNAME:R"
```

## Install the bot

```powershell
terraform output -raw install_commands   # the full runbook, your IP filled in
```

Copy the values file up, then SSH in:

```powershell
scp -i .\.ssh\kdb-test.pem values-test.yaml ubuntu@<public-ip>:~/
ssh -i .\.ssh\kdb-test.pem ubuntu@<public-ip>
```

On the node — confirm the bootstrap finished before anything else:

```bash
test -f /opt/bootstrap.done || tail -50 /var/log/cloud-init-output.log
kubectl get nodes                      # one node, Ready

cd ~/k8s-defect-bot && git pull
sudo docker build -t k8s-defect-bot:0.3.0 .

# Straight into k3s's containerd namespace -- no registry, no pull.
sudo docker save k8s-defect-bot:0.3.0 -o /tmp/kdb.tar
sudo k3s ctr images import /tmp/kdb.tar
sudo rm -f /tmp/kdb.tar

helm upgrade --install k8s-defect-bot ./helm/k8s-defect-bot \
  --namespace k8s-defect-bot --create-namespace \
  -f ~/values-test.yaml \
  --set ingress.host=<public-ip>.nip.io \
  --set config.dashboardUrl=http://<public-ip>.nip.io \
  --wait --timeout 10m
```

That `sudo rm` is the bug that broke the old config: `docker save` ran under
`sudo`, so the tar belongs to root, and `/tmp` carries the sticky bit — a plain
`rm` fails with `Operation not permitted`.

The build takes roughly two minutes on a t4g.small. It produces an **arm64**
image, which is correct because it never leaves this node.

Then read the generated admin password and sign in at `http://<public-ip>.nip.io`:

```bash
kubectl -n k8s-defect-bot get secret k8s-defect-bot-users \
  -o jsonpath='{.data.generated-password}' | base64 -d; echo
```

`values-test.yaml` leaves `auth.users` empty on purpose, so the chart generates
that password and reuses it across upgrades — better than putting one on a
command line where it lands in shell history.

## Drive it from Windows

```powershell
terraform output -raw kubeconfig_command    # prints all four lines

scp -i .\.ssh\kdb-test.pem ubuntu@<ip>:/etc/rancher/k3s/k3s.yaml .\kubeconfig.yaml
(Get-Content .\kubeconfig.yaml) -replace '127.0.0.1','<ip>' | Set-Content -Encoding utf8 .\kubeconfig.yaml
$env:KUBECONFIG = "$PWD\kubeconfig.yaml"
kubectl get nodes
```

k3s puts the instance's public IP in the API server certificate as a SAN, so
this works without `--insecure-skip-tls-verify`.

## Give it something to find

```bash
kubectl create namespace defect-test
kubectl -n defect-test create deployment crasher  --image=busybox -- /bin/false
kubectl -n defect-test create deployment badimage --image=nginx:v-does-not-exist
```

`crashloopbackoff` and `imagepullbackoff` show up within one 120s scan.
`/api/nodes` fills in after the node agent's first 60s report.

[usage.md](usage.md#part-6--create-fake-issues) has a manifest that exercises
every rule that can be forced — including the ones with five-minute grace
periods — plus a safe way to make the node-agent checks fire without filling the
disk. Clean up with `kubectl delete namespace defect-test`.

## Iterating on the bot's code

The node builds **what is pushed to the branch**, not your local working tree.
Push first, then either re-run the install block above (fast), or replace the
node entirely:

```powershell
terraform apply -replace=aws_instance.node
```

`user_data_replace_on_change` means any edit to the cloud-init template rebuilds
the node anyway.

## Tear it down

```powershell
terraform destroy
```

Nothing here holds state worth keeping. The EBS volume has
`delete_on_termination`, so destroy leaves no volume and no public IP behind —
and therefore no bill.

## Notable settings in `values-test.yaml`

These are the ones that fail *quietly* on k3s if you get them wrong:

| Setting | Why |
|---|---|
| `nodeAgent.containerRuntimeSocket: /run/k3s/containerd/containerd.sock` | The chart default is a kubeadm path. On k3s, `node_container_runtime` reports critical on every node — which looks like a real defect and is not. |
| `auth.sessionCookieSecure: false` | Over plain HTTP, `true` means the browser accepts the session cookie and then never sends it back: sign-in appears to succeed, every request after it is unauthenticated. |
| `networkPolicy.enabled: false` | k3s ships Flannel, which accepts NetworkPolicy objects and enforces none of them. Enabling it would look like protection and be none. The security group is the real control. |
| `image.pullPolicy: IfNotPresent` | The image is imported into containerd directly. There is no registry, so `Always` would fail every pull. |

## Files

| File | |
|---|---|
| `versions.tf` | Provider constraints, region, default tags |
| `variables.tf` | Every input, all defaulted |
| `network.tf` | VPC, subnet, IGW, routes, security group, IP auto-detection |
| `compute.tf` | AMI lookup, SSH key, the instance |
| `templates/cloud-init.yaml.tftpl` | Node bootstrap: k3s, docker, helm, git clone |
| `values-test.yaml` | Helm values for the manual install; Terraform never reads it |
| `outputs.tf` | Connection commands, the install runbook, cost estimate |
