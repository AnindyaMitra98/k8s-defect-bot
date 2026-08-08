"""Turns a detected Defect into a root-cause explanation + copy-pasteable remediation.

Heuristic templates always run and never fail the scan. Claude enrichment is a
best-effort add-on layered on top (see analyzer/llm.py): it only runs when
explicitly enabled, and any failure is swallowed so the heuristic result is
still returned.
"""
from __future__ import annotations

import logging
from typing import Callable

from app.config import Settings
from app.models import Defect

logger = logging.getLogger("k8s-defect-bot.solution_engine")


def _crashloopbackoff(d: Defect):
    container = d.details.get("container")
    last_reason = d.details.get("last_reason")
    exit_code = d.details.get("last_exit_code")
    root_cause = (
        f"Container '{container}' in pod {d.name} is repeatedly crashing after startup "
        f"(restart_count={d.details.get('restart_count')})."
    )
    if last_reason:
        root_cause += f" Last termination reason was '{last_reason}' with exit code {exit_code}."
    remediation = [
        "Check the container's recent logs to see the actual crash error.",
        "If the crash is an unhandled application error, fix the code/config and roll out a new image.",
        "If it crashes immediately due to bad configuration (env vars, mounted config/secret), verify those values.",
        "If the container legitimately exits 0 (e.g. a job-like image run as a Deployment), switch it to a Job "
        "or add a long-running process.",
    ]
    commands = [
        f"kubectl logs {d.name} -n {d.namespace} -c {container} --previous",
        f"kubectl describe pod {d.name} -n {d.namespace}",
        f"kubectl get events -n {d.namespace} --field-selector involvedObject.name={d.name} --sort-by='.lastTimestamp'",
    ]
    return root_cause, remediation, commands


def _imagepullbackoff(d: Defect):
    image = d.details.get("image")
    reason = d.details.get("reason")
    waiting_message = d.details.get("waiting_message") or ""
    root_cause = (
        f"Kubelet could not pull image '{image}' for container '{d.details.get('container')}' ({reason}). "
        f"{waiting_message}"
    ).strip()
    remediation = [
        "Verify the image name and tag are correct and that the image actually exists in the registry.",
        "If the registry is private, confirm an imagePullSecret is attached to the pod (or its ServiceAccount).",
        "Check node-to-registry network connectivity/DNS if the image exists and credentials look correct.",
        "Try pulling the image manually on a node to reproduce the failure outside Kubernetes.",
    ]
    commands = [
        f"kubectl describe pod {d.name} -n {d.namespace}",
        f"kubectl get pod {d.name} -n {d.namespace} -o jsonpath='{{.spec.imagePullSecrets}}'",
    ]
    return root_cause, remediation, commands


def _pending_pods(d: Defect):
    reason = d.details.get("reason")
    age = d.details.get("age_minutes")
    root_cause = f"Pod has not been scheduled for {age} minutes (last scheduler-related event: {reason})."
    remediation = [
        "Run 'kubectl describe pod' and read the Events section for the exact scheduling failure "
        "(insufficient CPU/memory, unmatched node selector/taint toleration, unbound PVC, etc.).",
        "If resources are insufficient, scale the node pool or reduce the pod's requests.",
        "If it's an affinity/taint issue, adjust the pod's nodeSelector/tolerations or the node's taints/labels.",
        "If it's waiting on storage, check whether the PVC is Bound.",
    ]
    commands = [
        f"kubectl describe pod {d.name} -n {d.namespace}",
        f"kubectl get events -n {d.namespace} --field-selector involvedObject.name={d.name}",
        "kubectl describe nodes | grep -A5 'Allocated resources'",
    ]
    return root_cause, remediation, commands


def _oomkilled(d: Defect):
    mem_limit = d.details.get("memory_limit") or "not set"
    root_cause = (
        f"Container '{d.details.get('container')}' exceeded its memory limit ({mem_limit}) "
        f"and was killed by the kernel OOM killer."
    )
    remediation = [
        "Profile the application's real memory usage and raise the container's memory limit/request accordingly.",
        "Look for memory leaks or unbounded caches if usage grows steadily over time rather than spiking.",
        "If the limit is already generous, check for a legitimate traffic/data spike around the kill time.",
    ]
    commands = [
        f"kubectl describe pod {d.name} -n {d.namespace}",
        f"kubectl top pod {d.name} -n {d.namespace} --containers",
    ]
    return root_cause, remediation, commands


