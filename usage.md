# Deploying k8s-defect-bot

A runbook for two targets: a **bare-metal / self-managed** cluster (kubeadm, k3s,
RKE2, MicroK8s, on your own hardware or VMs) and **Amazon EKS**. Work through
Part 1 top to bottom and you have a running, authenticated, alerting deployment.
Everything after that is optional layers, day-2 operations, and reference
material.

Most of Part 1 is identical on both platforms. Where they genuinely differ —
getting the image onto the nodes, where mail goes, and how people reach the
dashboard — the step splits into **On EKS** and **On bare metal**. Anything not
marked applies to both.

**Time:** about 30 minutes either way.

---

## Contents

**Part 1 — Deploy** (do these in order)
- [Before you start](#before-you-start)
- [Step 1 — Set your variables](#step-1--set-your-variables)
- [Step 2 — Get the image onto the nodes](#step-2--get-the-image-onto-the-nodes)
- [Step 3 — Create the credentials](#step-3--create-the-credentials)
- [Step 4 — Write your values file](#step-4--write-your-values-file)
- [Step 5 — Install](#step-5--install)
- [Step 6 — Verify](#step-6--verify)
- [Step 7 — Hand it to your team](#step-7--hand-it-to-your-team)

**Part 2 — Optional layers**
- [Claude analysis on your Pro plan](#claude-analysis-on-your-pro-plan)
- [The kernel-log check](#the-kernel-log-check)

**Part 3 — Running it**
- [Day-2 operations](#day-2-operations)
- [Tuning the noise](#tuning-the-noise)

**Part 4 — Reference**
- [What gets deployed](#what-gets-deployed)
- [Bare-metal and distribution specifics](#bare-metal-and-distribution-specifics)
- [Configuration reference](#configuration-reference)
- [API reference](#api-reference)

**Part 5 — When something is wrong**
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Uninstall](#uninstall)

---

# Part 1 — Deploy

## Before you start

### You need

Both platforms:

```bash
kubectl version --client
helm version             # v3
docker --version         # or podman/nerdctl -- anything that builds an OCI image
python3 --version        # 3.11+, to generate credentials
```

On EKS, additionally the AWS CLI v2:

```bash
aws --version
aws eks update-kubeconfig --region <region> --name <cluster-name>
```

Then, on either platform, confirm you are pointed at the right cluster with
permission to create namespaces, ClusterRoles, and ClusterRoleBindings:

```bash
kubectl config current-context     # read this before every step below
kubectl get nodes
kubectl auth can-i create clusterrolebinding    # expect yes
```

**IRSA is not required on EKS.** The bot talks to the Kubernetes API using its
ServiceAccount, not AWS credentials — which is also why nothing in the collector
is AWS-specific.

### What differs between the two platforms

Everything else in this runbook is shared.

| | EKS | Bare metal / self-managed |
|---|---|---|
| Image distribution | ECR ([Step 2](#step-2--get-the-image-onto-the-nodes)) | Your registry, or side-load onto each node |
| Reaching the dashboard | ALB Ingress ([Step 7](#step-7--hand-it-to-your-team)) | ingress-nginx, MetalLB, or NodePort |
| TLS | ACM certificate on the ALB | cert-manager, or your own certificate Secret |
| Mail | SES SMTP | Your internal relay, or any SMTP provider |
| `networkPolicy.nodeCidrs` | The VPC subnet CIDRs | Your node subnet, from `kubectl get nodes -o wide` |
| Container runtime socket | Default is correct | **Distribution-specific — k3s and RKE2 differ** |
| Node clock | Amazon Time Sync, usually fine | Your own NTP; `node_clock_skew` earns its keep here |

The one that bites people on bare metal is the runtime socket: k3s and RKE2 do
not use `/run/containerd/containerd.sock`, so `node_container_runtime` reports
critical on every node until you set it. See
[Bare-metal and distribution specifics](#bare-metal-and-distribution-specifics).

### Two things to know before you commit

**1. It runs as a single replica, on purpose.** Findings and login sessions live
in memory — there is no database and no PVC. A restart re-scans within seconds
and signs everyone out; it does not lose anything durable. Do not raise
`replicaCount`: two replicas would serve different findings depending on which
pod answered, and sign-in would break as sessions landed on the wrong one. The
Deployment uses `Recreate` so an upgrade never briefly runs two.

**2. It is read-only.** RBAC is `get`/`list`. Every remediation it produces is a
command printed for a human to run. It cannot modify, delete, or create anything
in your cluster.

### Decide these now

| Decision | Recommended for production | Where it goes |
|---|---|---|
| Who may sign in | A real user registry from a Secret | [Step 3](#step-3--create-the-credentials) |
| How people are notified | SES, or your internal relay, plus a per-person severity floor | [Step 3](#step-3--create-the-credentials) |
| How it is reached | EKS: internal ALB with ACM TLS. Bare metal: ingress-nginx with cert-manager | [Step 4](#step-4--write-your-values-file) |
| Claude analysis | Start `none`; add later | [Part 2](#claude-analysis-on-your-pro-plan) |

---

## Step 1 — Set your variables

Everything below reuses these. Set them once in the shell you'll work in.

**On EKS:**

```bash
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export CLUSTER_NAME=my-prod-eks
export NAMESPACE=k8s-defect-bot
# Keep this exactly "k8s-defect-bot" -- see the note below.
export RELEASE=k8s-defect-bot
export ECR_REPO=k8s-defect-bot
export IMAGE_TAG=0.3.0
export IMAGE_URI=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:$IMAGE_TAG
export VALUES=my-production-values.yaml

echo "$IMAGE_URI"
```

**On bare metal:**

```bash
export CLUSTER_NAME=dc1-prod                 # whatever you call it; appears in every email subject
export NAMESPACE=k8s-defect-bot
# Keep this exactly "k8s-defect-bot" -- see the note below.
export RELEASE=k8s-defect-bot
export IMAGE_TAG=0.3.0
# Your registry. With no registry at all, use a bare name and side-load -- see Step 2.
export REGISTRY=registry.internal.example.com
export IMAGE_URI=$REGISTRY/k8s-defect-bot:$IMAGE_TAG
export VALUES=my-baremetal-values.yaml

echo "$IMAGE_URI"
```

> **Why the release name is pinned.** The chart names resources after the release,
> except when the release name already contains the chart name — then it uses the
> release name as-is. `RELEASE=k8s-defect-bot` therefore gives you a Deployment
> called exactly `k8s-defect-bot`, and every `"$RELEASE"` command in this runbook
> works verbatim. Pick `RELEASE=defect-bot` instead and everything is named
> `defect-bot-k8s-defect-bot`, so those commands quietly fail with
> `NotFound`. If you must use a different release name, resolve the real one with:
>
> ```bash
> helm -n "$NAMESPACE" get manifest "$RELEASE" | grep -m1 -A2 'kind: Deployment'
> ```

---

## Step 2 — Get the image onto the nodes

One image serves both the collector and the node agent, so whatever you do here
covers both.

### On EKS — push to ECR

```bash
# Create the repository (once per account/region)
aws ecr create-repository \
  --repository-name "$ECR_REPO" \
  --region "$AWS_REGION" \
  --image-scanning-configuration scanOnPush=true

# Log in
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin \
    "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

# Build and push
docker build --platform linux/amd64 -t "$IMAGE_URI" .
docker push "$IMAGE_URI"
```

### On bare metal — a registry, or side-load

**If you have a registry** (Harbor, GitLab, a plain `registry:2`), this is the
same as EKS minus the ECR ceremony:

```bash
docker login "$REGISTRY"
docker build --platform linux/amd64 -t "$IMAGE_URI" .
docker push "$IMAGE_URI"
```

If it uses a private CA or a credential, add a pull secret and reference it:

```bash
kubectl -n "$NAMESPACE" create secret docker-registry regcred \
  --docker-server="$REGISTRY" --docker-username=<user> --docker-password=<pass>
# values.yaml:  imagePullSecrets: [{ name: regcred }]
```

**If you have no registry**, save the image once and import it on **every node** —
the DaemonSet means every node needs it, not just the one running the collector:

```bash
docker build --platform linux/amd64 -t k8s-defect-bot:0.3.0 .
docker save k8s-defect-bot:0.3.0 -o /tmp/kdb.tar

# copy /tmp/kdb.tar to each node, then on each node:
sudo ctr -n k8s.io images import /tmp/kdb.tar      # containerd (kubeadm, RKE2)
sudo k3s ctr images import /tmp/kdb.tar            # k3s
sudo microk8s ctr images import /tmp/kdb.tar       # MicroK8s
sudo podman load -i /tmp/kdb.tar                   # CRI-O with shared storage
```

The `-n k8s.io` namespace is not optional for plain containerd — an image
imported into the default namespace is invisible to the kubelet, and you get
`ImagePullBackOff` for an image you can see in `ctr images ls`.

Then keep the chart from trying to fetch it:

```yaml
image:
  repository: k8s-defect-bot     # no registry host
  tag: "0.3.0"
  pullPolicy: IfNotPresent       # the default; Never also works
```

Side-loading is fine for a handful of nodes and becomes the worst part of every
upgrade past that. A single-node `registry:2` on the cluster network pays for
itself quickly.

### Both platforms

> **`--platform` is not optional** when your workstation's architecture differs
> from your nodes'. An image built on an Apple Silicon Mac defaults to `arm64`,
> and on `amd64` nodes the pod `CrashLoopBackOff`s with `exec format error`.
> Build `--platform linux/arm64` for Graviton node groups or arm64 hardware
> (a Raspberry Pi cluster, Ampere servers). Check with
> `kubectl get nodes -o wide` — the ARCH column is authoritative.

Pin an immutable tag like `0.3.0`. Never deploy `:latest` to production — you
lose the ability to say what is actually running.

---

## Step 3 — Create the credentials

Three Secrets, created directly in the cluster so nothing sensitive lands in a
values file, in git, or in your shell history.

```bash
kubectl create namespace "$NAMESPACE"
```

### 3a. The user registry — who may sign in, and who gets the mail

The bot keeps **one list of people**, and it does both jobs: it decides who may
sign in and it holds the address their notifications go to. Keeping them together
is deliberate — an access list and a mailing list maintained separately drift
apart, and you end up mailing people who left.

Generate an entry per person. The CLI prompts for the password and never echoes
it:

```bash
python3 -m app.auth new-user alice@example.com --name "Alice" --role admin
python3 -m app.auth new-user bob@example.com   --name "Bob"   --role viewer
```

| Role | Can do |
|---|---|
| `viewer` | Read the dashboard and every read-only API endpoint |
| `admin` | The same, plus trigger scans, list users, and send test mail |

Collect the entries into one JSON array and set each person's notification
preferences:

```bash
cat > /tmp/users.json <<'JSON'
[
  {
    "email": "alice@example.com",
    "name": "Alice",
    "role": "admin",
    "password_hash": "scrypt$16384$8$1$PASTE$FROM-THE-CLI",
    "notify": {
      "mode": "immediate",
      "min_severity": "critical",
      "include_resolved": true,
      "include_node_findings": true
    }
  },
  {
    "email": "bob@example.com",
    "name": "Bob",
    "role": "viewer",
    "password_hash": "scrypt$16384$8$1$PASTE$FROM-THE-CLI",
    "notify": {
      "mode": "digest",
      "min_severity": "critical",
      "namespaces": ["prod"],
      "muted_types": ["missing_resource_limits"]
    }
  }
]
JSON

kubectl -n "$NAMESPACE" create secret generic k8s-defect-bot-users \
  --from-file=users.json=/tmp/users.json

rm /tmp/users.json
```

Passwords are scrypt-hashed with a per-user random salt and verified in constant
time. Only the hash is stored, so someone who reads the Secret gets something
useless to them rather than a password your colleague reuses elsewhere.

Keep the file in a private repo or a password manager if you want it reviewable —
just not the plaintext passwords.

<details>
<summary>Every field a user entry accepts</summary>

| Field | Default | Meaning |
|---|---|---|
| `email` | required | Sign-in identity and delivery address |
| `name` | `""` | Shown in the header and in mail |
| `role` | `viewer` | `admin` or `viewer` |
| `password_hash` | — | From `python3 -m app.auth hash-password` |
| `api_token_hash` | — | From `python3 -m app.auth new-token`, for scripts and CI |
| `disabled` | `false` | Revoke access without deleting the entry |
| `notify.mode` | `immediate` | `immediate`, `digest`, or `off` |
| `notify.min_severity` | `critical` | `warning` to also receive warnings |
| `notify.muted_types` | `[]` | Defect types this person never wants |
| `notify.namespaces` | `[]` (all) | Restrict to specific namespaces |
| `notify.include_resolved` | `true` | Whether cleared defects are reported |
| `notify.include_node_findings` | `true` | Node-agent findings and agent outages |

A person can be stricter than the cluster-wide floor, never looser.
</details>

### 3b. SMTP credentials

Any SMTP server works. The bot needs a host, a port, and — unless your relay is
open to the cluster — a username and password.

**On EKS (SES):** create **SES SMTP credentials** in the SES console — these are
*not* your AWS access keys — and verify your `from` address or domain first.

```bash
kubectl -n "$NAMESPACE" create secret generic k8s-defect-bot-smtp \
  --from-literal=smtp-username='<SES SMTP username>' \
  --from-literal=smtp-password='<SES SMTP password>'
```

> **Check whether your SES account is still in the sandbox.** In sandbox mode SES
> only delivers to verified addresses, so mail to your team silently goes
> nowhere. `aws ses get-account-sending-enabled` and the SES console will tell
> you; request production access if needed.

**On bare metal:** the same Secret, with your relay's credentials — a Microsoft
365 / Google Workspace account with an app password, or your own Postfix.

```bash
kubectl -n "$NAMESPACE" create secret generic k8s-defect-bot-smtp \
  --from-literal=smtp-username='k8s-defect-bot@example.com' \
  --from-literal=smtp-password='<app password>'
```

Most internal relays accept unauthenticated mail from inside the network. In that
case **create no Secret at all** — leave `notifications.smtp.existingSecret`,
`username`, and `password` empty and set only the host and port:

```yaml
notifications:
  enabled: true
  smtp:
    host: smtp.internal.example.com
    port: 25
    starttls: false      # most plain internal relays do not offer STARTTLS on 25
  from: k8s-defect-bot@example.com
```

Whatever you use, confirm the relay will accept the `from` address for a machine
sender. A relay that silently drops unknown senders looks exactly like working
alerting until the day you need it — which is what
[Step 6's test email](#step-6--verify) is for.

### 3c. The agent token

Skip this one — Helm generates a shared token for the node agents and reuses it
across upgrades, so agents and collector never disagree about it mid-rollout.
Only create it yourself if you want to control the value:

```bash
kubectl -n "$NAMESPACE" create secret generic k8s-defect-bot-agent-token \
  --from-literal=token="$(openssl rand -hex 32)"
# then set nodeAgent.existingSecret: k8s-defect-bot-agent-token
```

---

## Step 4 — Write your values file

Start from the annotated template for your platform — both hold no secrets, only
choices:

```bash
# EKS
cp helm/k8s-defect-bot/values-production.yaml my-production-values.yaml

# Bare metal
cp helm/k8s-defect-bot/values-baremetal.yaml my-baremetal-values.yaml
```

Replace every `REPLACE-ME`. The settings that matter most:

**On EKS:**

```yaml
image:
  repository: <account>.dkr.ecr.<region>.amazonaws.com/k8s-defect-bot
  tag: "0.3.0"

config:
  clusterName: my-prod-eks                              # every email subject
  dashboardUrl: https://defect-bot.internal.example.com # links in email

auth:
  existingSecret: k8s-defect-bot-users
  sessionCookieSecure: true          # you are serving over HTTPS

notifications:
  enabled: true
  smtp:
    host: email-smtp.us-east-1.amazonaws.com
    existingSecret: k8s-defect-bot-smtp
  from: k8s-defect-bot@example.com   # verified in SES

networkPolicy:
  enabled: true
  nodeCidrs:
    - 10.0.0.0/16                    # see below -- getting this wrong is silent
```

**On bare metal:**

```yaml
image:
  repository: registry.internal.example.com/k8s-defect-bot   # or a bare name if side-loaded
  tag: "0.3.0"
  pullPolicy: IfNotPresent

config:
  clusterName: dc1-prod
  dashboardUrl: https://defect-bot.internal.example.com

auth:
  existingSecret: k8s-defect-bot-users
  sessionCookieSecure: true          # false if you are serving plain HTTP over NodePort

notifications:
  enabled: true
  smtp:
    host: smtp.internal.example.com
    existingSecret: k8s-defect-bot-smtp   # omit entirely for an open internal relay

nodeAgent:
  # k3s and RKE2: /run/k3s/containerd/containerd.sock -- see the table in Part 4
  containerRuntimeSocket: /run/containerd/containerd.sock

ingress:
  enabled: true
  className: nginx

networkPolicy:
  enabled: true
  nodeCidrs:
    - 192.168.10.0/24                # your node subnet
```

**Find your node CIDRs.** The node agents run with `hostNetwork`, so their
traffic arrives from the *node's* address, not a pod address. A NetworkPolicy
that only allows a pod selector will lock every agent out — and the symptom is
just an empty `/api/nodes`, with nothing in the logs to explain it.

```bash
# Bare metal (works anywhere): read the INTERNAL-IP column and cover it
kubectl get nodes -o wide

# EKS: the VPC subnets the node groups sit in
aws ec2 describe-subnets \
  --subnet-ids $(aws eks describe-cluster --name "$CLUSTER_NAME" \
    --query 'cluster.resourcesVpcConfig.subnetIds' --output text) \
  --query 'Subnets[].CidrBlock' --output text
```

On bare metal, cover the subnet rather than listing `/32`s — a node replaced with
a new address otherwise goes silently blind. And check your CNI actually enforces
NetworkPolicy: Calico, Cilium, and Antrea do; **plain Flannel does not**, so the
policy applies cleanly and protects nothing. `networkPolicy.enabled: false` with
an honest note beats a policy you believe in wrongly.

Commit your values file to git. It contains no credentials.

---

## Step 5 — Install

Render it first and actually read the output — this is the last point before
anything reaches the cluster:

```bash
helm lint ./helm/k8s-defect-bot -f "$VALUES"

helm template "$RELEASE" ./helm/k8s-defect-bot \
  --namespace "$NAMESPACE" \
  -f "$VALUES" | less
```

Then install:

```bash
helm upgrade --install "$RELEASE" ./helm/k8s-defect-bot \
  --namespace "$NAMESPACE" --create-namespace \
  -f "$VALUES" \
  --wait --timeout 5m
```

`--wait` blocks until the collector passes its readiness probe, so the command
failing means the deploy failed rather than leaving you to discover it later.

Two things that stop an install on a self-managed cluster and never come up on
EKS:

- **Pod Security Admission.** The node agent needs `hostNetwork` and two hostPath
  mounts, which the `baseline` and `restricted` policies forbid. If your cluster
  sets a restrictive default, the DaemonSet is rejected with
  `violates PodSecurity` and the collector comes up alone. Label the namespace
  for the agent to be admitted:
  ```bash
  kubectl label namespace "$NAMESPACE" pod-security.kubernetes.io/enforce=privileged
  ```
- **A single-node cluster.** The control plane is usually tainted
  `node-role.kubernetes.io/control-plane:NoSchedule`, and the collector — unlike
  the agent — does not tolerate anything by default, so it sits `Pending`
  forever. Either add the toleration (commented in `values-baremetal.yaml`) or
  untaint the node.

---

## Step 6 — Verify

Work down this list. Each check tells you something the previous one didn't.

```bash
# 1. One collector, and one agent per node
kubectl -n "$NAMESPACE" get pods -o wide
kubectl -n "$NAMESPACE" get daemonset

# 2. The collector started clean -- read these lines, don't just grep for errors
kubectl -n "$NAMESPACE" logs deploy/"$RELEASE" | head -30
```

You are looking for, in order:

```
starting k8s-defect-bot on cluster 'my-prod-eks' (interval=300s, ...)
loaded 2 user(s): alice@example.com, bob@example.com
authentication enabled for 2 user(s); 2 may receive notifications
email notifications on via email-smtp.us-east-1.amazonaws.com:587 as ...
scan complete in 2.41s: 7 defects (2 critical, 5 warning) across 214 pods / 3 nodes
notification baseline established: 7 existing defect(s), no mail sent
```

**Any of these means something needs fixing before you go further:**

| Log line | Meaning |
|---|---|
| `AUTHENTICATION IS OFF` | The users Secret is missing or empty — anyone who reaches the Service has full access |
| `the user registry could not be loaded` | Malformed JSON; sign-in is locked until you fix it |
| `notifications are enabled but not usable` | `SMTP_HOST` is unset |
| `no user has a delivery mode set` | Everyone is `mode: off`; nothing will ever be sent |
| `AGENT_TOKEN is not set` | The agent intake accepts unauthenticated reports |
| `Cluster appears unreachable` | RBAC or connectivity — findings are empty because nothing could be read |

Then confirm the moving parts:

```bash
kubectl -n "$NAMESPACE" port-forward svc/"$RELEASE" 8080:80 &

# 3. Auth is actually enforced
curl -s -o /dev/null -w "%{http_code}\n" localhost:8080/api/defects   # expect 401
curl -s -o /dev/null -w "%{http_code}\n" localhost:8080/healthz       # expect 200

# 4. Sign in, then check every node is reporting
curl -s -c /tmp/c.txt -d "email=alice@example.com&password=<password>" \
  localhost:8080/login -o /dev/null
curl -s -b /tmp/c.txt localhost:8080/api/nodes | python3 -m json.tool
```

The length of `/api/nodes` must equal `kubectl get nodes --no-headers | wc -l`.
A node missing here has no agent — usually a taint, or the NetworkPolicy CIDRs.
Agents report every 120s, so give it two minutes first.

```bash
# 5. Prove the mail path works, end to end, to a real inbox
curl -s -b /tmp/c.txt -X POST localhost:8080/api/notifications/test
```

Expect `{"status":"sent",...}` and a message in Alice's inbox. **Do this now**,
not the first time something breaks — an untested alerting path is the same as
no alerting path.

```bash
# 6. Read the dashboard yourself
open http://localhost:8080
```

---

## Step 7 — Hand it to your team

### On EKS — the ALB

The ALB from step 4 takes a couple of minutes to provision:

```bash
kubectl -n "$NAMESPACE" get ingress
# ADDRESS appears once the load balancer is ready
```

Point your internal DNS record at that address.

### On bare metal — pick one of three

There is no cloud load balancer, so choose how people reach the collector. In
descending order of how much you'll like living with it:

**1. An ingress controller** (ingress-nginx, Traefik — k3s ships Traefik
already). Best if you run one, and the only option that gives you a hostname and
TLS without extra work:

```yaml
ingress:
  enabled: true
  className: nginx          # or "traefik" on a default k3s install
  host: defect-bot.internal.example.com
  tls:
    - hosts: [defect-bot.internal.example.com]
      secretName: k8s-defect-bot-tls
```

With cert-manager, add `cert-manager.io/cluster-issuer` to
`ingress.annotations` and it fills that Secret in. Without it, create the Secret
from your own certificate:

```bash
kubectl -n "$NAMESPACE" create secret tls k8s-defect-bot-tls \
  --cert=defect-bot.crt --key=defect-bot.key
```

**2. MetalLB**, if you have an address pool and no ingress controller:

```yaml
service:
  type: LoadBalancer
```

MetalLB assigns an address from its pool — the chart does not render a
`loadBalancerIP` or service annotations, so you take what the pool gives you
(`kubectl -n "$NAMESPACE" get svc`) and point DNS at it. Pinning a specific
address means editing the Service afterwards.

**3. NodePort** — nothing extra to install, fine for a small team:

```yaml
service:
  type: NodePort
```

Kubernetes assigns a port from 30000–32767; the chart does not pin one, so read
it back:

```bash
kubectl -n "$NAMESPACE" get svc "$RELEASE" -o jsonpath='{.spec.ports[0].nodePort}'
# reach it at http://<any-node-ip>:<that-port>
```

This is plain HTTP. Set `auth.sessionCookieSecure: false` for it — with `true`
the browser refuses to send the session cookie back over HTTP and sign-in
appears to succeed and then immediately fail.

### Both platforms

Confirm it answers, then hand it over:

```bash
curl -sI https://defect-bot.internal.example.com/login | head -1   # expect 200
```

Send your team: the URL, their email, their password, and the note that the
dashboard is read-only — every fix it suggests is a command for a human to run.

> **Keep it internal.** The dashboard shows pod names, images, events, and
> container log tails to anyone signed in. On EKS, `scheme: internal` plus your
> VPN is the baseline, and the ALB's OIDC integration is a good second gate on
> top of the bot's own login. On bare metal the equivalent is not exposing the
> ingress host outside your network at all — plus `networkPolicy.allowFrom` to
> keep the rest of the cluster from reaching the collector.

**You now have a production deployment.** Everything below is optional or
operational.

---

# Part 2 — Optional layers

## Claude analysis on your Pro plan

Every defect already gets a heuristic root cause, remediation steps, and
copy-pasteable commands with no Claude involvement. Enrichment adds a specific
reading of the log tail and events on top.

| `llm.provider` | Credentials | Billing |
|---|---|---|
| `none` (default) | none | free |
| `claude_cli` | your Claude Code login | **your Claude Pro/Max subscription** |
| `anthropic_api` | `ANTHROPIC_API_KEY` | per-token API billing, separate from any plan |

> **Your Pro subscription is not an API key.** Pro covers the Claude apps and
> Claude Code, not API credits. `claude_cli` shells out to `claude -p`, which
> uses whatever that CLI is logged into — which is how a subscription gets used
> programmatically.

### Option A — run the collector locally (start here)

The collector works fine outside the cluster; it falls back to your kubeconfig.
Nothing to build, no token, no Secret:

```bash
pip install -r requirements.txt
LLM_PROVIDER=claude_cli CLAUDE_MODEL=sonnet python main.py
# http://localhost:8080
```

Leave the production deployment on `provider: none` and run this when you want a
deeper read on something. This is the verified path.

### Option B — Claude in-cluster

Needs an image that bundles the CLI, and a credential.

```bash
# 1. Build the variant (same registry as before, a different tag)
export CLAUDE_IMAGE="${IMAGE_URI%:*}:0.3.0-claude"
docker build --platform linux/amd64 -f Dockerfile.claude -t "$CLAUDE_IMAGE" .
docker push "$CLAUDE_IMAGE"
# no registry: build it, then side-load onto whichever node runs the collector --
# this image is only needed there, not on every node

# 2. Mint a long-lived token from your subscription, on your machine
claude setup-token

# 3. Store it
kubectl -n "$NAMESPACE" create secret generic k8s-defect-bot-llm \
  --from-literal=claude-code-oauth-token='<token>'
```

Then in your values file:

```yaml
image:
  tag: "0.3.0-claude"
llm:
  provider: claude_cli
  model: sonnet
  existingSecret: k8s-defect-bot-llm
  writableHome: true      # the CLI needs a writable HOME; rootfs is read-only
```

> **Verify this path on your own plan before relying on it.** `claude setup-token`
> states it requires a Claude subscription; whether your specific Pro plan issues
> a usable long-lived token is something only your account can confirm. If step 2
> fails, Option A needs none of it. The cluster also needs egress to
> `api.anthropic.com`, which some private-subnet clusters block.

**On an air-gapped cluster, Option B is simply unavailable** — enrichment is an
outbound HTTPS call, and no configuration works around that. Option A on a
workstation that does have egress covers the same need: point it at the cluster
with your kubeconfig, read the enriched findings, leave the in-cluster
deployment on `provider: none`. The heuristic root cause, remediation, and
commands are unaffected either way; enrichment only ever adds to them.

### Keeping usage bounded

- Only `llm.maxDefectsPerScan` defects per scan (default **5**), worst-first.
- The Claude Code system prompt is replaced with a small task-specific one,
  cutting several thousand tokens per call.
- All file, shell, and web tools are denied — one turn, no host access.
- Failures and timeouts fall back silently to the heuristic answer.

`llm.model` defaults to `opus`, the most capable. On a Pro plan and a busy
cluster that consumes quota quickly — **`sonnet` is the practical choice.**

---

## The kernel-log check

`node_kernel_errors` reads `/dev/kmsg` for OOM kills, blocked tasks, and I/O
errors. It needs root plus `CAP_SYSLOG`, so it is off by default. Enable it
knowing that trade-off:

```yaml
nodeAgent:
  privileged: true
  enabledChecks:
    # ... the ten defaults ...
    - node_kernel_errors
```

---

# Part 3 — Running it

## Day-2 operations

### Upgrading

```bash
# With a registry (ECR or your own)
docker build --platform linux/amd64 -t "${IMAGE_URI%:*}:0.3.1" .
docker push "${IMAGE_URI%:*}:0.3.1"

# Side-loading instead: repeat the Step 2 import on EVERY node before upgrading.
# A node that missed the new tag gets ImagePullBackOff and loses its agent --
# which shows up as a stale node in /api/nodes, not as a failed upgrade.

helm upgrade "$RELEASE" ./helm/k8s-defect-bot \
  --namespace "$NAMESPACE" -f "$VALUES" \
  --set image.tag=0.3.1 --wait --timeout 5m
```

The generated agent token and any bootstrap password are read back from the live
Secrets, so an upgrade never rotates them out from under running agents.

### Rolling back

```bash
helm history "$RELEASE" -n "$NAMESPACE"
helm rollback "$RELEASE" <revision> -n "$NAMESPACE" --wait
```

Nothing persists, so a rollback is clean — the new pod re-scans within seconds.

### Adding or removing a person

Edit the registry and restart. The Secret is read once at startup.

```bash
kubectl -n "$NAMESPACE" get secret k8s-defect-bot-users \
  -o jsonpath='{.data.users\.json}' | base64 -d > /tmp/users.json

# add, remove, or set "disabled": true on an entry
$EDITOR /tmp/users.json

kubectl -n "$NAMESPACE" create secret generic k8s-defect-bot-users \
  --from-file=users.json=/tmp/users.json --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NAMESPACE" rollout restart deploy/"$RELEASE"
rm /tmp/users.json
```

Setting `"disabled": true` revokes access *and* kills any live session on the
next request, while keeping the audit trail of who had access.

### Rotating a password or token

Same flow: generate a new hash with `python3 -m app.auth hash-password`, replace
it in the registry, restart. For the agent token, delete the Secret and let Helm
regenerate it on the next upgrade — both the collector and the DaemonSet pick up
the new value as they roll.

### What to watch

The bot is a monitoring tool, so it needs a little monitoring of its own:

| Signal | Why |
|---|---|
| Collector pod restarts | It should stay up for weeks |
| `/readyz` | 503 means no scan has completed |
| `agents_reporting` vs node count (`/api/summary`) | Silently losing agents means blind nodes |
| `suppressed` on the notifications panel | Someone is hitting the hourly cap — the cluster is flapping or the floor is too low |
| Failed sends on the notifications panel | SMTP broke; alerting is down |

A single alert on "collector pod not ready for 10 minutes" from whatever you
already run covers most of it.

---

## Tuning the noise

The failure mode for a tool like this is not missing a defect — it is being
ignored. If people stop reading it, fix that first.

| Symptom | Fix |
|---|---|
| Hundreds of `missing_resource_limits` | Drop it from `config.enabledRules`, or mute it per person |
| Alerts about namespaces a team doesn't own | Set `notify.namespaces` per person |
| Too much mail | `mode: digest`, or lower `notifications.maxEmailsPerHour` |
| Warnings nobody acts on | Keep `notifications.minSeverity: critical` |
| A node check that is always noisy on your AMI | Raise its threshold rather than disabling it |
| Dashboard full of `kube-system` | Set `config.namespaceFilter` to the namespaces you own |

Mute per person before disabling globally — one person's noise is another's
signal.

---

# Part 4 — Reference

## What gets deployed

Two workloads from one image:

| Workload | Kind | Where | What it does |
|---|---|---|---|
| **Collector** | Deployment (1 replica) | Anywhere | Scans via the Kubernetes API every 5 min, serves the dashboard and API, receives agent reports, sends mail |
| **Node agent** | DaemonSet | Every node | Runs 10 node-local checks, POSTs findings to the collector |

```
                    ┌──────────────────────────────────┐
   kube-apiserver ──►  Collector (Deployment, 1 pod)   ◄── ALB / port-forward
      read-only     │   • cluster scan every 300s      │
                    │   • dashboard + JSON API         │──► SMTP ──► your team
                    │   • in-memory store              │
                    └──────────────▲───────────────────┘
                                   │ POST /api/agent/report
                                   │ (bearer token, every 120s)
              ┌────────────────────┼────────────────────┐
        ┌─────┴─────┐        ┌─────┴─────┐        ┌─────┴─────┐
        │  agent    │        │  agent    │        │  agent    │   ← DaemonSet
        │  node 1   │        │  node 2   │        │  node 3   │
        └───────────┘        └───────────┘        └───────────┘
```

### Why a DaemonSet as well as a cluster scan

The control-plane scan sees what the API server knows: a node is `Ready` or it
isn't. By the time that flips, pods have already been evicted. The agent reads
the node itself, so you see the trend instead of the outcome:

| The agent catches | The API server would only tell you |
|---|---|
| Disk at 84% and climbing | `DiskPressure`, after eviction starts |
| Inodes exhausted on a disk that looks half-empty | Pods failing to write, cause unclear |
| Conntrack table 91% full | Random connection timeouts across every pod |
| Kubelet healthz failing | `NotReady`, ~40s later |
| Cluster DNS broken *from that node* | Nothing — CoreDNS itself looks fine |
| Node clock 6 minutes off | Nothing — until TLS starts failing |
| PID exhaustion, load, memory headroom | `MemoryPressure`/`PIDPressure` at the threshold |

### Node agent checks

| Check | Fires when | Severity |
|---|---|---|
| `node_disk_usage` | Filesystem ≥ 80% / ≥ 90% full | warning / critical |
| `node_inode_usage` | Inodes ≥ 80% / ≥ 90% used | warning / critical |
| `node_memory_available` | Memory ≥ 85% / ≥ 95% used | warning / critical |
| `node_load_average` | 5-min load ≥ 2.0 / ≥ 4.0 per CPU | warning / critical |
| `node_pid_usage` | PIDs ≥ 80% / ≥ 90% of `pid_max` | warning / critical |
| `node_conntrack_usage` | Conntrack ≥ 80% / ≥ 90% full | warning / critical |
| `node_kubelet_health` | `127.0.0.1:10248/healthz` not OK | critical |
| `node_container_runtime` | containerd socket missing | critical |
| `node_dns_resolution` | Cluster DNS lookup fails from this node | critical |
| `node_apiserver_reachable` | Cannot connect to the API server | critical |
| `node_kernel_errors` | OOM kills / blocked tasks / I/O errors | critical (opt-in) |
| `node_clock_skew` | Node clock differs from collector by > 60s | warning / critical |
| `node_agent_unreachable` | A node stops reporting entirely | warning |

The last two come from the **collector** — an agent cannot detect its own absence
or measure its own clock drift.

<details>
<summary>Why the agent pod is configured the way it is</summary>

| Setting | Reason |
|---|---|
| `hostNetwork: true` | Kubelet healthz binds `127.0.0.1:10248`, unreachable from a pod netns |
| `dnsPolicy: ClusterFirstWithHostNet` | Otherwise `hostNetwork` uses the node's resolver and the DNS check never exercises cluster DNS |
| `tolerations: [{operator: Exists}]` | Monitoring is only useful if it also runs on the nodes people taint |
| `priorityClassName: system-node-critical` | A node under pressure must not evict the agent reporting that pressure |
| `automountServiceAccountToken: false` | The agent needs no Kubernetes API access at all |
| hostPath `/` and `/proc`, read-only | The only way to read the node's real filesystem and kernel stats |
| non-root, all caps dropped, read-only rootfs | Everything it reads is world-readable |
</details>

---

## Bare-metal and distribution specifics

Nothing in the collector is cloud-specific — it reads the Kubernetes API and
that's all. The node agent is where a self-managed cluster differs, because it
reads the node itself.

### The container runtime socket

The single setting most likely to be wrong. The agent checks that
`nodeAgent.containerRuntimeSocket` exists and is a socket, resolved under its
read-only mount of `/`. Point it at a path your distribution doesn't use and
**every node reports `node_container_runtime` as critical** — a wall of red that
says nothing about your cluster.

| Distribution | `nodeAgent.containerRuntimeSocket` |
|---|---|
| kubeadm, plain containerd | `/run/containerd/containerd.sock` (the default) |
| **k3s** | `/run/k3s/containerd/containerd.sock` |
| **RKE2** | `/run/k3s/containerd/containerd.sock` |
| **MicroK8s** | `/var/snap/microk8s/common/run/containerd.sock` |
| CRI-O | `/var/run/crio/crio.sock` |
| Docker via cri-dockerd | `/var/run/cri-dockerd.sock` |

Confirm rather than trust the table — on any node:

```bash
sudo ls -l /run/containerd/containerd.sock /run/k3s/containerd/containerd.sock 2>&1 | grep -v 'No such'
# or ask the kubelet what it was told to use:
ps aux | grep -o '\--container-runtime-endpoint=[^ ]*'
```

### Everything else worth knowing per distribution

| | k3s | RKE2 | MicroK8s | kubeadm |
|---|---|---|---|---|
| Image import | `sudo k3s ctr images import` | `sudo ctr -n k8s.io images import` | `sudo microk8s ctr images import` | `sudo ctr -n k8s.io images import` |
| Ingress class | `traefik` (bundled) | `nginx` (bundled) | `nginx` (`microk8s enable ingress`) | whatever you installed |
| LoadBalancer Services | ServiceLB, bundled | ServiceLB, bundled | MetalLB (`microk8s enable metallb`) | needs MetalLB |
| NetworkPolicy enforced | only with a CNI that does | Canal/Cilium, yes | Calico, yes | depends on your CNI |

k3s and RKE2 ship a LoadBalancer implementation, so `service.type: LoadBalancer`
works out of the box on both — usually the least-effort exposure on those two.

### What the node checks mean when the hardware is yours

The checks don't change, but what you do about them does — the EKS answer is
often "replace the node", which is not available to you.

| Check | On EKS | On your own hardware |
|---|---|---|
| `node_disk_usage` | Grow the EBS volume | Real disks. Log rotation and image pruning are the fix; expansion is a maintenance window |
| `node_inode_usage` | Rebuild the node | The filesystem was formatted with the inode count it has — moving the workload to its own volume is usually faster than reformatting |
| `node_clock_skew` | Rare; Amazon Time Sync is there by default | **Earns its keep.** Nothing installs NTP for you. Check `chronyc tracking` on the node |
| `node_apiserver_reachable` | Security group or private endpoint DNS | Your HA VIP (keepalived/kube-vip) or the load balancer in front of the control plane |
| `node_dns_resolution` | CoreDNS, or the node's security group on UDP 53 | CoreDNS, or a host firewall — `firewalld` and `ufw` block pod-to-service traffic in ways cloud SGs don't |
| `node_conntrack_usage` | Instance-type dependent | Tunable in `/etc/sysctl.d`, and permanent there — unlike a launch-template change that only applies to new nodes |
| `node_kubelet_health` | Replace the node | `journalctl -u kubelet` (or `-u k3s`, `-u rke2-server`) |

### Pod Security Admission

The agent needs `hostNetwork` and hostPath mounts of `/` and `/proc`, which the
`baseline` and `restricted` PSA levels forbid. Clusters built by hand more often
enforce a default than EKS does:

```bash
kubectl label namespace "$NAMESPACE" pod-security.kubernetes.io/enforce=privileged
```

This applies to the bot's own namespace only. If that is not acceptable, the
collector runs perfectly well with `nodeAgent.enabled: false` — you lose the
node-local half of the picture, not the cluster scan.

### Running it entirely outside the cluster

The collector falls back to your kubeconfig, so a laptop or jump host works as a
deployment target:

```bash
pip install -r requirements.txt
CLUSTER_NAME=dc1-prod python main.py     # http://localhost:8080
```

You get the 13 cluster rules and no node agents. For a homelab or a cluster
you're evaluating, this is a legitimate way to run it — no image, no registry,
no Helm.

---

## Configuration reference

Every value is settable as an environment variable (raw manifests) or a Helm
value. Only the ones you are likely to change are listed.

### Collector

| Env / Helm value | Default | Meaning |
|---|---|---|
| `CLUSTER_NAME` / `config.clusterName` | `my-eks-cluster` | Dashboard header and every email subject |
| `DASHBOARD_URL` / `config.dashboardUrl` | `""` | Absolute URL for links in email |
| `SCAN_INTERVAL_SECONDS` / `config.scanIntervalSeconds` | `300` | Seconds between cluster scans |
| `NAMESPACE_FILTER` / `config.namespaceFilter` | `""` (all) | Comma-separated namespaces to scan |
| `ENABLED_RULES` / `config.enabledRules` | all 13 | Which cluster rules run |
| `LOG_TAIL_LINES` / `config.logTailLines` | `100` | Log lines pulled for a failing container; `0` disables |
| `EVENT_LOOKBACK_MINUTES` / `config.eventLookbackMinutes` | `60` | Age cutoff for the generic warning-events rule |
| `NODE_REPORT_TTL_SECONDS` / `config.nodeReportTtlSeconds` | `900` | After this an agent is stale and its findings expire |
| `CLOCK_SKEW_WARN_SECONDS` / `config.clockSkewWarnSeconds` | `60` | Skew above this raises `node_clock_skew` |
| `LOG_LEVEL` / `config.logLevel` | `INFO` | Python log level |

### Authentication

| Env / Helm value | Default | Meaning |
|---|---|---|
| `AUTH_ENABLED` / `auth.enabled` | `true` | Set false to disable sign-in entirely |
| `AUTH_USERS_FILE` | `/etc/k8s-defect-bot/users.json` | Registry mounted from a Secret |
| `AUTH_USERS` | unset | Registry as inline JSON instead of a file |
| — / `auth.users` | `[]` | Entries the chart writes into a Secret; empty generates an admin |
| — / `auth.existingSecret` | `""` | Your own Secret (needs a `users.json` key) — **use this in production** |
| `SESSION_TTL_SECONDS` / `auth.sessionTtlSeconds` | `43200` | Absolute session lifetime |
| `SESSION_IDLE_TIMEOUT_SECONDS` / `auth.sessionIdleTimeoutSeconds` | `3600` | Idle timeout |
| `SESSION_COOKIE_SECURE` / `auth.sessionCookieSecure` | `false` | **Set true when serving over HTTPS** |
| `LOGIN_MAX_ATTEMPTS` / `auth.loginMaxAttempts` | `5` | Failures before lockout |
| `LOGIN_LOCKOUT_SECONDS` / `auth.loginLockoutSeconds` | `300` | Lockout duration |

### Notifications

| Env / Helm value | Default | Meaning |
|---|---|---|
| `NOTIFY_ENABLED` / `notifications.enabled` | `false` | Master switch |
| `SMTP_HOST` / `notifications.smtp.host` | `""` | Required when enabled |
| `SMTP_PORT` / `notifications.smtp.port` | `587` | 587 with STARTTLS, 465 with SSL |
| `SMTP_STARTTLS` / `notifications.smtp.starttls` | `true` | Upgrade after EHLO |
| `SMTP_SSL` / `notifications.smtp.ssl` | `false` | Implicit TLS instead |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | unset | From a Secret; omit for an unauthenticated relay |
| `SMTP_FROM` / `notifications.from` | `k8s-defect-bot@example.com` | Must be verified in SES |
| `NOTIFY_MIN_SEVERITY` / `notifications.minSeverity` | `critical` | Cluster-wide floor; users can only be stricter |
| `NOTIFY_DIGEST_INTERVAL_SECONDS` / `notifications.digestIntervalSeconds` | `3600` | How often digests flush |
| `NOTIFY_MAX_EMAILS_PER_HOUR` / `notifications.maxEmailsPerHour` | `10` | Per-recipient ceiling |
| `NOTIFY_BASELINE_FIRST_SCAN` / `notifications.baselineFirstScan` | `true` | Stay quiet on the first pass after a restart |
| `NOTIFY_MAX_DEFECTS_PER_EMAIL` / `notifications.maxDefectsPerEmail` | `25` | Truncation point |

### Node agent

| Env / Helm value | Default | Meaning |
|---|---|---|
| `AGENT_INTERVAL_SECONDS` / `nodeAgent.intervalSeconds` | `120` | Seconds between check cycles |
| `ENABLED_NODE_CHECKS` / `nodeAgent.enabledChecks` | 10 checks | Which checks run |
| `DISK_WARN_PERCENT` / `.thresholds.diskWarnPercent` | `80` | Disk/inode warning threshold |
| `DISK_CRITICAL_PERCENT` / `.thresholds.diskCriticalPercent` | `90` | Disk/inode critical threshold |
| `MEMORY_WARN_PERCENT` / `.thresholds.memoryWarnPercent` | `85` | Memory-used warning threshold |
| `LOAD_PER_CPU_WARN` / `.thresholds.loadPerCpuWarn` | `2.0` | 5-min load per CPU |
| `PID_WARN_PERCENT` / `.thresholds.pidWarnPercent` | `80` | PID usage |
| `CONNTRACK_WARN_PERCENT` / `.thresholds.conntrackWarnPercent` | `80` | Conntrack usage |
| `KUBELET_HEALTHZ_URL` / `nodeAgent.kubeletHealthzUrl` | `http://127.0.0.1:10248/healthz` | Kubelet health endpoint |
| `CONTAINER_RUNTIME_SOCKET` / `nodeAgent.containerRuntimeSocket` | `/run/containerd/containerd.sock` | Runtime socket |
| `DNS_PROBE_HOST` / `nodeAgent.dnsProbeHost` | `kubernetes.default.svc.cluster.local` | Name resolved by the DNS check |

### Claude

| Env / Helm value | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` / `llm.provider` | `none` | `none`, `claude_cli`, or `anthropic_api` |
| `CLAUDE_MODEL` / `llm.model` | `opus` | `opus`, `sonnet`, `haiku`, `fable`, or a full model ID |
| `LLM_MAX_DEFECTS_PER_SCAN` / `llm.maxDefectsPerScan` | `5` | Cap on enriched defects per scan |
| `CLAUDE_TIMEOUT_SECONDS` / `llm.timeoutSeconds` | `120` | Per-call timeout |
| — / `llm.writableHome` | `false` | Mount a writable HOME; required for in-cluster `claude_cli` |

---

## API reference

```
GET  /                                    dashboard
GET  /login   POST /login   GET /logout   session sign-in
GET  /api/me                              the signed-in user
GET  /api/summary                         scan totals and agent fleet health
GET  /api/defects[?severity=&namespace=&type=&source=&node=]
GET  /api/defects/{id}
GET  /api/nodes                           agent status, staleness, clock skew
POST /api/scan                            scan now                       (admin)
GET  /api/users                           roster, never any hashes       (admin)
GET  /api/notifications                   delivery status and history    (admin)
POST /api/notifications/test              prove the SMTP path            (admin)
POST /api/notifications/flush             flush pending digests          (admin)
POST /api/agent/report                    node-agent intake (its own shared token)
GET  /healthz   /readyz                   always public, for probes
```

Everything except the health probes, the login form, and the agent intake needs a
session cookie or an API token:

```bash
curl -H "Authorization: Bearer kdb_..." https://defect-bot.internal.example.com/api/defects
```

Mint a token with `python3 -m app.auth new-token` and put the hash in that
person's registry entry.

---

# Part 5 — When something is wrong

## Troubleshooting

### Collector pod is `CrashLoopBackOff`

```bash
kubectl -n "$NAMESPACE" logs deploy/"$RELEASE" --previous
```

- `exec format error` → wrong architecture. Rebuild for the ARCH in
  `kubectl get nodes -o wide`.
- `ImagePullBackOff` **on EKS** → the node's instance role lacks ECR pull
  permission. The managed-node-group default role includes
  `AmazonEC2ContainerRegistryReadOnly`; a custom role may not.
- `ImagePullBackOff` **on bare metal**, with a side-loaded image → almost always
  one of three things: the image was imported into containerd's default
  namespace instead of `k8s.io`, `pullPolicy` is `Always` so the kubelet ignores
  the local copy, or the image simply isn't on *that* node. Check on the node:
  ```bash
  sudo ctr -n k8s.io images ls | grep k8s-defect-bot     # k3s: sudo k3s ctr images ls
  ```
- `ImagePullBackOff` with `x509: certificate signed by unknown authority` → your
  registry's CA isn't trusted by the container runtime. That is a node-level
  trust store change, not something the chart can fix.

### Dashboard says "Cluster appears unreachable"

Zero API calls succeeded. Check RBAC:

```bash
kubectl auth can-i list pods --all-namespaces \
  --as=system:serviceaccount:"$NAMESPACE":"$RELEASE"
```

Should print `yes`. If not, the ClusterRoleBinding didn't apply.

### `/readyz` returns 503

Expected until the first scan finishes; the probe allows about two minutes. If it
never becomes ready, the scan is failing rather than slow — read the logs.

### A node is missing from `/api/nodes`

In order of likelihood:

```bash
# 1. Is there an agent pod on that node at all?
kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/component=node-agent -o wide

# 2. A taint the DaemonSet doesn't tolerate (only if you disabled tolerateAll)
kubectl describe node <node> | grep -A5 Taints

# 3. Can it reach the collector?
kubectl -n "$NAMESPACE" logs -l app.kubernetes.io/component=node-agent --tail=30
```

`could not reach collector at ...` with `networkPolicy.enabled: true` almost
always means **`networkPolicy.nodeCidrs` is wrong or missing**. The agents use
`hostNetwork`, so they arrive from the node's address and a pod selector will not
match them. On bare metal, compare the CIDRs against the INTERNAL-IP column of
`kubectl get nodes -o wide` — a node added later on a different subnet is the
usual cause of one node going quiet while the rest report.

### The agent DaemonSet won't start: `violates PodSecurity`

The namespace enforces the `baseline` or `restricted` policy, which forbids the
`hostNetwork` and hostPath mounts the agent needs:

```bash
kubectl -n "$NAMESPACE" get events --field-selector reason=FailedCreate
kubectl label namespace "$NAMESPACE" pod-security.kubernetes.io/enforce=privileged
kubectl -n "$NAMESPACE" rollout restart ds "$RELEASE"-agent
```

### The collector pod stays `Pending`

```bash
kubectl -n "$NAMESPACE" get pods --field-selector status.phase=Pending
kubectl -n "$NAMESPACE" describe pod <name-from-above> | tail -20
```

(Only the agent pods carry an `app.kubernetes.io/component` label, so there is no
`component=collector` selector to filter on.)

On a single-node or all-tainted cluster, `node(s) had untolerated taint` means
the collector has nowhere to run — it tolerates nothing by default, unlike the
agent. Add the toleration (commented in `values-baremetal.yaml`) or untaint the
node. `Insufficient cpu/memory` on small hardware means lowering `resources.requests`.

### `node_container_runtime` is critical on every node

Not a cluster problem — the socket path is wrong for your distribution. k3s and
RKE2 use `/run/k3s/containerd/containerd.sock`, not the default. See the table in
[Bare-metal and distribution specifics](#bare-metal-and-distribution-specifics),
confirm the real path on a node, and set `nodeAgent.containerRuntimeSocket`.

A finding on *one* node while the others are clean is the opposite: that
node's runtime is genuinely broken, and the pods on it are already in trouble.

### The Ingress or LoadBalancer Service never gets an address

On bare metal there is nothing to assign one unless you installed it:

```bash
kubectl -n "$NAMESPACE" get svc,ingress
```

`EXTERNAL-IP: <pending>` on a LoadBalancer Service means no MetalLB (or k3s/RKE2
ServiceLB) is running. An Ingress with no ADDRESS means no ingress controller, or
an `ingress.className` that matches none of the installed ones —
`kubectl get ingressclass` lists what you actually have.

### Agents log `collector rejected the report (HTTP 403)`

The agent's `AGENT_TOKEN` doesn't match the collector's. With Helm both come from
one Secret, so this means a hand-edited manifest or two different releases:

```bash
kubectl -n "$NAMESPACE" get secret "$RELEASE"-agent-token \
  -o jsonpath='{.data.token}' | base64 -d
```

### Every node reports `node_kubelet_health` as critical

The agent isn't on the host network, or your kubelet's healthz port isn't 10248:

```bash
kubectl -n "$NAMESPACE" get ds "$RELEASE"-agent \
  -o jsonpath='{.spec.template.spec.hostNetwork}'   # expect true
# on a node: sudo ss -lntp | grep kubelet
```

On bare metal also check the node's own firewall — `firewalld` or `ufw` can drop
loopback-adjacent traffic depending on how the zone is configured, and a host
firewall is something a cloud node rarely has.

### Sign-in succeeds and immediately bounces back to the login page

`auth.sessionCookieSecure: true` while serving plain HTTP. The browser accepts
the cookie and then refuses to send it back, so every subsequent request looks
unauthenticated. Either serve HTTPS or set it to `false` — the usual cause is a
NodePort deployment using the production values file unchanged.

### Every node reports `node_dns_resolution` as critical

Cluster DNS is genuinely broken, or `dnsPolicy` was changed away from
`ClusterFirstWithHostNet`:

```bash
kubectl -n kube-system get pods -l k8s-app=kube-dns
kubectl -n kube-system get endpoints kube-dns
```

### Locked out of the dashboard

The registry is the only source of accounts, so recovery is editing it:

```bash
python3 -m app.auth new-user you@example.com --role admin > /tmp/users.json
kubectl -n "$NAMESPACE" create secret generic k8s-defect-bot-users \
  --from-file=users.json=/tmp/users.json --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NAMESPACE" rollout restart deploy/"$RELEASE"
```

### Every sign-in fails with "authentication is misconfigured"

The registry didn't parse, so the collector refuses all logins rather than
falling back to open access:

```bash
kubectl -n "$NAMESPACE" get secret k8s-defect-bot-users \
  -o jsonpath='{.data.users\.json}' | base64 -d | python3 -m json.tool
```

### The dashboard header says "unauthenticated"

No users are configured. Anyone who can reach the Service has full access. See
[step 3a](#3a-the-user-registry--who-may-sign-in-and-who-gets-the-mail).

### No notification emails arrive

```bash
kubectl -n "$NAMESPACE" logs deploy/"$RELEASE" | grep -i notif
curl -H "Authorization: Bearer kdb_..." localhost:8080/api/notifications
```

- `no user has a delivery mode set` → everyone is `mode: off`.
- Sends listed as failed → read the recorded SMTP error. On SES the usual causes
  are an unverified `from` identity, still being in the SES sandbox, or using AWS
  access keys instead of SES SMTP credentials. On an internal relay they are a
  sender address the relay won't accept from a machine, or a relay that only
  permits specific source IPs — and the collector is a pod, so its source is
  whatever your CNI presents.
- `STARTTLS extension not supported` → `starttls: true` against a plain port 25
  relay. Set it false.
- Nothing listed at all → nothing has *changed* since the last scan. That is the
  intended behaviour; use the test email to prove the path.
- Egress to port 587 may simply be blocked — by a private-subnet route on EKS, or
  by an egress firewall rule on your own network.

### Too much email

Raise `notifications.minSeverity`, switch a person to `mode: digest`, mute the
type in their entry, or lower `notifications.maxEmailsPerHour` as a backstop.

### `missing_resource_limits` floods the dashboard

Expected on a cluster that hasn't adopted limits — it fires on most of
`kube-system`. Scope with `config.namespaceFilter` or drop the rule.

### Claude enrichment isn't happening

```bash
kubectl -n "$NAMESPACE" logs deploy/"$RELEASE" | grep -iE "llm|claude"
```

- `Claude CLI 'claude' not found on PATH` → you're running the base image; use
  `Dockerfile.claude`.
- `claude CLI exited 1 ... not logged in` → the token is missing or expired.
- No LLM lines at all → `llm.provider` is still `none`.

### Stale findings after fixing something

Cluster scans run every `scanIntervalSeconds`; agents report every
`intervalSeconds`. Force a scan with the "Scan now" button or `POST /api/scan`;
node findings clear on the agent's next cycle.

---

## Security notes

- **Read-only by design.** The ClusterRole grants `get` and `list` only.
- **Authentication is enforced only once users exist.** With no registry the
  dashboard is open and the collector says so loudly in its logs and header. A
  *malformed* registry locks sign-in rather than disabling it — a typo must never
  silently remove access control.
- **Credentials are stored hashed.** Passwords use scrypt with per-user salts and
  constant-time comparison; API tokens are SHA-256 and shown once. The one
  exception is the chart's bootstrap admin, whose plaintext password sits in the
  Secret so upgrades can reuse it — which is why production uses
  `auth.existingSecret` instead.
- **`viewer` is a write boundary, not a redaction boundary.** Anyone signed in
  sees pod names, images, events, and container log tails.
- **Log tails may contain secrets.** The bot reads the last 100 lines of failing
  containers, and applications sometimes log credentials. Set
  `config.logTailLines: 0` to disable log collection entirely if that matters
  more than the diagnostic value.
- **Sessions are in-memory and single-replica.** A restart signs everyone out.
- **The agent intake is token-protected**, and the agent itself has no cluster
  access — no RBAC, no mounted ServiceAccount token.
- **Notification emails carry cluster detail** — namespaces, pod and node names,
  and the first line of each root cause — to whatever mail system you point them
  at. They never include log tails. Treat the recipient list as an access-control
  decision, because it is one.
- **Claude enrichment sends data to Anthropic** when enabled: defect metadata,
  events, and up to 4000 characters of container logs. Off by default.

---

## Uninstall

```bash
helm uninstall "$RELEASE" -n "$NAMESPACE"
kubectl delete namespace "$NAMESPACE"
```

That removes the ClusterRole and ClusterRoleBinding too. Nothing persists — no
PVCs, no database, no external state.

---

## Appendix — deploying without Helm

The raw manifests exist for clusters where Helm isn't an option. They are less
convenient and have no generated secrets, so Helm is the recommended path.

```bash
sed -i "s|image: k8s-defect-bot:0.3.0|image: $IMAGE_URI|g" \
  manifests/03-deployment.yaml manifests/05-agent-daemonset.yaml

kubectl create namespace "$NAMESPACE"

# The agent token is NOT generated for you here
kubectl -n "$NAMESPACE" create secret generic k8s-defect-bot-agent-token \
  --from-literal=token="$(openssl rand -hex 32)"

# Edit the placeholder credentials first -- without a real registry the
# collector runs unauthenticated
$EDITOR manifests/06-auth-notify.yaml

kubectl apply -f manifests/
```

`manifests/` contains, in apply order: namespace, RBAC, ConfigMaps, collector
Deployment, Service, agent DaemonSet, and the users/SMTP Secrets. There is no
NetworkPolicy or Ingress — add your own.

The same manifests work on bare metal, and are in fact closer to ready there:
they already carry a bare `k8s-defect-bot:0.3.0` image name with
`imagePullPolicy: IfNotPresent`, which is exactly what a side-loaded image needs.
Skip the `sed` above entirely in that case.

The one edit to make by hand is the runtime socket, since there is no values file
to set it from — otherwise every node reports `node_container_runtime` as
critical on k3s, RKE2, MicroK8s, and CRI-O:

```bash
$EDITOR manifests/02-configmap.yaml       # CONTAINER_RUNTIME_SOCKET, line 77
```
