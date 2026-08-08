# Test environment for k8s-defect-bot

One EC2 spot instance running k3s, with the bot built on it and installed from
the chart in this repo. Everything a real deployment has — collector, node-agent
DaemonSet, ingress, sign-in — on the cheapest infrastructure that can carry it.

**To run it, follow [usage.md](usage.md)** — this file is what it is and why.
Not production either; [../usage.md](../usage.md) covers deploying for real.

---

## What it creates

```
VPC 10.0.0.0/16
└── public subnet ──── internet gateway        (no NAT gateway, on purpose)
    └── 1x EC2 spot instance (t4g.small, ARM)
        ├── k3s server + agent (single node)
        ├── Traefik ingress   → http://<public-ip>.nip.io
        ├── collector Deployment
        └── node-agent DaemonSet (1 pod, this node)
```

Security group allows SSH, HTTP, and the Kubernetes API **from your IP only**,
auto-detected at plan time. The dashboard shows pod names, images, events, and
container log tails, so it is not something to leave open.

## Cost

| Item | Roughly |
|---|---|
| t4g.small **spot** | ~$0.007/hr → **~$5.10/mo** (checked in us-east-1; on-demand is $0.0168) |
| Public IPv4 address | $0.005/hr → **~$3.60/mo** (charged whether or not the instance runs) |
| 20 GiB gp3 root volume | **~$1.60/mo** |
| VPC, IGW, security groups, data transfer at this scale | **$0** |
| NAT gateway | **not created** — it alone would be ~$32/mo |
| **Total, left running** | **~$10/mo**, or about **$0.015/hr** |

Spot varies by AZ — $0.0070 in `us-east-1a` versus $0.0106 in `us-east-1f` when
this was written. The subnet takes the first AZ in the region, which is not
guaranteed to be the cheapest one; check with:

```bash
aws ec2 describe-spot-price-history --instance-types t4g.small \
  --product-descriptions "Linux/UNIX" --max-items 5 \
  --query "SpotPriceHistory[].[AvailabilityZone,SpotPrice]" --output text
```

`terraform destroy` takes it to zero. Stopping the instance does **not** — the
EBS volume and the public IP keep billing, so destroy is the right verb here.
Nothing on this cluster is worth preserving; a rebuild is one apply.

Prices are indicative (us-east-1, mid-2026) and spot moves — check current rates.

## Use it

```bash
cd terraform
terraform init
terraform plan     # read it -- confirm allowed_cidr is your IP
terraform apply    # ~12 minutes, mostly pip install inside docker build
terraform destroy  # when you are done
```

The full runbook — verifying it works, provoking findings, re-deploying after a
code change, cost control, and troubleshooting — is in **[usage.md](usage.md)**.

## Why it is built this way

**k3s, not EKS.** An EKS control plane is $0.10/hr — about $73/month — before a
single node exists. k3s on one instance gives the same Kubernetes API for
roughly a twentieth of the price, and exercises the bare-metal path in usage.md,
including the k3s container-runtime socket that trips people up.

**No NAT gateway.** At ~$32/month it would cost more than everything else here
combined. The node sits in a public subnet with a security group scoped to one
address.

**Built on the node, not pushed to a registry.** No ECR repository, no registry
credentials, no cross-architecture build. `docker build` on an ARM instance
produces an ARM image, and `k3s ctr images import` puts it where the kubelet
looks. `pullPolicy: IfNotPresent` keeps k3s from trying to fetch it.

**Provisioners rather than the Helm/Kubernetes providers.** Those providers need
a reachable cluster when Terraform *plans*, which on a first apply does not yet
exist — the classic chicken-and-egg that makes people run apply twice. Doing the
install over SSH avoids it entirely.

**Spot, one-time.** An interruption terminates the node, and the next apply
builds a fresh one. That is an acceptable trade for a cluster holding nothing,
and it is why nothing here is stateful. Set `use_spot = false` for on-demand if
you want a node that survives.

## Test-environment choices you should not copy

These are wrong for production and deliberate here:

| Setting | Here | Production |
|---|---|---|
| Admin password | Plaintext in the values, hashed at load | `password_hash` via `auth.existingSecret` |
| `sessionCookieSecure` | `false` — plain HTTP | `true`, behind TLS |
| TLS | None | cert-manager or your own certificate |
| `networkPolicy` | Disabled — k3s ships Flannel, which does not enforce it | Enabled, on a CNI that does |
| Notifications | Off | On, with a real relay |
| Replicas / storage | 1, in-memory | Same — this one is not a compromise |

## Where everything else lives

| You want | Go to |
|---|---|
| To run it, step by step | [usage.md](usage.md) |
| Variables and outputs | [usage.md — Reference](usage.md#part-4--reference), `variables.tf` |
| Troubleshooting | [usage.md — Troubleshooting](usage.md#troubleshooting) |
| What this does *not* test | [usage.md](usage.md#what-this-environment-does-not-test) |
| Deploying the bot for real | [../usage.md](../usage.md) |
