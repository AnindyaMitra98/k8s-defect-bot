"""Kubernetes API client bootstrap.

Uses in-cluster auth (ServiceAccount token) when running inside a pod, and falls
back to the local kubeconfig for development outside a cluster.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger("k8s-defect-bot.k8s_client")

_configured = False


def _ensure_config_loaded() -> None:
    global _configured
    if _configured:
        return
    try:
        config.load_incluster_config()
        logger.info("loaded in-cluster kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("loaded local kubeconfig (not running in-cluster)")
    _configured = True


@dataclass
class ApiClients:
    core: client.CoreV1Api
    apps: client.AppsV1Api
    networking: client.NetworkingV1Api
    # Deprecated API group, only used to detect clusters still running deprecated
    # Ingress objects. The class was dropped from the python client along with
    # Kubernetes 1.22, so this is None on modern installs and the
    # 'deprecated_apis' rule simply finds nothing.
    extensions: Optional[Any]


@lru_cache
def get_api_clients() -> ApiClients:
    _ensure_config_loaded()
    extensions_api = getattr(client, "ExtensionsV1beta1Api", None)
    try:
        extensions_client = extensions_api() if extensions_api else None
    except Exception:  # pragma: no cover - defensive, client instantiation shouldn't hit the network
        extensions_client = None
    return ApiClients(
        core=client.CoreV1Api(),
        apps=client.AppsV1Api(),
        networking=client.NetworkingV1Api(),
        extensions=extensions_client,
    )


def get_pod_log_tail(
    core: client.CoreV1Api,
    namespace: str,
    pod: str,
    container: Optional[str],
    tail_lines: int,
) -> Optional[str]:
    try:
        return core.read_namespaced_pod_log(
            name=pod,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            timestamps=True,
        )
    except ApiException as exc:
        logger.debug("could not fetch logs for %s/%s (%s): %s", namespace, pod, container, exc.reason)
        return None
