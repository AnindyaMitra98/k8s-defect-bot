# Test environment: step-by-step

A complete walkthrough, from nothing to a running k8s-defect-bot with deliberately
broken workloads for it to find. Follow it top to bottom; every command is here.

[README.md](README.md) explains what this environment is and why it's built the
way it is. This file is what you *do*.

**Time:** ~25 minutes, most of it waiting.
**Cost:** ~$0.015/hr while it exists — about 12 cents for a working session.
Nothing is billed once you destroy it, and there is no auto-shutdown, so
[Part 8](#part-8--destroy-it) is not optional.

## Contents

- [Before you start](#before-you-start)
- [If it's already running](#if-its-already-running)
- [Part 1 — Create the cluster](#part-1--create-the-cluster)
- [Part 2 — Get into the node](#part-2--get-into-the-node)
- [Part 3 — Install the bot](#part-3--install-the-bot)
- [Part 4 — Open the dashboard](#part-4--open-the-dashboard)
- [Part 5 — kubectl from your own machine](#part-5--kubectl-from-your-own-machine)
- [Part 6 — Create fake issues](#part-6--create-fake-issues)
- [Part 7 — Test the node agent](#part-7--test-the-node-agent)
- [Part 8 — Destroy it](#part-8--destroy-it)
- [Troubleshooting](#troubleshooting)
- [Reference: what triggers each rule](#reference-what-triggers-each-rule)

---

## Before you start

You need:

| | |
|---|---|
| **AWS credentials** | Any account. Permissions for EC2, VPC, and key pairs. Check with `aws sts get-caller-identity`. |
| **Terraform** | >= 1.6. `terraform version` |
| **An SSH client** | `ssh` and `scp`. Windows 10+ ships OpenSSH; macOS and Linux already have it. |
| **kubectl** | Optional — only for [Part 5](#part-5--kubectl-from-your-own-machine). Everything else runs on the node. |

You do **not** need Docker locally, a container registry, a domain name, or a
DNS record. The image is built on the node and the dashboard is reached through
`nip.io`, which resolves `<ip>.nip.io` to that IP for free.

**What you'll end up with:** one EC2 spot instance running single-node k3s, with
the collector, the node-agent DaemonSet, and a Traefik ingress in front of the
dashboard — reachable from your IP address only.

---

## If it's already running

Coming back to an environment you built earlier, or picking up after an apply you
lost track of? Everything you need is in the state file — you don't have to
remember any of it, and you should not rebuild to find out.

```bash
cd terraform
terraform output public_ip               # where the node is
terraform output -raw install_commands   # the whole runbook, your IP filled in
terraform output -raw kubeconfig_command
```

Then pick up at [Part 2](#part-2--get-into-the-node).

**If nothing responds, check for IP drift first.** The security group allows a
single `/32`, captured when you last applied, and a laptop that changed networks
no longer matches it. This is the most common cause of "it worked yesterday":

```powershell
terraform output allowed_cidr                      # what the security group allows
(Invoke-RestMethod https://checkip.amazonaws.com).Trim()   # where you are now
```

Different? `terraform apply` again — the CIDR is re-detected on every plan, and
updating the rule doesn't touch the instance.

> An apply that appears stuck may simply be running: while it holds
> `.terraform.tfstate.lock.info`, `terraform.tfstate` can sit at zero bytes and
> look empty. Let it finish before concluding the lock is stale — force-unlocking
> a live apply is how you end up with resources the state file doesn't know about.

---

## Part 1 — Create the cluster

Terraform builds the infrastructure and installs k3s. It does **not** install the
bot; that's Part 3, and you do it by hand.

```bash
cd terraform
terraform init
terraform plan
```

Read the plan before applying. Two things to check:

- `allowed_cidr` is **your** public IP as a `/32`, not `0.0.0.0/0`. Terraform
  auto-detects it. If you're behind a corporate NAT or a changing address, set it
  yourself in `terraform.tfvars` (copy `terraform.tfvars.example`).
- `Plan: 14 to add`. No NAT gateway, no load balancer, no EIP — those are the
  things that would make this expensive.
- `repo_branch` is the branch cloud-init clones onto the node, and it defaults to
  **`main`** — not the branch you happen to have checked out. If the test
  environment is being developed on a feature branch, the node gets `main`'s
  version of `terraform/`, which can be missing files this walkthrough tells you
  to use. [Part 3a](#3a-copy-the-values-file-up) covers the one that matters.

```bash
terraform apply
```

Apply takes about a minute. **The node then needs another 3–4 minutes** to finish
cloud-init: installing k3s, Docker, Helm, and cloning the source. Go and read
`terraform output next_steps` while you wait.

---

## Part 2 — Get into the node

### Windows: fix the key ACL — do this before your first ssh

This is not a warning you can defer. **SSH will not work until you do it.**

Terraform wrote the private key to `terraform/.ssh/kdb-test.pem` and asked for
mode `0600`, but **that's a no-op on NTFS** — the file keeps its inherited ACL,
which grants `Authenticated Users`, and OpenSSH refuses to use a key anyone can
read:

```
Bad permissions. Try removing permissions for user: NT AUTHORITY\Authenticated Users (S-1-5-11)
WARNING: UNPROTECTED PRIVATE KEY FILE!
Permissions for './.ssh/kdb-test.pem' are too open.
ubuntu@<ip>: Permission denied (publickey).
```

Once, in PowerShell, from the `terraform/` directory:

```powershell
icacls .\.ssh\kdb-test.pem /inheritance:r /grant:r "${env:USERNAME}:R"
```

`/inheritance:r` drops the inherited ACEs; `/grant:r` then re-grants read to just
you. Confirm it took — you want exactly one line, your account, `(R)`:

```powershell
icacls .\.ssh\kdb-test.pem
# E:\...\kdb-test.pem DESKTOP-XXXX\you:(R)
```

> **Keep the braces.** `"$env:USERNAME:R"` looks equivalent and is not:
> PowerShell parses `USERNAME:R` as the variable name inside the `env:` drive,
> finds nothing, and passes `icacls` an empty principal. The command reports
> success, grants nobody, and ssh fails with the identical error — which is a
> genuinely confusing half hour. `terraform output fix_key_permissions` prints
> the braced form with your path already filled in.

### macOS / Linux

```bash
chmod 600 .ssh/kdb-test.pem
```

### SSH in

```bash
terraform output ssh_command      # prints the line below, with your IP
ssh -i .ssh/kdb-test.pem ubuntu@<public-ip>
```

The user is `ubuntu`. If it refuses the connection, the node is still booting —
wait a minute and retry.

### Confirm the bootstrap actually finished

**Do this before anything else.** The bootstrap script runs `set -e`, so the
sentinel file only exists if every step succeeded:

```bash
test -f /opt/bootstrap.done && echo "bootstrap OK" || tail -50 /var/log/cloud-init-output.log
kubectl get nodes
```

You want one node, `Ready`:

```
NAME       STATUS   ROLES           AGE   VERSION
kdb-test   Ready    control-plane   3m    v1.36.3+k3s1
```

The version tracks whatever the k3s stable channel is serving unless you pin
`k3s_version`, so expect it to differ from the line above.

`kubectl` works without any setup here — cloud-init put `KUBECONFIG` in
`/etc/profile.d/k3s.sh` for every login shell.

If the node says `NotReady`, give it another minute. If `bootstrap.done` is
missing, read the cloud-init log; that's where the real error is.

---

## Part 3 — Install the bot

Terraform deliberately doesn't do this. Running it yourself is how you find out
whether the deployment procedure still works.

**You need two terminals**, and mixing them up is the usual way this goes wrong:

| Terminal | Where | Steps |
|---|---|---|
| **A — workstation** | `terraform/` on your own machine | 3a only |
| **B — node** | your SSH session from [Part 2](#part-2--get-into-the-node) | 3b – 3f |

Every step below is labelled. Leave both open; you'll come back to A in
[Part 5](#part-5--kubectl-from-your-own-machine).

Grab your IP once, in terminal A, and keep it in front of you:

```powershell
terraform output public_ip
```

Budget about **five minutes**, nearly all of it the image build in 3b.

### 3a. Copy the values file up

**Terminal A (workstation), from the `terraform/` directory.**

`values-test.yaml` lives here, next to the `.tf` files. Send it to the node:

```powershell
scp -i .\.ssh\kdb-test.pem values-test.yaml ubuntu@<public-ip>:~/
```

bash / zsh is the same command with forward slashes:

```bash
scp -i .ssh/kdb-test.pem values-test.yaml ubuntu@<public-ip>:~/
```

You want `values-test.yaml   100%  3143` and no other output. If this is the
first `scp` rather than `ssh` you've run, and it fails on the key, you skipped
the [ACL fix](#windows-fix-the-key-acl--do-this-before-your-first-ssh) —
`scp` uses the same key and refuses it for the same reason.

That file carries the handful of settings that are wrong-by-default on k3s — see
[README.md](README.md#notable-settings-in-values-testyaml) for what each one
prevents. Installing without it mostly works and then reports a critical defect
on every node that isn't real.

**Copy it from your workstation, not from the node's clone.** The node has a copy
of the repo, so reaching for `~/k8s-defect-bot/terraform/values-test.yaml` is the
obvious move — but cloud-init cloned `repo_branch` (default `main`), and if this
file was added on a branch that hasn't been merged yet, it simply isn't there:

```bash
ls ~/k8s-defect-bot/terraform/values-test.yaml   # on the node
# No such file or directory  ->  stale clone; scp it up, as above
```

That's a clone that predates the file, not a broken bootstrap — `scp` and carry
on. The bot's own source (`app/`, `scraper/`, `helm/`) is unaffected, so the
build and install in the next steps are still valid.

### 3b. Build the image, on the node

**Terminal B (node).** Confirm the values file landed, then build:

```bash
ls -l ~/values-test.yaml                    # from 3a; must be here before 3d

cd ~/k8s-defect-bot
git pull                                    # cloud-init cloned it at boot
sudo docker build -t k8s-defect-bot:0.3.0 .
```

Takes about two minutes on a t4g.small, and prints a wall of build output. The
line that matters is the last one:

```
Successfully tagged k8s-defect-bot:0.3.0
```

It produces an **arm64** image, which is correct — it never leaves this node.

> The node builds **what is pushed to the branch**, not your local working tree.
> If you're testing a change, push it first. `git pull` here is a shallow clone
> (`--depth 1`), so it fast-forwards the branch cloud-init cloned and nothing
> else — it will not move you to a different branch.

If the build is killed partway with no error of its own, you're out of memory;
see [Troubleshooting](#troubleshooting).

### 3c. Import the image into k3s

**Terminal B (node).** k3s doesn't use Docker's image store, so the image has to
be handed to its containerd. No registry is involved:

```bash
sudo docker save k8s-defect-bot:0.3.0 -o /tmp/kdb.tar
sudo k3s ctr images import /tmp/kdb.tar
sudo rm -f /tmp/kdb.tar
```

The import prints an `unpacking ... done` line. Verify it's really there before
moving on — if it isn't, 3d fails with `ImagePullBackOff` and no way to recover,
because there's no registry to fall back to:

```bash
sudo k3s ctr images ls | grep k8s-defect-bot
```

> That last `sudo` matters. `docker save` ran as root, so the tar belongs to
> root, and `/tmp` carries the sticky bit — a plain `rm` fails with
> `Operation not permitted`. This is the exact bug that used to break the
> automated version of this install.

### 3d. Install the chart

**Terminal B (node), from `~/k8s-defect-bot`** — the chart path is relative, so
`cd` back if you've wandered:

```bash
cd ~/k8s-defect-bot
IP=$(curl -s ifconfig.me)          # or just type your public IP
echo "$IP"                         # sanity-check it before it goes into a URL

helm upgrade --install k8s-defect-bot ./helm/k8s-defect-bot \
  --namespace k8s-defect-bot --create-namespace \
  -f ~/values-test.yaml \
  --set ingress.host=$IP.nip.io \
  --set config.dashboardUrl=http://$IP.nip.io \
  --wait --timeout 10m
```

All four of those arguments earn their place:

| Argument | Without it |
|---|---|
| `-f ~/values-test.yaml` | a false `node_container_runtime` critical, and a login loop |
| `--set ingress.host` | the ingress answers only for `k8s-defect-bot.local` |
| `--set config.dashboardUrl` | notification links point at the wrong host |
| `--wait` | returns before the pods are ready, so a failure looks like a success |

`--wait` blocks until the pods are actually ready, so when this returns with
`STATUS: deployed`, it worked. Two minutes is normal; the timeout is generous
because a slow first start is not a failure.

### 3e. Check it came up

**Terminal B (node).**

```bash
kubectl -n k8s-defect-bot get pods
```

Expect two pods, both `Running`: one collector, one node agent.

```
NAME                              READY   STATUS    RESTARTS   AGE
k8s-defect-bot-7d4b8f9c5-xxxxx    1/1     Running   0          30s
k8s-defect-bot-agent-xxxxx        1/1     Running   0          30s
```

Read the collector's startup lines rather than grepping for errors — they tell
you what it actually decided to do:

```bash
kubectl -n k8s-defect-bot logs -l app.kubernetes.io/component=collector --tail=30
```

> Select by component, not `deploy/k8s-defect-bot`. Both workloads share the
> `name` and `instance` labels, so addressing the Deployment resolves to whichever
> of the two pods kubectl picks first — and it is quite happy to hand you the
> agent's logs while you read them as the collector's.

Then confirm `values-test.yaml` actually took effect. This is the one check worth
doing every time, because the failure is silent — the install succeeds and the
dashboard simply lies to you about a broken node:

```bash
kubectl -n k8s-defect-bot get configmap k8s-defect-bot-agent \
  -o jsonpath='{.data.CONTAINER_RUNTIME_SOCKET}{"\n"}'
```

You want a **k3s** path (`/run/k3s/containerd/containerd.sock`). A kubeadm path,
or nothing at all, means the values file didn't apply — you missed `-f
~/values-test.yaml` in 3d, or scp'd it somewhere other than `~`. Re-run 3d; it's
idempotent.

### 3f. Get the sign-in password

**Terminal B (node).** `values-test.yaml` leaves `auth.users` empty on purpose, so
the chart generated an admin and stored the password in a Secret — better than
putting one on a command line where it lands in your shell history.

```bash
echo "Email:    admin@example.com"
echo -n "Password: "
kubectl -n k8s-defect-bot get secret k8s-defect-bot-users \
  -o jsonpath='{.data.generated-password}' | base64 -d; echo
```

The password survives `helm upgrade` — the chart reads it back from the live
Secret rather than rotating it under you.

---

## Part 4 — Open the dashboard

In a browser on the machine whose IP you allowed:

```
http://<public-ip>.nip.io
```

Sign in with `admin@example.com` and the password from 3f.

If it doesn't load, the cause is almost always one of two things — you're on a
different network than when you ran `terraform apply` (so the security group has
the wrong IP), or Traefik isn't ready yet. See [Troubleshooting](#troubleshooting).

**What you should see immediately, on a cluster with nothing wrong with it:** a
handful of `missing_resource_limits` warnings. k3s's own system pods — Traefik,
CoreDNS, metrics-server, local-path-provisioner — ship without resource limits,
and that rule fires on every container that lacks them. That's not a bug; it's
the noisiest rule in the set, which is exactly why `values-baremetal.yaml`
suggests dropping it on clusters that haven't adopted limits yet.

The scan interval here is **120 seconds**. There's a "Scan now" button on the
dashboard if you don't want to wait.

---

## Part 5 — kubectl from your own machine

Optional, but it makes Part 6 much more pleasant — you can create the broken
workloads from your own terminal and watch the dashboard in a browser beside it.

`terraform output -raw kubeconfig_command` prints these with your IP filled in.

**PowerShell:**

```powershell
scp -i .\.ssh\kdb-test.pem ubuntu@<ip>:/etc/rancher/k3s/k3s.yaml .\kubeconfig.yaml
(Get-Content .\kubeconfig.yaml) -replace '127.0.0.1','<ip>' | Set-Content -Encoding utf8 .\kubeconfig.yaml
$env:KUBECONFIG = "$PWD\kubeconfig.yaml"
kubectl get nodes
```

**bash / zsh:**

```bash
scp -i .ssh/kdb-test.pem ubuntu@<ip>:/etc/rancher/k3s/k3s.yaml ./kubeconfig.yaml
sed -i.bak "s/127.0.0.1/<ip>/" ./kubeconfig.yaml
export KUBECONFIG=$PWD/kubeconfig.yaml
kubectl get nodes
```

The fetched kubeconfig points at `127.0.0.1`, which is only correct *on* the node
— that's what the replace is for. No `--insecure-skip-tls-verify` is needed: k3s
put the public IP in the API server certificate as a SAN at boot.

`kubeconfig.yaml` is a live admin credential for the cluster. It's gitignored.

---

## Part 6 — Create fake issues

Now break things on purpose and confirm the bot notices.

Everything goes into a `defect-test` namespace, so cleanup is one command. Run
these from wherever your `kubectl` works — the node or your workstation.

### The 30-second smoke test

Two commands, two critical defects, no YAML:

```bash
kubectl create namespace defect-test
kubectl -n defect-test create deployment crasher  --image=busybox -- /bin/false
kubectl -n defect-test create deployment badimage --image=nginx:v-does-not-exist
```

Within about a minute, `badimage` enters `ImagePullBackOff` and `crasher` enters
`CrashLoopBackOff`. Hit "Scan now" on the dashboard, or wait for the next 120s
scan, and you should see two **critical** findings — each with a root cause, a
remediation command, and the container's log tail.

If that works, the bot is running. The rest of this section is about coverage.

### The full set

This creates one object per rule that can be reasonably forced:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: defect-test
---
# imagepullbackoff (CRITICAL) -- a tag that does not exist.
apiVersion: apps/v1
kind: Deployment
metadata: { name: badimage, namespace: defect-test }
spec:
  replicas: 1
  selector: { matchLabels: { app: badimage } }
  template:
    metadata: { labels: { app: badimage } }
    spec:
      containers:
        - name: app
          image: nginx:v-does-not-exist
---
# crashloopbackoff (CRITICAL) -- exits non-zero immediately, forever.
apiVersion: apps/v1
kind: Deployment
metadata: { name: crasher, namespace: defect-test }
spec:
  replicas: 1
  selector: { matchLabels: { app: crasher } }
  template:
    metadata: { labels: { app: crasher } }
    spec:
      containers:
        - name: app
          image: busybox
          command: ["/bin/false"]
---
# failing_probes (WARNING) -- readiness probe against a port nothing listens on.
# The kubelet emits an "Unhealthy" event, which is what the rule keys off.
apiVersion: apps/v1
kind: Deployment
metadata: { name: badprobe, namespace: defect-test }
spec:
  replicas: 1
  selector: { matchLabels: { app: badprobe } }
  template:
    metadata: { labels: { app: badprobe } }
    spec:
      containers:
        - name: app
          image: busybox
          command: ["sleep", "3600"]
          readinessProbe:
            tcpSocket: { port: 9999 }
            periodSeconds: 5
---
# oomkilled (CRITICAL) -- busybox awk grows a string until the memory cgroup
# kills it. restartPolicy Never so the terminated state sticks around to be seen.
apiVersion: v1
kind: Pod
metadata: { name: oomer, namespace: defect-test }
spec:
  restartPolicy: Never
  containers:
    - name: app
      image: busybox
      command:
        - awk
        - 'BEGIN { s = ""; while (1) { s = s sprintf("%1000000s", "") } }'
      resources:
        requests: { memory: "32Mi" }
        limits:   { memory: "32Mi" }
---
# pending_pods -- asks for more CPU than the node has, so it never schedules.
# WARNING after 5 minutes Pending, CRITICAL after 15.
apiVersion: v1
kind: Pod
metadata: { name: toobig, namespace: defect-test }
spec:
  containers:
    - name: app
      image: busybox
      command: ["sleep", "3600"]
      resources:
        requests: { cpu: "16" }
---
# pvc_binding_failures (WARNING after 5 minutes) -- a storage class that does not
# exist. Naming one explicitly matters: k3s has a default class that would bind.
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: stuck-pvc, namespace: defect-test }
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: does-not-exist
  resources:
    requests: { storage: 1Gi }
---
# service_port_mismatch (WARNING) -- a selector that matches no pod at all, so
# the Service has no endpoints. Fires on the first scan.
apiVersion: v1
kind: Service
metadata: { name: ghost, namespace: defect-test }
spec:
  selector: { app: nothing-matches-this }
  ports:
    - port: 80
      targetPort: 8080
EOF
```

Every pod above also lacks resource limits, so `missing_resource_limits` fires on
each of them as a bonus.

### What to expect, and when

| Defect type | Severity | Appears after | From |
|---|---|---|---|
| `imagepullbackoff` | critical | ~30s | `badimage` |
| `crashloopbackoff` | critical | ~1 min | `crasher` |
| `oomkilled` | critical | ~1 min | `oomer` |
| `failing_probes` | warning | ~1 min | `badprobe` |
| `service_port_mismatch` | warning | first scan | `ghost` |
| `missing_resource_limits` | warning | first scan | every test pod, plus k3s system pods |
| `pending_pods` | warning → critical | **5 min**, then 15 | `toobig` |
| `pvc_binding_failures` | warning | **5 min** | `stuck-pvc` |

The two five-minute ones have deliberate grace periods — a pod that's Pending for
ten seconds is a pod being scheduled, not a defect. Be patient rather than
assuming it's broken.

### Watching it happen

```bash
kubectl -n defect-test get pods           # the raw truth, for comparison
```

On the dashboard: filter by namespace `defect-test`, or by severity. Click any
finding to see its root cause, remediation command, and log tail.

From the API, if you'd rather (needs a session cookie or an API token, so this is
easiest from the node with a port-forward):

```bash
kubectl -n k8s-defect-bot port-forward svc/k8s-defect-bot 8080:80 &
curl -s localhost:8080/api/summary
curl -s "localhost:8080/api/defects?severity=critical"
```

### Three rules you won't be able to force

Included for honesty, so you don't waste time hunting for them:

- **`high_restart_count`** (5+ restarts) explicitly *skips* any container
  currently in `CrashLoopBackOff`, so it doesn't duplicate that finding. It shows
  up only when a scan happens to catch `crasher` mid-restart rather than
  mid-backoff. Leave the cluster running a while and it'll turn up on its own.
- **`warning_events`** is the catch-all for Warning events, and it deliberately
  excludes every reason another rule already covers (`BackOff`, `Failed`,
  `FailedScheduling`, `Unhealthy`, `OOMKilling`, `FailedMount`,
  `FailedAttachVolume`). The workloads above produce almost nothing else.
- **`deprecated_apis`** looks for `extensions/v1beta1` Ingress objects. That API
  was removed in Kubernetes 1.22, so on this cluster it can never fire. It's for
  old clusters.

`node_pressure` and `node_not_ready` come from conditions the kubelet reports.
Forcing them means genuinely filling the disk or wedging the node, which tends to
take k3s down with it. Part 7 is the safe way to exercise node-level detection.

### Clean up the fake issues

```bash
kubectl delete namespace defect-test
```

Within one or two scans the findings disappear from the dashboard. Watching them
clear is itself worth doing — it proves the store is being replaced wholesale on
each scan rather than accumulating.

---

## Part 7 — Test the node agent

The node agent is a separate detection path: a DaemonSet that reads `/proc` and
the filesystem on each node and POSTs its findings to the collector. It's the
half of the bot that a cluster scan can't tell you about.

### Confirm it's reporting at all

Dashboard → **Nodes**. You want one node, reporting recently.

```bash
kubectl -n k8s-defect-bot logs -l app.kubernetes.io/component=node-agent --tail=20
```

A node that never appears has usually failed to reach the collector — the agent
runs with `hostNetwork: true`, so it arrives from the node's own address, not a
pod IP.

### Make its checks fire, safely

Rather than filling the disk, lower the thresholds until reality crosses them.
On the node:

```bash
cd ~/k8s-defect-bot
helm upgrade k8s-defect-bot ./helm/k8s-defect-bot \
  --namespace k8s-defect-bot --reuse-values \
  --set nodeAgent.thresholds.diskWarnPercent=1 \
  --set nodeAgent.thresholds.memoryWarnPercent=1 \
  --set nodeAgent.thresholds.loadPerCpuWarn=0.01
```

Within 60 seconds (the agent's interval here) `node_disk_usage`,
`node_memory_available`, and `node_load_average` all report. That proves the whole
agent path end to end: check → POST `/api/agent/report` → store → dashboard.

Put them back:

```bash
helm upgrade k8s-defect-bot ./helm/k8s-defect-bot \
  --namespace k8s-defect-bot --reuse-values \
  --set nodeAgent.thresholds.diskWarnPercent=80 \
  --set nodeAgent.thresholds.memoryWarnPercent=85 \
  --set nodeAgent.thresholds.loadPerCpuWarn=2.0
```

### The check most likely to be genuinely wrong

`node_container_runtime` verifies the container runtime socket exists. The chart's
default path is a kubeadm path; **k3s uses a different one**, and
`values-test.yaml` overrides it for exactly that reason. If you see this check
reporting critical, you installed without `-f ~/values-test.yaml`. It's a false
positive, and it's the single most common way to make this cluster look broken
when it isn't.

---

## Part 8 — Destroy it

There is no auto-shutdown and no budget alarm. This costs money for as long as it
exists.

From `terraform/` **on your workstation**:

```bash
terraform destroy
```

Type `yes`. Everything goes: instance, EBS volume, public IP, VPC, key pair.
Nothing here holds state worth keeping — the whole environment rebuilds from
`terraform apply` in about five minutes.

Confirm nothing was left behind:

```bash
aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=k8s-defect-bot" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text
```

Empty output means you're not being billed.

> Stopping the instance instead of destroying it still bills the EBS volume, and
> a spot instance can't be stopped anyway. Destroy is the only thing that takes
> the cost to zero.

---

## Troubleshooting

**`UNPROTECTED PRIVATE KEY FILE` on Windows.**
Terraform's `file_permission = "0600"` does nothing on NTFS. Run the `icacls`
command in [Part 2](#part-2--get-into-the-node).

**SSH times out.**
Either the node is still booting (wait a minute), or your public IP changed since
`terraform apply` — a laptop moving between networks does this constantly. Check:

```bash
terraform output allowed_cidr    # what the security group allows
curl -s https://checkip.amazonaws.com
```

If they differ, `terraform apply` again; the CIDR is re-detected each plan.

**`/opt/bootstrap.done` doesn't exist.**
cloud-init failed partway. The real error is in `/var/log/cloud-init-output.log`.
Rebuild the node rather than repairing it by hand:

```bash
terraform apply -replace=aws_instance.node
```

**Pods stuck `ImagePullBackOff` on `k8s-defect-bot:0.3.0` itself.**
The image import didn't land. There's no registry, so the pod can't fall back to
pulling. Confirm and redo step 3c:

```bash
sudo k3s ctr images ls | grep k8s-defect-bot
```

**`docker build` is killed partway through.**
You're on a smaller instance than the default. `t4g.micro` has 1 GiB and runs the
cluster fine but OOMs during the build. Use `t4g.small` or larger.

**Dashboard doesn't load, but the pods are Running.**
Check the ingress got an address, and that Traefik is up:

```bash
kubectl -n k8s-defect-bot get ingress
kubectl -n kube-system get pods | grep traefik
```

Also confirm you set `--set ingress.host=<ip>.nip.io` at install time — with the
chart default the ingress answers only for `k8s-defect-bot.local`.

**Sign-in appears to succeed, then every page bounces back to login.**
`sessionCookieSecure` is true while you're on plain HTTP: the browser accepts the
cookie and then refuses to send it back. `values-test.yaml` sets it false. You
installed without it.

**Every node reports `node_container_runtime` critical.**
The k3s containerd socket override is missing. Same cause — install with
`-f ~/values-test.yaml`.

**The Nodes tab is empty.**
The agent isn't reaching the collector. Check its logs
(`kubectl -n k8s-defect-bot logs -l app.kubernetes.io/component=node-agent`). If
you enabled `networkPolicy`, that's why: the agent uses `hostNetwork`, so it
arrives from the node's address and a pod selector won't match it.
`values-test.yaml` leaves NetworkPolicy off — k3s ships Flannel, which enforces
none of it anyway.

**`helm upgrade` fails with `field is immutable` on the Deployment selector.**
You're upgrading across the change that added `app.kubernetes.io/component:
collector` to the collector's selector. A Deployment's `spec.selector` cannot be
edited in place, so the old object has to go first. Nothing is lost — the
collector holds its state in memory and rebuilds it on the next scan:

```bash
kubectl -n k8s-defect-bot delete deployment k8s-defect-bot
helm upgrade --install k8s-defect-bot ./helm/k8s-defect-bot ...   # as in 3d
```

**An empty dashboard with a scan-error banner.**
That banner is deliberate. When API calls fail, the bot says so rather than
rendering an empty result set as a confident all-clear. Read the collector logs.

---

## Reference: what triggers each rule

Exact conditions, from `scraper/rules.py`. Useful when a finding you expected
doesn't appear.

| Rule | Fires when | Severity |
|---|---|---|
| `crashloopbackoff` | A container's waiting reason is `CrashLoopBackOff` | critical |
| `imagepullbackoff` | Waiting reason is `ImagePullBackOff` or `ErrImagePull` | critical |
| `oomkilled` | Current or previous termination reason is `OOMKilled` | critical |
| `pending_pods` | Pod phase `Pending` for ≥5 min | warning; critical at 15 min |
| `failing_probes` | An `Unhealthy` event exists for the pod | warning |
| `high_restart_count` | ≥5 restarts, **and not currently in CrashLoopBackOff** | warning; critical at 20 |
| `missing_resource_limits` | Any container missing a cpu/memory limit *or* request | warning |
| `pvc_binding_failures` | PVC not `Bound`; Pending needs ≥5 min | warning; critical if `Lost` |
| `service_port_mismatch` | Service has a selector but no endpoint addresses | warning |
| `warning_events` | Any Warning event whose reason isn't already covered above | warning |
| `node_pressure` | Node reports a pressure condition | varies |
| `node_not_ready` | Node `Ready` condition isn't true | critical |
| `deprecated_apis` | An `extensions/v1beta1` Ingress exists (impossible on k8s ≥1.22) | warning |

Node-agent checks (`agent/checks.py`) are independent of all of the above and are
driven by `nodeAgent.thresholds` — see [Part 7](#part-7--test-the-node-agent).

---

## Where to go next

This environment is a test rig, not a deployment. For a real cluster:

- [`../usage.md`](../usage.md) — the full deployment runbook, EKS and bare metal
- [`../helm/k8s-defect-bot/values-baremetal.yaml`](../helm/k8s-defect-bot/values-baremetal.yaml) — production values, with the reasoning inline
- [README.md](README.md) — why this test environment is built the way it is
