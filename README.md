# k8s-defect-bot

Finds common Kubernetes defects, explains what's actually wrong, and hands you
the commands to fix it. Read-only: every remediation is a command printed for a
human to run, never something the bot does to your cluster.

Two parts, one image:

- **Collector** — a single-replica Deployment that scans the cluster through the
  Kubernetes API, serves an authenticated dashboard and JSON API, and emails
  people when something changes.
- **Node agent** — a DaemonSet on every node, reporting node-local health the
  API server cannot answer (disk and inode headroom, memory, load, PIDs,
  conntrack, kubelet health, container runtime, DNS, API-server reachability).

---

## Quick start

Against whatever cluster your kubeconfig points at:

```bash
pip install -r requirements.txt
python main.py
# http://localhost:8080
```

To deploy it to a bare-metal cluster or Amazon EKS, including the DaemonSet and
Claude analysis on a Claude Pro subscription, follow **[usage.md](usage.md)**.

---

## What it detects

**From the control plane (13 rules):** CrashLoopBackOff (including init
containers), ImagePullBackOff, long-Pending pods, OOMKilled, failing probes,
high restart counts, node pressure and NotReady, unhandled warning events,
missing resource requests/limits, PVC binding failures, Services with no
endpoints, and deprecated API usage.

**From each node (10 checks + 2 collector-side):** disk and inode usage, memory
headroom, load per CPU, PID usage, conntrack table pressure, kubelet healthz,
container runtime socket, cluster DNS resolution from that node, API-server
reachability, kernel ring-buffer errors (opt-in), plus clock skew and agents
that stop reporting.

The split matters. The API server tells you a node is `NotReady` — after pods
have been evicted. The agent tells you the disk is at 84% and climbing, that the
conntrack table is nearly full, or that cluster DNS is broken *from that one
node* while CoreDNS itself looks perfectly healthy.

---

## What a finding looks like

Every defect carries a root cause, ordered remediation steps, and copy-pasteable
commands — with no LLM involved:

```
CRITICAL  oomkilled  Pod  prod/checkout-7d4f8c9b5-x2k9p

Root cause
  Container 'api' exceeded its memory limit (256Mi) and was killed by the
  kernel OOM killer.

Remediation
  1. Profile the application's real memory usage and raise the container's
     memory limit/request accordingly.
  2. Look for memory leaks or unbounded caches if usage grows steadily over
     time rather than spiking.
  3. If the limit is already generous, check for a legitimate traffic/data
     spike around the kill time.

Commands
  kubectl describe pod checkout-7d4f8c9b5-x2k9p -n prod
  kubectl top pod checkout-7d4f8c9b5-x2k9p -n prod --containers
```

Enable Claude and it reads the log tail, events, and details on top of that, and
adds what specifically broke this time.

---

## Who can see it, and who hears about it

The bot keeps **one list of people**, and it does both jobs: it decides who may
sign in, and it holds the address their notifications go to. Keeping them
together is deliberate — an access list and a mailing list maintained separately
drift apart, and you end up mailing people who left.

The registry is a JSON array in a Secret, so access is reviewable in git. There
is no signup flow, no password reset, and no user database.

```bash
python -m app.auth new-user ops@example.com --name "Ops" --role admin
```

Passwords are scrypt-hashed with a per-user salt; API tokens are stored as
SHA-256 and shown once. Roles are `admin` (can trigger scans and send test mail)
and `viewer` (read-only). Sessions are cookie-based with a 12-hour lifetime and a
1-hour idle timeout; scripts use `Authorization: Bearer kdb_...` instead.

With no users configured the dashboard stays open and says so loudly — that keeps
an upgrade from locking you out. A *malformed* registry locks the door instead of
removing it: a typo must never silently disable access control.

## Email notifications

The dashboard already shows what's wrong; what's worth mailing is **what
changed**. Each scan is diffed against the previous one, and the delta goes to
whoever asked for it: new defects, cleared defects, node agents that dropped off
or came back, and scans that started failing or recovered.

Each person picks their own severity floor, muted types, namespaces, and whether
they want `immediate` mail, a `digest`, or nothing.

Four things keep it from becoming the alerting everyone mutes: the first scan
after a restart is a silent baseline, a persistent condition is announced once
rather than every pass, a flapping defect isn't re-announced within the hour, and
each recipient has an hourly ceiling.

Any SMTP server works — Amazon SES, your own relay, or a Workspace account.
Setup is in [usage.md](usage.md#3b-smtp-credentials).

## Claude analysis on a Pro plan

Claude enrichment is **off by default** and entirely optional. When you want it,
`LLM_PROVIDER=claude_cli` shells out to the Claude Code CLI, which uses whatever
that CLI is logged into — so **your existing Claude Pro or Max subscription
works, with no API key and no per-token billing**:

```bash
LLM_PROVIDER=claude_cli CLAUDE_MODEL=sonnet python main.py
```

There's also `LLM_PROVIDER=anthropic_api` for an API key, and `none` (the
default) for heuristics only. Enrichment is capped at 5 defects per scan,
worst-first, and any failure falls back silently to the heuristic answer.
Details and the in-cluster setup are in
[usage.md](usage.md#claude-analysis-on-your-pro-plan).

---

## API

```
GET  /                                    dashboard
GET  /login  POST /login  GET /logout     session sign-in
GET  /api/me                              the signed-in user
GET  /api/summary                         scan totals and agent fleet health
GET  /api/defects[?severity=&namespace=&type=&source=&node=]
GET  /api/defects/{id}
GET  /api/nodes                           node-agent status, staleness, clock skew
POST /api/scan                            scan now                      (admin)
GET  /api/users                           roster, never any hashes      (admin)
GET  /api/notifications                   delivery status and history   (admin)
POST /api/notifications/test              prove the SMTP path           (admin)
POST /api/agent/report                    node-agent intake (its own shared token)
GET  /healthz  /readyz                    always public, for probes
```

Everything except the health probes, the login form, and the agent intake
requires a session cookie or an API token.

---

## Layout

```
main.py                  FastAPI app + background scan loop
app/                     config, models, store, routes, auth, notifications
scraper/                 Kubernetes client, cluster snapshot, 13 rules
agent/                   DaemonSet node agent and its checks
analyzer/                heuristic remediation templates + Claude providers
ui/                      server-rendered dashboard and email templates
manifests/               raw Kubernetes YAML
helm/k8s-defect-bot/     Helm chart
tests/                   pytest suite
```

No external JavaScript: a cluster-internal pod usually can't reach a CDN, so the
htmx subset the dashboard needs is vendored in `ui/static/hx-lite.js`.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Run the node agent against a local collector:

```bash
NODE_NAME=$(hostname) COLLECTOR_URL=http://localhost:8080 \
HOST_ROOT=/ HOST_PROC=/proc \
python -m agent.node_agent --once
```

State lives in memory by design — no database, no PVC — which is why the
collector runs a single replica.
