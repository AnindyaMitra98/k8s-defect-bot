# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows
.venv/Scripts/python -m pytest tests/ -q                                    # full suite
.venv/Scripts/python -m pytest tests/test_rules.py::test_defect_ids_are_stable_across_scans -q

python main.py                       # collector against the current kubeconfig context, :8080
DEV_RELOAD=1 python main.py          # uvicorn autoreload

# Node agent against a local collector (Linux only -- the checks read /proc and /)
NODE_NAME=$(hostname) COLLECTOR_URL=http://localhost:8080 HOST_ROOT=/ HOST_PROC=/proc \
  python -m agent.node_agent --once

python -m app.auth new-user ops@example.com --name Ops --role admin  # users.json entry
python -m app.auth hash-password / new-token                        # individual credentials

docker build -t k8s-defect-bot:0.3.0 .                    # collector + agent (one image)
docker build -f Dockerfile.claude -t k8s-defect-bot:0.3.0-claude .  # + Claude Code CLI
helm upgrade --install k8s-defect-bot helm/k8s-defect-bot -f values-production.yaml
```

There is no pytest config file; `tests/conftest.py` puts the repo root on `sys.path`.
Deployment procedure lives in [usage.md](usage.md); the EC2/k3s test environment in
[terraform/](terraform/).

## Architecture

One image, two entrypoints. The **collector** (`main.py`, single-replica Deployment)
scans the control plane and serves the dashboard/API. The **node agent**
(`python -m agent.node_agent`, DaemonSet) runs node-local checks and POSTs them to
`/api/agent/report`. Both share `app/config.py` and `app/models.py`.

**Two independent defect sources, one store.** `app/store.py` holds cluster defects
(wholesale replaced by each scan) and node defects (upserted per node, TTL-expired).
State is in memory on purpose — no DB, no PVC — which is why the collector must stay
at one replica.

**Scan pipeline** (`scraper/cluster_scanner.py:scan`), order matters:
gather snapshot → `run_all_rules` → fetch pod log tails → `generate_solution`
(heuristics, always) → append missing-agent defects → `enrich_batch` (Claude, optional).
Every defect has a complete heuristic answer before any model call, so a slow or
failing LLM never degrades the result. `perform_scan()` runs the blocking k8s client
via `asyncio.to_thread` under `_scan_lock`, so the background loop and an operator's
`POST /api/scan` can't overlap.

**Defect ids are identity-derived, not content-derived** (`make_defect_id`): type +
kind + namespace + name + component. They must stay stable across scans — the notifier's
diff and the store's dict keying both depend on it. `component` is what keeps two
crash-looping containers in one pod from collapsing into one entry.

**Four parallel registries must stay in sync.** A defect type is a registry key
everywhere it appears:

| Add a cluster rule | Add a node check |
|---|---|
| `scraper/rules.py` fn + `REGISTRY` | `agent/checks.py` fn + `REGISTRY` |
| `DEFAULT_ENABLED_RULES` in `app/config.py` | `DEFAULT_ENABLED_NODE_CHECKS` in `app/config.py` |
| `TEMPLATES` in `analyzer/solution_engine.py` | `TEMPLATES` in `analyzer/solution_engine.py` |

A missing `TEMPLATES` entry doesn't raise — it silently degrades to a generic
"investigate with kubectl" remediation. `app/routes.py` builds the dashboard's type
filter by unioning both registries.

**Nothing in the detection path is allowed to crash the loop.** Each rule, each node
check, each API call (`ClusterScanner._safe`), the LLM pass, and the notification pass
are individually wrapped. The counterpart is `_scan_error()`: swallowed API failures
are turned into an explicit summary message, because an empty result set from an
unreachable cluster would otherwise render as a confident all-clear.

**Singletons configured at startup.** `store`, `auth`, and `notifier` are module-level
instances; `lifespan` in `main.py` calls `.configure(settings)` on each. `get_settings()`
is `lru_cache`d — tests construct `Settings(...)` directly instead (see the `settings`
fixture). `scraper/cluster_scanner.py` imports `analyzer` and `app.store` inside
functions to break an import cycle; keep it that way.

**Auth is a Secret-backed registry, not a database** (`app/auth.py`). One user list does
double duty: who may sign in, and where their mail goes. No users configured → the
dashboard stays open and warns loudly (an upgrade must not lock you out); a *malformed*
registry → all sign-ins refused. `app/routes.py` splits `public_router` (health probes,
login, agent intake with its own shared `AGENT_TOKEN`) from `router`, which carries
`Depends(require_user)` for everything else.

**Notifications mail the delta, not the state** (`app/notify.py`). Four dampers, each
load-bearing: the first scan after startup is a silent baseline, only scan-error
*transitions* are reported, a re-appearing defect is suppressed within the cooldown,
and each recipient has an hourly ceiling.

**LLM enrichment** (`analyzer/llm.py`): `none` (default), `claude_cli` (shells out to
the Claude Code CLI — works off a Pro/Max subscription, all tools denied so the call is
one text-in/text-out turn), or `anthropic_api` (API key). Enrichment appends to the
heuristic answer rather than replacing it.

## Conventions

- **No external JavaScript.** A cluster-internal pod usually can't reach a CDN, so the
  htmx subset the dashboard needs is vendored in `ui/static/hx-lite.js`. Server-rendered
  Jinja partials in `ui/templates/_*.html` are swapped in by htmx.
- Jinja and static paths are relative (`ui/templates`, `ui/static`), so the app must run
  from the repo root / `/app`.
- `.gitattributes` forces LF. This repo is developed on Windows but every artifact is
  consumed by Linux, and a CRLF cloud-init script fails on the node with an unhelpful
  "bad interpreter".
- Comments here explain *why* a decision was made, especially where the obvious
  implementation would be wrong. Match that when editing.
- Remediation is always a command printed for a human. The bot never writes to the
  cluster; its RBAC is read-only and should stay that way.