def _failing_probes(d: Defect):
    root_cause = (
        "The container's liveness/readiness probe is failing, so kubelet is restarting it "
        "or removing it from Service endpoints."
    )
    remediation = [
        "Confirm the probe's path/port match what the application actually serves.",
        "Check whether the app needs a longer initialDelaySeconds or a startupProbe before it can answer probes.",
        "Check application logs around the probe-failure timestamps for slow startup or dependency failures.",
    ]
    container_flag = f" -c {d.details.get('container')}" if d.details.get("container") else ""
    commands = [
        f"kubectl describe pod {d.name} -n {d.namespace}",
        f"kubectl get pod {d.name} -n {d.namespace} -o jsonpath='{{.spec.containers[*].livenessProbe}}'",
        f"kubectl logs {d.name} -n {d.namespace}{container_flag}",
    ]
    return root_cause, remediation, commands


def _high_restart_count(d: Defect):
    root_cause = (
        f"Container '{d.details.get('container')}' has restarted {d.details.get('restart_count')} times, "
        f"indicating an intermittent crash or probe failure."
    )
    remediation = [
        "Check '--previous' logs across the last few restarts to find a recurring error pattern.",
        "Correlate restarts with resource pressure (CPU throttling, memory limits) or dependency outages.",
        "If restarts are expected behavior for this workload, exclude it via namespace filtering or tune the threshold.",
    ]
    commands = [
        f"kubectl logs {d.name} -n {d.namespace} -c {d.details.get('container')} --previous",
        f"kubectl describe pod {d.name} -n {d.namespace}",
    ]
    return root_cause, remediation, commands


def _node_pressure(d: Defect):
    condition = d.details.get("condition")
    root_cause = (
        f"Node reports {condition}=True, meaning kubelet is under resource pressure and may evict pods "
        f"or stop scheduling new ones."
    )
    remediation = [
        "Identify what's consuming the resource (disk, memory, or PID count) on the node.",
        "DiskPressure: clean up unused images/containers or expand the node's disk.",
        "MemoryPressure: identify high-memory pods and consider tighter limits or scaling out.",
        "PIDPressure: find processes/pods leaking PIDs (fork bombs, zombie processes).",
    ]
    commands = [
        f"kubectl describe node {d.name}",
        f"kubectl get pods -A --field-selector spec.nodeName={d.name} -o wide",
    ]
    return root_cause, remediation, commands


def _node_not_ready(d: Defect):
    root_cause = f"Node's Ready condition is not True ({d.details.get('reason')}: {d.details.get('message')})."
    remediation = [
        "Check kubelet health and connectivity on the node (systemctl status kubelet, node/system logs).",
        "Check for a network partition between the node and the control plane.",
        "If the node is being drained/replaced intentionally, this can be safely ignored.",
    ]
    commands = [
        f"kubectl describe node {d.name}",
        f"kubectl get events -A --field-selector involvedObject.name={d.name}",
    ]
    return root_cause, remediation, commands


def _warning_events(d: Defect):
    root_cause = (
        f"Kubernetes emitted a Warning event ({d.details.get('reason')}) for this {d.kind.lower()} "
        f"that isn't covered by a more specific check above."
    )
    remediation = [
        "Read the full event message and the relevant controller's logs for context.",
        "Cross-reference the event reason with Kubernetes documentation for that resource/controller.",
    ]
    ns_flag = f" -n {d.namespace}" if d.namespace else ""
    commands = [
        f"kubectl describe {d.kind.lower()} {d.name}{ns_flag}",
        f"kubectl get events{ns_flag} --field-selector involvedObject.name={d.name} --sort-by='.lastTimestamp'",
    ]
    return root_cause, remediation, commands


