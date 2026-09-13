"""
Bounded, automatically cleaned-up `kubectl port-forward` helper.

Picks a free localhost port, starts port-forward in its own process
group, waits for it to become connectable, and guarantees the process
(and its process group) is terminated on exit - even on error - so no
background kubectl process is ever left running after validation.

DAY4-INT (batch 5 remediation): every OTHER kubectl invocation in this
project goes through `kube.run()`, which always passes an explicit
`--kubeconfig` (never relying on an ambient `KUBECONFIG` env var or the
caller's default kubeconfig/current context). This module's `kubectl
port-forward` subprocess was the one exception - it never passed
`--kubeconfig` at all, so it silently fell back to the default
`~/.kube/config`, which does not contain this project's isolated
per-day context (kind writes each day's cluster only to the explicit
`--kubeconfig` path given at `kind create cluster` time). Live day4-check
evidence: `error: context "kind-maops-k8s-day4" does not exist`, the
first time any script reached a `port_forward()` call site in a full
run. `port_forward()` below now defaults its own `--kubeconfig` to
`kube.KUBECONFIG_PATH` - the SAME configured path (with the same
`KUBECONFIG_PATH` env-var override and `Path.home()`-derived default)
every other script already uses - so every existing call site is fixed
automatically, with zero caller changes required. An explicit
`kubeconfig_path` argument remains available to override this per call,
matching `kube.run()`'s own override shape.

SIGTERM safety (DAY1-INT-M1): Python's default disposition for SIGTERM
terminates the interpreter immediately without unwinding `try/finally`
blocks (unlike SIGINT, which Python's default handler turns into a
catchable `KeyboardInterrupt`). A CI job timeout or `timeout`/`pkill`
wrapper typically kills a hung step with SIGTERM, which would otherwise
skip the `finally: _terminate(proc)` below and leak the `kubectl`
child. `_convert_sigterm_to_exception()` narrowly converts SIGTERM to a
catchable exception only for the duration of `port_forward()`'s body,
so the existing `try/finally` cleanup runs; the previous SIGTERM
disposition is restored immediately afterward, and SIGINT/other signals
are left completely untouched.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import threading
import time

import kube


class PortForwardSignalInterrupt(BaseException):
    """Raised in place of process termination when SIGTERM arrives while a
    port_forward() context is active, so its cleanup can still run."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"interrupted by signal {signum}")


@contextlib.contextmanager
def _convert_sigterm_to_exception():
    """Narrowly-scoped, reusable: while active, SIGTERM raises
    PortForwardSignalInterrupt instead of killing the interpreter outright,
    so an enclosing try/finally can still run. Always restores whatever
    SIGTERM disposition was previously installed - never left globally
    changed once this context exits. Leaves every other signal (SIGINT
    included) completely untouched.

    Thread safety (found during DAY3 remediation while adding the
    in-flight rollout sampler, which calls port_forward() from a
    background thread): `signal.signal()` raises `ValueError` when called
    from anything other than the main thread of the main interpreter -
    CPython only ever delivers process signals to the main thread in the
    first place, so a background thread installing its own handler is
    both impossible and unnecessary. Off the main thread this is a no-op
    (the enclosing try/finally in `port_forward()` below still guarantees
    `_terminate(proc)` runs on that thread regardless - only the
    SIGTERM-while-a-port-forward-is-open safety net is specific to the
    main thread). Before this guard, calling `port_forward()` from a
    background thread raised ValueError before the process was ever
    terminated, leaking the `kubectl port-forward` child on every call."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _handler(signum, _frame):
        raise PortForwardSignalInterrupt(signum)

    previous = signal.signal(signal.SIGTERM, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_connectable(port: int, timeout: float, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RuntimeError(f"port-forward process exited early (code {proc.returncode}): {stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"port-forward did not become connectable on 127.0.0.1:{port} within {timeout}s")


@contextlib.contextmanager
def port_forward(
    kube_context: str,
    namespace: str,
    name: str,
    remote_port: int,
    ready_timeout: float = 30.0,
    resource_kind: str = "service",
    kubeconfig_path: str | None = None,
):
    """Yields a local port that forwards to <resource_kind>/<name>:remote_port
    (resource_kind defaults to "service"; pass "pod" to forward directly to a
    Pod, bypassing Service endpoint/readiness filtering entirely - used by
    dependency_check.py to reach a gateway Pod that the Service itself has
    stopped routing to). Cleans up on exit - on normal completion, on an
    exception, and on SIGTERM.

    `kubeconfig_path` defaults to `kube.KUBECONFIG_PATH` (read at call
    time, not at import time, so a test/caller that overrides
    `kube.KUBECONFIG_PATH` is honored) - the same configured path, with
    the same `KUBECONFIG_PATH` env-var override and `Path.home()`-derived
    default, every other kubectl call in this project already uses via
    `kube.run()`. Pass an explicit value only to override it for one
    call; every existing caller needs no change."""
    if kubeconfig_path is None:
        kubeconfig_path = kube.KUBECONFIG_PATH
    local_port = _free_port()
    cmd = [
        "kubectl",
        "--kubeconfig",
        kubeconfig_path,
        "--context",
        kube_context,
        "-n",
        namespace,
        "port-forward",
        f"{resource_kind}/{name}",
        f"{local_port}:{remote_port}",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # own process group, so we can clean up reliably
    )
    with _convert_sigterm_to_exception():
        try:
            _wait_connectable(local_port, ready_timeout, proc)
            yield local_port
        finally:
            _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=5)
