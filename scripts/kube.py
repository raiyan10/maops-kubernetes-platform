"""Small kubectl subprocess/JSON helper shared by the real-cluster validation scripts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

CLUSTER_NAME = "maops-k8s-day5"
CONTEXT = f"kind-{CLUSTER_NAME}"
NAMESPACE = "maops-platform"
# DAY5: dedicated namespace for validation/diagnostic tooling (the
# maops-diagnostics ServiceAccount and ephemeral probe Pods used by
# rbac_check.py/networkpolicy_check.py) - kept separate from
# maops-platform so NetworkPolicy default-deny in the application
# namespace never has to account for validation traffic originating
# FROM inside that same namespace.
VALIDATION_NAMESPACE = "maops-day5-validation"

# DAY4: explicit, overridable kubeconfig path - every kubectl call this
# module makes passes --kubeconfig explicitly (see run() below) rather
# than depending on or mutating the caller's default kubeconfig/current
# context. `KUBECONFIG_PATH` (the env var the Makefile exports) always
# wins when set; the fallback is derived from the user's own home
# directory at runtime (`Path.home()`), never a hardcoded personal path
# baked into source.
KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH") or str(Path.home() / ".kube" / f"{CLUSTER_NAME}.config")

# DAY3-INT-H2: timeout architecture.
#
# Every kubectl subprocess call in this project is bounded - a hung
# `kubectl` (e.g. an unreachable API server) must never hang a
# validation script forever. `run()` below applies DEFAULT_TIMEOUT_SECONDS
# to every ordinary read/mutate call unless a caller explicitly overrides
# it. A handful of operations are legitimately long-running by design
# (`kubectl rollout status --timeout=<n>s` chief among them) - those
# callers must compute an explicit subprocess-level timeout that
# comfortably exceeds the Kubernetes-side timeout they asked for, via
# `subprocess_timeout_for()`, rather than either hanging forever or
# being killed early by the generic default.
DEFAULT_TIMEOUT_SECONDS = 30.0
# How much longer the Python subprocess timeout must be than the
# Kubernetes-side `--timeout=<n>s` a command was given, so kubectl always
# gets the chance to report its own timeout first.
SUBPROCESS_TIMEOUT_BUFFER_SECONDS = 30.0


def subprocess_timeout_for(kubectl_side_timeout_seconds: float) -> float:
    """Returns a subprocess-level timeout that always exceeds a command's
    own `--timeout=<n>s` Kubernetes-side bound, so the subprocess timeout
    is never the one that fires first for a well-behaved kubectl."""
    return kubectl_side_timeout_seconds + SUBPROCESS_TIMEOUT_BUFFER_SECONDS


_DAY_NODE_NAME_RE = re.compile(rf"^{re.escape(CLUSTER_NAME)}-(control-plane|worker\d*)$")

GATEWAY_DEPLOYMENT = "maops-gateway"
APP_DEPLOYMENT = "maops-app"
STATE_STATEFULSET = "maops-state"
GATEWAY_SERVICE = "maops-gateway"
APP_SERVICE = "maops-app"
STATE_SERVICE = "maops-state"
STATE_HEADLESS_SERVICE = "maops-state-headless"
GATEWAY_PDB = "maops-gateway-pdb"
APP_PDB = "maops-app-pdb"

INSTANCE_LABEL = "maops-kubernetes-platform-day5"

GATEWAY_LABEL_SELECTOR = (
    "app.kubernetes.io/name=maops-kubernetes-platform,"
    f"app.kubernetes.io/instance={INSTANCE_LABEL},"
    "app.kubernetes.io/component=gateway"
)
APP_LABEL_SELECTOR = (
    "app.kubernetes.io/name=maops-kubernetes-platform,"
    f"app.kubernetes.io/instance={INSTANCE_LABEL},"
    "app.kubernetes.io/component=app"
)
STATE_LABEL_SELECTOR = (
    "app.kubernetes.io/name=maops-kubernetes-platform,"
    f"app.kubernetes.io/instance={INSTANCE_LABEL},"
    "app.kubernetes.io/component=state"
)

INTERNAL_SECRET = "maops-internal-auth"
INTERNAL_SECRET_KEY = "internal-token"
STATE_SECRET = "maops-state-auth"
STATE_SECRET_KEY = "state-token"

CONTROL_PLANE_LABEL = "node-role.kubernetes.io/control-plane"

# DAY5: ServiceAccount identities. Gateway/app/state each get their own
# purpose-built ServiceAccount (automountServiceAccountToken: false,
# no RBAC binding - application Pods never talk to the Kubernetes API).
# maops-diagnostics is the one identity that DOES receive a token, so
# rbac_check.py can exercise real `kubectl auth can-i`/API-call
# authorization from inside a live Pod running as it.
GATEWAY_SERVICE_ACCOUNT = "maops-gateway"
APP_SERVICE_ACCOUNT = "maops-app"
STATE_SERVICE_ACCOUNT = "maops-state"
DIAGNOSTICS_SERVICE_ACCOUNT = "maops-diagnostics"
DIAGNOSTICS_ROLE = "maops-diagnostics-reader"
DIAGNOSTICS_ROLE_BINDING = "maops-diagnostics-reader-binding"

# DAY5: NetworkPolicy peer identity - a probe Pod exercising the
# validation-client -> gateway allow (and the validation-client -> app /
# validation-client -> state denies) carries this component label,
# scoped to VALIDATION_NAMESPACE.
VALIDATION_CLIENT_LABEL_SELECTOR = "app.kubernetes.io/component=validation-client"
DIAGNOSTICS_LABEL_SELECTOR = "app.kubernetes.io/component=diagnostics"


def run(*args: str, check: bool = True, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Runs `kubectl --context CONTEXT <args>`, bounded by `timeout`
    seconds at the subprocess level (DAY3-INT-H2). `timeout=None` is never
    passed by any caller in this project - every kubectl invocation is
    bounded, either by this default or by an explicit longer timeout
    computed via `subprocess_timeout_for()` for a command that legitimately
    runs long (e.g. `rollout status --timeout=180s`).

    Raises `subprocess.TimeoutExpired` (a normal, catchable exception - the
    child process is killed by the stdlib before this raises, so this can
    never hang past `timeout`) if the command does not complete in time.
    Callers that perform a mutation or a restoration/rollback step must
    catch this alongside `subprocess.CalledProcessError` and convert it
    into a recorded failure rather than letting it propagate uncaught.
    """
    cmd = ["kubectl", "--kubeconfig", KUBECONFIG_PATH, "--context", CONTEXT, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


def get_json(*args: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
    result = run(*args, "-o", "json", timeout=timeout)
    return json.loads(result.stdout)


def wait_until(predicate, timeout: float, interval: float = 2.0, description: str = "condition"):
    """Poll `predicate` until it signals success or `timeout` elapses.

    Sentinel contract: `predicate` returns `None` to mean "not ready yet,
    keep retrying" and anything else (including falsy values like `0`,
    `""`, or `[]`) to mean "success - return this value". This lets a
    predicate report a legitimately falsy successful result without it
    being mistaken for "not ready".
    """
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value is not None:
                return value
        except Exception as exc:  # noqa: BLE001 - surfaced in the final timeout message
            last_error = exc
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for: {description} (last error: {last_error})")


def verify_context() -> None:
    """Fail closed (DAY3) if the live cluster this process is about to talk
    to isn't actually Day 5's isolated cluster.

    Every kubectl call in this project already passes an explicit
    `--context` flag (never relying on whatever the ambient
    `kubectl config current-context` happens to be), which rules out one
    class of "wrong cluster" mistake by construction. This function
    covers the remaining one: the context existing in kubeconfig at all,
    and actually resolving to the expected `maops-k8s-day5` kind cluster
    (identified by its node names, which kind derives from the cluster
    name) rather than some other cluster a stale/renamed context happens
    to point at. Every live validation/mutation script calls this first,
    before touching anything.
    """
    result = run("config", "get-contexts", "-o", "name", check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"could not list kubeconfig contexts: {stderr if stderr else result}")
    contexts = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if CONTEXT not in contexts:
        raise RuntimeError(
            f"expected kube context {CONTEXT!r} not found in kubeconfig (found: {contexts}) - "
            "refusing to proceed against an unverified cluster"
        )

    nodes_result = run("get", "nodes", "-o", "json", check=False)
    if nodes_result.returncode != 0:
        stderr = (nodes_result.stderr or "").strip()
        raise RuntimeError(f"context {CONTEXT!r} exists but cluster is unreachable: {stderr if stderr else nodes_result}")
    nodes = json.loads(nodes_result.stdout).get("items", [])
    node_names = [n.get("metadata", {}).get("name", "") for n in nodes]
    # DAY3-INT-M2: a bare startswith(f"{CLUSTER_NAME}-") prefix check would
    # wrongly accept a prefix-collision cluster/node name such as
    # "maops-k8s-day5-staging-control-plane" (which does start with
    # "maops-k8s-day5-"). Anchor the full node name against kind's actual
    # naming convention instead: "<cluster>-control-plane" or
    # "<cluster>-worker[N]" and nothing else.
    if not node_names or not all(_DAY_NODE_NAME_RE.match(name) for name in node_names):
        raise RuntimeError(
            f"node names {node_names} do not all belong to expected cluster {CLUSTER_NAME!r} - "
            "refusing to proceed: this context may point at the wrong cluster"
        )