def _missing_resource_limits(d: Defect):
    root_cause = (
        "One or more containers don't declare CPU/memory requests and/or limits. This makes "
        "scheduling and OOM/throttling behavior unpredictable and can starve other workloads on the node."
    )
    remediation = [
        "Set explicit requests and limits for every container based on observed usage "
        "(kubectl top, or a VerticalPodAutoscaler recommender).",
        "Requests should reflect steady-state usage; limits should leave headroom for spikes.",
    ]
    commands = [
        f"kubectl top pod {d.name} -n {d.namespace} --containers",
        f"kubectl set resources deployment <owning-deployment> -n {d.namespace} "
        f"--requests=cpu=100m,memory=128Mi --limits=cpu=500m,memory=256Mi",
    ]
    return root_cause, remediation, commands


def _pvc_binding_failures(d: Defect):
    phase = d.details.get("phase")
    sc = d.details.get("storage_class") or "(default)"
    root_cause = f"PersistentVolumeClaim is {phase}; no PersistentVolume has been bound to it (storageClass={sc})."
    remediation = [
        "Confirm a StorageClass with that name exists and its provisioner is healthy.",
        "Check that the requested size/access mode is satisfiable by an available PV (static provisioning) "
        "or that the dynamic provisioner has capacity.",
        "Check the storage provisioner's controller logs for allocation errors.",
    ]
    commands = [
        f"kubectl describe pvc {d.name} -n {d.namespace}",
        "kubectl get storageclass",
        f"kubectl get events -n {d.namespace} --field-selector involvedObject.name={d.name}",
    ]
    return root_cause, remediation, commands


def _service_port_mismatch(d: Defect):
    root_cause = (
        "The Service's selector doesn't match any Ready pod, or the matching pods aren't exposing "
        "the targetPort -- so the Service currently has zero usable endpoints."
    )
    remediation = [
        "Verify the Service's spec.selector labels exactly match the labels on the intended pods.",
        "Verify spec.ports[].targetPort matches a containerPort the pod actually listens on.",
        "Confirm the target pods are Running and passing readiness probes (NotReady pods are excluded).",
    ]
    commands = [
        f"kubectl get endpoints {d.name} -n {d.namespace}",
        f"kubectl get pods -n {d.namespace} --show-labels",
        f"kubectl describe svc {d.name} -n {d.namespace}",
    ]
    return root_cause, remediation, commands


def _deprecated_apis(d: Defect):
    root_cause = (
        f"This {d.kind} was created via the deprecated API '{d.details.get('deprecated_api_version')}', "
        f"which may be unavailable in a future Kubernetes release."
    )
    remediation = [
        f"Recreate the manifest using apiVersion: {d.details.get('replacement')} instead.",
        "Use 'kubectl-convert' (krew plugin) to auto-migrate the manifest, then re-apply it.",
    ]
    commands = [
        f"kubectl get {d.kind.lower()} {d.name} -n {d.namespace} -o yaml > {d.name}.yaml",
        f"kubectl-convert -f {d.name}.yaml --output-version {d.details.get('replacement')}",
    ]
    return root_cause, remediation, commands


# --------------------------------------------------------------------------
# Node-agent findings (DaemonSet -- see agent/checks.py)
# --------------------------------------------------------------------------


def _node_disk_usage(d: Defect):
    path = d.details.get("path")
    root_cause = (
        f"Filesystem {path} on node {d.name} is {d.details.get('used_percent')}% full "
        f"({d.details.get('free_gb')} GiB free). Past the kubelet eviction threshold the node "
        f"reports DiskPressure, stops accepting pods, and begins evicting running ones."
    )
    remediation = [
        "Identify the consumer: container images, exited containers, or a pod writing to emptyDir/hostPath.",
        "Prune unused images and stopped containers on the node (crictl rmi --prune).",
        "If the disk is genuinely undersized, grow the EBS volume and expand the filesystem, "
        "or move the node group to a larger disk in its launch template.",
        "For log-driven growth, check that container log rotation is configured on the kubelet.",
    ]
    commands = [
        f"kubectl describe node {d.name} | grep -A6 Conditions",
        f"kubectl get pods -A --field-selector spec.nodeName={d.name} -o wide",
        "# on the node (SSM/ssh):  sudo du -xh / | sort -rh | head -30",
        "# on the node:            sudo crictl rmi --prune",
    ]
    return root_cause, remediation, commands


