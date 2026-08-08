"""Application configuration, sourced from environment variables / ConfigMap."""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_ENABLED_RULES = [
    "crashloopbackoff",
    "imagepullbackoff",
    "pending_pods",
    "oomkilled",
    "failing_probes",
    "high_restart_count",
    "node_pressure",
    "node_not_ready",
    "warning_events",
    "missing_resource_limits",
    "pvc_binding_failures",
    "service_port_mismatch",
    "deprecated_apis",
]

# Checks the DaemonSet agent runs on each node. `node_kernel_errors` needs
# privileged access to the kernel ring buffer, so it is off unless opted in.
DEFAULT_ENABLED_NODE_CHECKS = [
    "node_disk_usage",
    "node_inode_usage",
    "node_memory_available",
    "node_load_average",
    "node_pid_usage",
    "node_conntrack_usage",
    "node_kubelet_health",
    "node_container_runtime",
    "node_dns_resolution",
    "node_apiserver_reachable",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    # Scanning
    scan_interval_seconds: int = 300
    namespace_filter: Optional[str] = None  # comma-separated; empty = all namespaces
    enabled_rules: str = ",".join(DEFAULT_ENABLED_RULES)
    log_tail_lines: int = 100
    event_lookback_minutes: int = 60

    # Server
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8080
    # Shown in the dashboard title and email subjects, so a person on call can
    # tell which cluster a mail is about.
    cluster_name: str = "kubernetes"
    # Absolute URL of the dashboard, used to build links in emails. Empty omits them.
    dashboard_url: str = ""

    # ---- Authentication ----
    # Enforced as soon as at least one user is configured. With no users the
    # dashboard stays open and the collector warns loudly at startup, so an
    # existing deployment cannot lock itself out on upgrade.
    auth_enabled: bool = True
    # JSON array of users, either inline or (preferred) a file from a Secret.
    auth_users: Optional[str] = None
    auth_users_file: Optional[str] = "/etc/k8s-defect-bot/users.json"
    session_ttl_seconds: int = 43200          # 12h absolute lifetime
    session_idle_timeout_seconds: int = 3600  # 1h since last request
    session_cookie_name: str = "kdb_session"
    # Set true when served over HTTPS so the cookie is never sent in the clear.
    session_cookie_secure: bool = False
    login_max_attempts: int = 5
    login_lockout_seconds: int = 300

    # ---- Email notifications ----
    notify_enabled: bool = False
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_starttls: bool = True   # port 587
    smtp_ssl: bool = False       # port 465
    smtp_timeout_seconds: float = 20.0
    smtp_from: str = "k8s-defect-bot@example.com"
    smtp_from_name: str = "k8s-defect-bot"
    # Cluster-wide floor; a user's own min_severity can only be stricter.
    notify_min_severity: str = "critical"
    notify_digest_interval_seconds: int = 3600
    # Per-recipient ceiling, so a flapping cluster cannot mailbomb anyone.
    notify_max_emails_per_hour: int = 10
    # Treat the first scan after startup as the baseline and stay quiet, instead
    # of mailing every pre-existing defect the moment the pod restarts.
    notify_baseline_first_scan: bool = True
    notify_max_defects_per_email: int = 25

    # Node agents
    # Reports older than this are dropped from the dashboard -- a node whose agent
    # died shouldn't keep showing stale findings forever.
    node_report_ttl_seconds: int = 900
    # Shared bearer token the agents present to POST /api/agent/report.
    # Empty disables authentication (logged loudly at startup).
    agent_token: Optional[str] = None
    # Flag a node whose clock differs from the collector's by more than this.
    clock_skew_warn_seconds: float = 60.0

    # ---- Agent-side settings (read by agent/node_agent.py, not the collector) ----
    node_name: Optional[str] = None            # injected via fieldRef spec.nodeName
    collector_url: str = "http://k8s-defect-bot:80"
    agent_interval_seconds: int = 120
    enabled_node_checks: str = ",".join(DEFAULT_ENABLED_NODE_CHECKS)
    host_root: str = "/host/root"              # read-only hostPath mount of /
    host_proc: str = "/host/proc"              # read-only hostPath mount of /proc
    disk_warn_percent: float = 80.0
    disk_critical_percent: float = 90.0
    memory_warn_percent: float = 85.0
    load_per_cpu_warn: float = 2.0
    pid_warn_percent: float = 80.0
    conntrack_warn_percent: float = 80.0
    kubelet_healthz_url: str = "http://127.0.0.1:10248/healthz"
    container_runtime_socket: str = "/run/containerd/containerd.sock"
    dns_probe_host: str = "kubernetes.default.svc.cluster.local"

    # ---- LLM enrichment (optional) ----
    # "none"           -- heuristics only (default; no credentials, no cost)
    # "claude_cli"     -- shells out to the Claude Code CLI. Uses whatever that CLI
    #                     is logged into, so a Claude Pro/Max subscription works.
    # "anthropic_api"  -- Anthropic API with an API key (separate from a subscription).
    llm_provider: str = "none"
    claude_model: str = "sonnet"
    claude_cli_path: str = "claude"
    claude_timeout_seconds: float = 120.0
    anthropic_api_key: Optional[str] = None
    # Only enrich this many defects per scan, worst-first, to bound latency/usage.
    llm_max_defects_per_scan: int = 5

    @property
    def enabled_rules_set(self) -> set[str]:
        return {r.strip() for r in self.enabled_rules.split(",") if r.strip()}

    @property
    def enabled_node_checks_set(self) -> set[str]:
        return {c.strip() for c in self.enabled_node_checks.split(",") if c.strip()}

    @property
    def namespace_filter_list(self) -> Optional[list[str]]:
        if not self.namespace_filter:
            return None
        return [n.strip() for n in self.namespace_filter.split(",") if n.strip()]

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider not in ("", "none", "off", "false")


@lru_cache
def get_settings() -> Settings:
    return Settings()
