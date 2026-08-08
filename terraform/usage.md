# Running the k8s-defect-bot test cluster

A runbook for the Terraform in this directory. Work through **Part 1** and you
have a real Kubernetes cluster running the bot, for about **1.5 cents an hour**.

This is a disposable test environment, not a deployment guide. To deploy the bot
somewhere that matters, use [../usage.md](../usage.md) instead — this cluster
deliberately makes choices that runbook tells you not to make.

**Time:** about 12 minutes, most of it `pip install` inside `docker build`.

---

## Contents

**Part 1 — First run**
- [Before you start](#before-you-start)
- [Step 1 — Configure](#step-1--configure)
- [Step 2 — Plan](#step-2--plan)
- [Step 3 — Apply](#step-3--apply)
- [Step 4 — Verify](#step-4--verify)
- [Step 5 — Make it find something](#step-5--make-it-find-something)

**Part 2 — Working with it**
- [Re-deploying after a code change](#re-deploying-after-a-code-change)
- [Getting a shell and a kubectl](#getting-a-shell-and-a-kubectl)
- [Changing what gets installed](#changing-what-gets-installed)
- [Turning on the optional parts](#turning-on-the-optional-parts)

**Part 3 — Cost and teardown**
- [Keeping the bill near zero](#keeping-the-bill-near-zero)
- [Destroy](#destroy)

**Part 4 — Reference**
- [What gets created](#what-gets-created)
- [Variables](#variables)
- [Outputs](#outputs)

**Part 5 — When something is wrong**
- [Troubleshooting](#troubleshooting)
- [What this environment does not test](#what-this-environment-does-not-test)

---

# Part 1 — First run

## Before you start

```bash
terraform version      # >= 1.6
aws sts get-caller-identity
ssh -V                 # any OpenSSH client; Git Bash ships one on Windows
```

Terraform needs **EC2 and VPC permissions only** — it creates no IAM roles, no
instance profile, no S3 bucket, and no ECR repository. `AmazonEC2FullAccess` is
more than enough; a scoped policy needs `ec2:*` on VPCs, subnets, internet
gateways, route tables, security groups, key pairs, instances, and volumes.

You do **not** need Docker, Helm, or kubectl locally. All three run on the
instance. kubectl is useful afterwards but nothing here requires it.

> **This costs real money from the moment you apply.** Not much — around
> $0.015/hour, roughly $10/month if you forget it — but it is not free tier.
> [Destroy it](#destroy) when you are done.

## Step 1 — Configure

Every variable has a working default, so this step is optional:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # then edit, or don't
```

The one worth thinking about is `allowed_cidr`. Left unset, Terraform detects
your current public IP at plan time and locks SSH, the dashboard, and the
Kubernetes API to that single address. Set it explicitly if you are behind a
corporate NAT or a changing address:

```hcl
allowed_cidr = "203.0.113.42/32"
```

The configuration refuses `0.0.0.0/0`. The dashboard shows pod names, images,
events, and container log tails to anyone who reaches it.

## Step 2 — Plan

```bash
terraform init
terraform plan
```

Read three things in the output before going further:

| Look for | Should be |
|---|---|
| `Plan: 16 to add` | 16 — anything else means a variable changed the shape |
| `allowed_cidr = "..."` | **Your** public IP, as a `/32` |
| `instance_type` / `market_type` | `t4g.small` / `spot` unless you changed them |

If `allowed_cidr` is not your address, whatever you apply will be unreachable.

## Step 3 — Apply

```bash
terraform apply
```

Roughly what happens, and when:

| Phase | Takes | What it is |
|---|---|---|
| VPC, subnet, security group, key pair | ~20s | Plain AWS API calls |
| Spot instance reaches running | ~1 min | Fails fast if there is no spot capacity |
| `cloud-init status --wait` | ~3-4 min | apt, Docker, k3s, Helm installing on the node |
| Source upload | ~5s | 148 KB zip over SSH |
| `docker build` | ~5-7 min | Mostly `pip install`, on two ARM cores |
| `helm upgrade --install --wait` | ~1 min | Blocks until the collector passes readiness |

The apply looks stalled during `cloud-init status --wait` and during the build.
It isn't — those two steps are most of the runtime. Terraform prints the
remote-exec output as it goes, ending with the pod list and the ingress.

## Step 4 — Verify

```bash
terraform output dashboard_url
terraform output -raw admin_password
```

Open that URL and sign in with `admin@example.com` (or whatever you set
`admin_email` to) and that password.

Then check the parts that can fail quietly:

```bash
eval "$(terraform output -raw kubeconfig_command)"

# 1. Both workloads are up: one collector, one agent
kubectl -n k8s-defect-bot get pods -o wide

# 2. The collector started clean
kubectl -n k8s-defect-bot logs deploy/k8s-defect-bot | head -30

# 3. The node agent is actually reporting -- this is the one that silently fails.
#    The API takes a session cookie or a bearer token, not basic auth, so sign in first.
URL=$(terraform output -raw dashboard_url)
curl -s -c /tmp/kdb.jar -o /dev/null \
  -d "email=admin@example.com&password=$(terraform output -raw admin_password)" "$URL/login"
curl -s -b /tmp/kdb.jar "$URL/api/nodes"
```

`/api/nodes` should contain exactly one node within 60 seconds of the agent
starting. An empty list means the agent is running but its reports are not
landing — see [Troubleshooting](#troubleshooting).

In the log you want to see, in order:

```
starting k8s-defect-bot on cluster 'kdb-test-k3s' (interval=120s, ...)
loaded 1 user(s): admin@example.com
authentication enabled for 1 user(s)
scan complete in 0.9s: N defects (...) across M pods / 1 nodes
```

A `WARNING` about a plaintext password is expected here — the values pass one
deliberately, and the app hashes it at load. That is a test-environment choice,
not a bug.

## Step 5 — Make it find something

A fresh k3s cluster is mostly healthy, so the dashboard starts nearly empty.
Break things on purpose:

```bash
kubectl create deployment crasher  --image=busybox -- /bin/false
kubectl create deployment badimage --image=nginx:doesnotexist
kubectl run hog --image=polinux/stress --restart=Never \
  --limits=memory=32Mi -- stress --vm 1 --vm-bytes 128M --vm-hang 0
```

| What you did | Shows up as | After |
|---|---|---|
| `busybox /bin/false` | `crashloopbackoff` | ~1-2 min (needs a few restarts) |
| `nginx:doesnotexist` | `imagepullbackoff` | one scan (120s) |
| stress past its memory limit | `oomkilled` | one scan after the kill |
| Any of the above | `missing_resource_limits`, `warning_events` | one scan |

To see the node-agent half rather than the cluster-scan half, fill the disk:

```bash
kubectl -n k8s-defect-bot exec deploy/k8s-defect-bot -- df -h /   # baseline
ssh -i .ssh/kdb-test.pem ubuntu@"$(terraform output -raw public_ip)" \
  'fallocate -l 14G /tmp/ballast'
```

Within one agent interval (60s) that becomes a `node_disk_usage` finding with
the real percentage. `rm /tmp/ballast` clears it on the next cycle. Watch the
node's actual free space — leave the instance room to run.

Clean up the deliberate breakage:

```bash
kubectl delete deployment crasher badimage; kubectl delete pod hog
```

---

# Part 2 — Working with it

## Re-deploying after a code change

This is the loop you'll use most. Edit the bot's source in the parent directory,
then:

```bash
terraform apply
```

The archive's hash is a replace trigger on `terraform_data.deploy`, so any
change to the source re-uploads, rebuilds the image, and re-runs
`helm upgrade --install`. The node itself is untouched — no re-provisioning, no
new instance. A rebuild is 5-7 minutes, nearly all `docker build`.

Nothing changed? Terraform says `No changes` and does nothing, which is the
correct answer.

To skip Terraform entirely for a fast iteration, do it on the node:

```bash
ssh -i .ssh/kdb-test.pem ubuntu@<ip>
cd ~/src && sudo docker build -t k8s-defect-bot:0.3.0 . \
  && sudo docker save k8s-defect-bot:0.3.0 -o /tmp/k.tar \
  && sudo k3s ctr images import /tmp/k.tar \
  && kubectl -n k8s-defect-bot rollout restart deploy/k8s-defect-bot
```

Just remember the next `terraform apply` overwrites `~/src` from your local copy.

## Getting a shell and a kubectl

```bash
eval "$(terraform output -raw ssh_command)"          # shell on the node
eval "$(terraform output -raw kubeconfig_command)"   # kubectl from your machine
```

The kubeconfig command copies k3s's config down and rewrites `127.0.0.1` to the
public IP, which works because the instance installs k3s with `--tls-san` set to
that address. On the node itself, `kubectl` and `helm` are already configured.

Useful once you have either:

```bash
kubectl -n k8s-defect-bot logs deploy/k8s-defect-bot -f
kubectl -n k8s-defect-bot logs -l app.kubernetes.io/component=node-agent --tail=30
kubectl -n k8s-defect-bot get ingress
helm -n k8s-defect-bot get values k8s-defect-bot     # on the node
```

## Changing what gets installed

The Helm values live in `templates/values.yaml.tftpl`, rendered by Terraform.
Edit that file and `terraform apply` — the rendered content is a replace trigger,
so it reinstalls the chart without rebuilding anything else.

That is the right place to change scan intervals, thresholds, enabled rules, or
anything else in the chart. Do not edit values on the node; the next apply
overwrites them.

## Turning on the optional parts

**The kernel-log check** (`node_kernel_errors`) reads `/dev/kmsg` and needs the
agent to run as root. In `templates/values.yaml.tftpl`:

```yaml
nodeAgent:
  privileged: true
  enabledChecks:
    - node_disk_usage
    # ... the other nine defaults ...
    - node_kernel_errors
```

**Email notifications** work from here — the node has outbound internet through
the internet gateway. Add your relay to the values and create the SMTP Secret on
the node by hand. Port 25 outbound is blocked by AWS by default; use 587.

**Claude enrichment** also works, since this cluster has egress. Set
`llm.provider: claude_cli` and you will also need the `Dockerfile.claude` image
variant and a token — see [../usage.md](../usage.md#option-b--claude-in-cluster).
For a test cluster, running the collector on your own machine against this
cluster's kubeconfig is easier and needs none of it.

---

# Part 3 — Cost and teardown

## Keeping the bill near zero

| Action | Instance | EBS volume | Public IP | Total |
|---|---|---|---|---|
| Running | ~$0.007/hr | ~$0.002/hr | $0.005/hr | **~$0.015/hr** |
| `aws ec2 stop-instances` | $0 | ~$0.002/hr | $0.005/hr | **~$0.007/hr** |
| `terraform destroy` | $0 | $0 | $0 | **$0** |

Stopping saves less than half, and a **spot** instance cannot be restarted after
stopping with a one-time request anyway. Destroy is the right verb here — this
cluster holds nothing that a 12-minute apply cannot recreate.

## Destroy

```bash
terraform destroy
```

Everything goes: instance, volume, VPC, key pair. What stays behind, locally:

| File | What to do |
|---|---|
| `.ssh/kdb-test.pem` | Useless now; the key pair is gone. Delete it |
| `.build/*.zip` | Regenerated on the next plan |
| `terraform.tfstate` | Keep it — it is how Terraform knows there is nothing left |
| `kubeconfig.yaml` | Points at a dead IP. Delete it |

Confirm nothing survived:

```bash
aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=k8s-defect-bot" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text
```

Every resource carries `Project=k8s-defect-bot` and `ManagedBy=terraform` tags,
so that query is a reliable check.

---

# Part 4 — Reference

## What gets created

16 resources:

| Resource | Count | Notes |
|---|---|---|
| `aws_vpc.this` | 1 | 10.0.0.0/16 |
| `aws_subnet.public` | 1 | 10.0.1.0/24, first AZ in the region |
| `aws_internet_gateway.this` | 1 | Free; the reason there is no NAT gateway |
| `aws_route_table.public` + association | 2 | Default route to the IGW |
| `aws_security_group.node` | 1 | Plus 3 ingress rules and 1 egress rule |
| `aws_vpc_security_group_ingress_rule.*` | 3 | 22, 80, and 6443 — each from `allowed_cidr` only |
| `aws_vpc_security_group_egress_rule.all` | 1 | Outbound for apt, k3s, Helm, image pulls |
| `tls_private_key.ssh` + `aws_key_pair.this` | 2 | Generated per environment, never reused |
| `local_sensitive_file.ssh_key` | 1 | Writes `.ssh/<prefix>.pem` locally, mode 0600 |
| `aws_instance.node` | 1 | The whole cluster |
| `random_password.admin` | 1 | 24 alphanumeric characters |
| `terraform_data.deploy[0]` | 1 | Build + install; absent when `deploy_bot = false` |

The Kubernetes API rule (`kubernetes_api[0]`) disappears when
`expose_kubernetes_api = false`, taking the count to 15.

## Variables

| Variable | Default | Why change it |
|---|---|---|
| `region` | `us-east-1` | Latency, or cheaper spot elsewhere |
| `name_prefix` | `kdb-test` | Running two of these at once |
| `instance_type` | `t4g.small` | `t4g.micro` is cheaper but too small for the build; `t3.small` for x86 |
| `use_spot` | `true` | `false` if an interruption mid-test would annoy you |
| `root_volume_gb` | `20` | Only if you plan to fill the disk on purpose |
| `allowed_cidr` | auto-detect | Corporate NAT, or a changing address |
| `expose_kubernetes_api` | `true` | `false` keeps 6443 shut; use SSH instead |
| `admin_email` | `admin@example.com` | Cosmetic — no mail is sent |
| `cluster_name` | `kdb-test-k3s` | Shown in the dashboard header |
| `image_tag` | `0.3.0` | Match the chart's expectation |
| `k3s_version` | `""` (stable) | Pin for reproducibility |
| `scan_interval_seconds` | `120` | Faster feedback than the 300 default |
| `agent_interval_seconds` | `60` | Same |
| `project_source_dir` | `../` | Building a copy of the source from elsewhere |
| `deploy_bot` | `true` | `false` gives bare k3s, so you can walk ../usage.md by hand |

## Outputs

| Output | Use |
|---|---|
| `dashboard_url` | The dashboard, via nip.io |
| `admin_email` / `admin_password` | Sign-in. Password is sensitive: `terraform output -raw admin_password` |
| `public_ip` | The node |
| `ssh_command` | Ready-to-run, with the right key path |
| `kubeconfig_command` | Fetches and rewrites k3s's kubeconfig |
| `allowed_cidr` | What the security group actually allows — check this when locked out |
| `estimated_cost` | The breakdown, so it is visible rather than a surprise |
| `next_steps` | A short version of Steps 4 and 5 above |

---

# Part 5 — When something is wrong

## Troubleshooting

### `InsufficientInstanceCapacity` or `SpotMaxPriceTooLow`

No spot capacity for that type in that AZ right now. In order of effort:

```hcl
instance_type = "t4g.medium"   # different pool
region        = "us-east-2"    # different region
use_spot      = false          # on-demand, $0.0168/hr for t4g.small
```

### Apply hangs at `remote-exec` for more than ~6 minutes

cloud-init is still working, or it failed. It waits for `/opt/bootstrap.done`:

```bash
ssh -i .ssh/kdb-test.pem ubuntu@<ip> 'sudo tail -50 /var/log/cloud-init-output.log'
```

### `docker build` is killed, or the node freezes during apply

Out of memory. `t4g.micro` has 1 GiB, which k3s alone largely occupies. Use
`t4g.small` — the default — or build the image somewhere else.

### Timeout connecting over SSH, or the dashboard does not load

Your public IP changed since `terraform plan` detected it:

```bash
terraform output allowed_cidr        # what the security group allows
curl -s https://checkip.amazonaws.com # what you are now
```

`terraform apply` updates the rule; it does not touch the instance.

### Sign-in succeeds, then bounces back to the login page

`auth.sessionCookieSecure` got set to `true` while serving plain HTTP, so the
browser accepts the cookie and refuses to send it back. The values here set it
`false` deliberately; do not "fix" it without adding TLS.

### The dashboard 404s or the ingress has no address

```bash
kubectl -n k8s-defect-bot get ingress
kubectl -n kube-system get pods -l app.kubernetes.io/name=traefik
```

k3s installs Traefik a minute or two after first boot. If it never appears, k3s
was installed with `--disable=traefik` — it is not, in this configuration.

### `/api/nodes` is empty

Give it one agent interval (60s) first. Then:

```bash
kubectl -n k8s-defect-bot logs -l app.kubernetes.io/component=node-agent --tail=30
```

- `collector rejected the report (HTTP 403)` → collector and agent hold different
  tokens; `helm uninstall` and re-apply.
- `could not reach collector` → NetworkPolicy, which this configuration disables.
  If you enabled it, k3s's Flannel does not enforce it anyway — see
  [../usage.md](../usage.md#step-4--write-your-values-file).

### Every node check reports `node_container_runtime` critical

The socket path is wrong. k3s uses `/run/k3s/containerd/containerd.sock`, which
`templates/values.yaml.tftpl` already sets — this only appears if you changed it.

### `ImagePullBackOff` on the collector or agent

The build or import step did not take effect. On the node:

```bash
sudo k3s ctr images ls | grep k8s-defect-bot
```

Empty means `k3s ctr images import` did not run. Re-run `terraform apply`, or do
the build by hand — see [Re-deploying](#re-deploying-after-a-code-change).

### The instance disappeared

Spot reclaimed it. `terraform apply` builds a new one; the dashboard URL changes
with the new IP. `use_spot = false` avoids it at 2.4x the price.

## What this environment does not test

Worth knowing before you draw conclusions from it:

| Not exercised | Because |
|---|---|
| Multi-node behaviour | One node, so one agent. `node_agent_unreachable` and cross-node DNS differences never fire |
| Real node pressure | A quiet 2 GiB node — disk, load, and conntrack findings need provoking |
| The EKS path | This is k3s. Managed node groups, IRSA, ALB, and SES are untested here |
| NetworkPolicy | Disabled, and Flannel would not enforce it regardless |
| TLS and secure cookies | Plain HTTP over nip.io |
| Email | Notifications are off |
| Password hashing at rest | The admin password is passed in plaintext and hashed at load |
| Upgrade and rollback | A fresh install every time; `helm rollback` is never exercised |

For the paths this cannot cover, [../usage.md](../usage.md) is the reference —
and its Part 1 works against this cluster if you set `deploy_bot = false` and
follow it by hand, which is a good way to check that runbook is right.