def _node_inode_usage(d: Defect):
    root_cause = (
        f"Filesystem {d.details.get('path')} on node {d.name} has used "
        f"{d.details.get('used_percent')}% of its inodes. The disk can look near-empty and "
        f"still fail every write with ENOSPC once inodes are exhausted."
    )
    remediation = [
        "Find directories with huge numbers of small files (often container logs or a cache volume).",
        "Prune unused images/containers, which each consume many inodes.",
        "If a workload legitimately creates millions of small files, give it a dedicated volume "
        "provisioned with a higher inode count.",
    ]
    commands = [
        f"kubectl get pods -A --field-selector spec.nodeName={d.name} -o wide",
        "# on the node:  df -i",
        "# on the node:  sudo find /var/lib -xdev -type f | cut -d/ -f1-5 | sort | uniq -c | sort -rn | head",
    ]
    return root_cause, remediation, commands


def _node_memory_available(d: Defect):
    root_cause = (
        f"Node {d.name} has only {d.details.get('available_percent')}% of memory available "
        f"({d.details.get('available_mb')} MiB). Kubelet evicts pods below its eviction threshold, "
        f"and the kernel OOM killer may fire first on whichever process is largest."
    )
    remediation = [
        "Find the largest consumers on this node and compare their usage against their limits.",
        "Pods without memory limits are the usual cause -- they can consume everything the node has.",
        "Reduce scheduling pressure by lowering replicas, adding nodes, or moving to a larger instance type.",
        "Check for a memory leak if usage grows steadily rather than spiking with traffic.",
    ]
    commands = [
        f"kubectl top pods -A --sort-by=memory | head -20",
        f"kubectl get pods -A --field-selector spec.nodeName={d.name} -o wide",
        f"kubectl describe node {d.name} | grep -A8 'Allocated resources'",
    ]
    return root_cause, remediation, commands


def _node_load_average(d: Defect):
    root_cause = (
        f"Node {d.name} has a 5-minute load average of {d.details.get('load5')} across "
        f"{d.details.get('cpu_count')} CPUs ({d.details.get('load_per_cpu')} per CPU). Sustained "
        f"load above 1.0 per CPU means processes are queueing for CPU time, which shows up as "
        f"latency and probe timeouts across every pod on the node."
    )
    remediation = [
        "Check whether one workload is monopolising CPU, or whether the node is simply overcommitted.",
        "Look for CPU-throttled containers (limits set too low cause thrash rather than clean queueing).",
        "Add nodes or raise CPU requests so the scheduler stops packing this node.",
        "High load with low CPU usage means I/O wait -- check disk latency instead.",
    ]
    commands = [
        f"kubectl top pods -A --sort-by=cpu | head -20",
        f"kubectl describe node {d.name} | grep -A8 'Allocated resources'",
        "# on the node:  top -b -n1 | head -20",
    ]
    return root_cause, remediation, commands


def _node_pid_usage(d: Defect):
    root_cause = (
        f"Node {d.name} is using {d.details.get('used_percent')}% of its PID limit "
        f"({d.details.get('pids')} of {d.details.get('pid_max')}). PID exhaustion stops the node "
        f"forking any new process -- including kubelet's own health checks."
    )
    remediation = [
        "Find the pod leaking processes; zombie children and unreaped subprocesses are the usual cause.",
        "Add an init process (shareProcessNamespace or tini) to containers that spawn children.",
        "Set a per-pod PID limit on the kubelet (--pod-max-pids) so one workload cannot exhaust the node.",
    ]
    commands = [
        f"kubectl get pods -A --field-selector spec.nodeName={d.name} -o wide",
        "# on the node:  ps -eLf | wc -l",
        "# on the node:  ps -eo stat,ppid,pid,comm | grep -w defunct | head",
    ]
    return root_cause, remediation, commands


def _node_conntrack_usage(d: Defect):
    root_cause = (
        f"Node {d.name} has {d.details.get('used_percent')}% of its netfilter conntrack table in use "
        f"({d.details.get('count')} of {d.details.get('max')}). When the table fills, the kernel drops "
        f"new connections -- seen from applications as random connection timeouts and DNS failures."
    )
    remediation = [
        "Identify the workload opening the most connections; short-lived connections without keep-alive are typical.",
        "Raise net.netfilter.nf_conntrack_max on the node (via the node group's user data or a tuning DaemonSet).",
        "Enable connection reuse/keep-alive in the offending client, which reduces table churn far more than tuning does.",
    ]
    commands = [
        f"kubectl get pods -A --field-selector spec.nodeName={d.name} -o wide",
        "# on the node:  sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max",
        "# on the node:  sudo conntrack -L | awk '{print $4}' | sort | uniq -c | sort -rn | head",
    ]
    return root_cause, remediation, commands


def _node_kubelet_health(d: Defect):
    root_cause = (
        f"The kubelet health endpoint on node {d.name} did not return OK "
        f"({d.details.get('error') or d.details.get('status')}). An unhealthy kubelet stops "
        f"reporting node status, and the node goes NotReady shortly after."
    )
    remediation = [
        "Check the kubelet service and its logs on the node.",
        "On EKS, confirm the node can still reach the API server endpoint and that its "
        "instance role / aws-auth mapping is intact.",
        "If the kubelet is wedged rather than crashed, restarting it is usually safe -- cordon and drain first.",
    ]
    commands = [
        f"kubectl describe node {d.name}",
        "# on the node:  sudo systemctl status kubelet",
        "# on the node:  sudo journalctl -u kubelet -n 200 --no-pager",
    ]
    return root_cause, remediation, commands


def _node_container_runtime(d: Defect):
    root_cause = (
        f"The container runtime socket {d.details.get('socket')} is not present or not usable on "
        f"node {d.name}. Without it the kubelet cannot start, stop, or inspect any container."
    )
    remediation = [
        "Check that containerd is running on the node.",
        "Confirm the socket path matches the kubelet's --container-runtime-endpoint.",
        "If the runtime is dead, the node needs a restart or replacement -- cordon and drain it first.",
    ]
    commands = [
        f"kubectl cordon {d.name}",
        "# on the node:  sudo systemctl status containerd",
        "# on the node:  sudo crictl info",
    ]
    return root_cause, remediation, commands


def _node_dns_resolution(d: Defect):
    root_cause = (
        f"Node {d.name} could not resolve {d.details.get('host')} via cluster DNS "
        f"({d.details.get('error')}). Every pod on this node that looks up a Service by name "
        f"will fail the same way."
    )
    remediation = [
        "Check that CoreDNS pods are Running and that its Service has endpoints.",
        "On EKS, confirm the node's security group allows UDP/TCP 53 to the CoreDNS pods.",
        "Check conntrack pressure on this node -- a full table drops DNS packets specifically.",
        "Verify /etc/resolv.conf inside a pod on this node points at the cluster DNS Service IP.",
    ]
    commands = [
        "kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide",
        "kubectl -n kube-system get endpoints kube-dns",
        f"kubectl get pods -A --field-selector spec.nodeName={d.name} -o wide",
    ]
    return root_cause, remediation, commands


def _node_apiserver_reachable(d: Defect):
    root_cause = (
        f"Node {d.name} cannot open a connection to the Kubernetes API server at "
        f"{d.details.get('endpoint')} ({d.details.get('error')}). The kubelet will stop renewing "
        f"its node lease and the node will be marked NotReady."
    )
    remediation = [
        "On EKS, check the cluster security group allows 443 from the node group's security group.",
        "For a private-endpoint cluster, confirm the VPC's DNS resolution and the endpoint's private hosted zone.",
        "Check for a NAT gateway or route table change if this started suddenly across many nodes.",
    ]
    commands = [
        f"kubectl describe node {d.name}",
        "aws eks describe-cluster --name <cluster> --query 'cluster.resourcesVpcConfig'",
        "# on the node:  curl -k -m 5 https://$KUBERNETES_SERVICE_HOST/healthz",
    ]
    return root_cause, remediation, commands


def _node_kernel_errors(d: Defect):
    root_cause = (
        f"The kernel ring buffer on node {d.name} contains {d.details.get('match_count')} recent "
        f"error line(s) matching '{d.details.get('pattern')}'. These usually precede a visible "
        f"failure -- OOM kills, blocked tasks, or filesystem corruption."
    )
    remediation = [
        "Read the matched lines to identify which subsystem is failing.",
        "OOM kills name the victim process -- map it back to a pod and raise its memory limit.",
        "Repeated 'task blocked for more than N seconds' points at storage latency, not the workload.",
        "Filesystem or EBS errors mean the node should be cordoned, drained, and replaced.",
    ]
    commands = [
        f"kubectl cordon {d.name}",
        "# on the node:  sudo dmesg -T | tail -100",
        f"kubectl get pods -A --field-selector spec.nodeName={d.name} -o wide",
    ]
    return root_cause, remediation, commands


def _node_clock_skew(d: Defect):
    root_cause = (
        f"Node {d.name}'s clock differs from the collector's by "
        f"{d.details.get('skew_seconds')}s. Clock skew breaks TLS certificate validation, "
        f"token expiry checks, and makes correlating logs across nodes unreliable."
    )
    remediation = [
        "Check that chrony/ntpd is running and synchronised on the node.",
        "On EKS, nodes should sync to the Amazon Time Sync Service at 169.254.169.123.",
        "Confirm the node's security group and NACLs allow outbound UDP 123.",
    ]
    commands = [
        "# on the node:  chronyc tracking",
        "# on the node:  timedatectl status",
    ]
    return root_cause, remediation, commands


def _node_agent_unreachable(d: Defect):
    root_cause = (
        f"No report received from the node agent on {d.name} within the TTL. Either the agent "
        f"pod is not running there, or it cannot reach the collector Service."
    )
    remediation = [
        "Check the DaemonSet has a pod scheduled on that node and that it is Running.",
        "A node with taints the DaemonSet does not tolerate will silently have no agent.",
        "Check the agent pod's logs for connection errors to the collector Service.",
    ]
    commands = [
        "kubectl -n k8s-defect-bot get pods -l app.kubernetes.io/component=node-agent -o wide",
        f"kubectl -n k8s-defect-bot logs -l app.kubernetes.io/component=node-agent --field-selector spec.nodeName={d.name}",
        f"kubectl describe node {d.name} | grep -A5 Taints",
    ]
    return root_cause, remediation, commands


TEMPLATES: dict[str, Callable[[Defect], tuple[str, list[str], list[str]]]] = {
    "crashloopbackoff": _crashloopbackoff,
    "imagepullbackoff": _imagepullbackoff,
    "pending_pods": _pending_pods,
    "oomkilled": _oomkilled,
    "failing_probes": _failing_probes,
    "high_restart_count": _high_restart_count,
    "node_pressure": _node_pressure,
    "node_not_ready": _node_not_ready,
    "warning_events": _warning_events,
    "missing_resource_limits": _missing_resource_limits,
    "pvc_binding_failures": _pvc_binding_failures,
    "service_port_mismatch": _service_port_mismatch,
    "deprecated_apis": _deprecated_apis,
    # node-agent findings
    "node_disk_usage": _node_disk_usage,
    "node_inode_usage": _node_inode_usage,
    "node_memory_available": _node_memory_available,
    "node_load_average": _node_load_average,
    "node_pid_usage": _node_pid_usage,
    "node_conntrack_usage": _node_conntrack_usage,
    "node_kubelet_health": _node_kubelet_health,
    "node_container_runtime": _node_container_runtime,
    "node_dns_resolution": _node_dns_resolution,
    "node_apiserver_reachable": _node_apiserver_reachable,
    "node_kernel_errors": _node_kernel_errors,
    "node_clock_skew": _node_clock_skew,
    "node_agent_unreachable": _node_agent_unreachable,
}


def generate_solution(defect: Defect, settings: Settings) -> Defect:
    """Fills in the heuristic root cause / remediation / commands. Never fails."""
    template = TEMPLATES.get(defect.type)
    if template:
        root_cause, remediation, commands = template(defect)
        defect.root_cause = root_cause
        defect.remediation = remediation
        defect.commands = commands
    else:
        defect.root_cause = defect.message
        defect.remediation = ["Investigate using kubectl describe/logs/events for this resource."]
        defect.commands = []
    return defect


def enrich_batch(defects: list[Defect], settings: Settings) -> int:
    """Best-effort Claude pass over a batch of already-solved defects.

    Separated from generate_solution so the heuristic answer is complete before
    any model call happens -- enrichment can then be slow, throttled, or absent
    without changing what the dashboard shows.
    """
    from analyzer.llm import enrich

    try:
        return enrich(defects, settings)
    except Exception:
        logger.warning("LLM enrichment pass failed; keeping heuristic results", exc_info=True)
        return 0
