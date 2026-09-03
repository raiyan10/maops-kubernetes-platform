"""
Bounded, automatically cleaned-up `kubectl port-forward` helper.

Picks a free localhost port, starts port-forward in its own process
group, waits for it to become connectable, and guarantees the process
(and its process group) is terminated on exit - even on error - so no
background kubectl process is ever left running after validation.

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
import time


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
    included) completely untouched."""

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
):
    """Yields a local port that forwards to <resource_kind>/<name>:remote_port
    (resource_kind defaults to "service"; pass "pod" to forward directly to a
    Pod, bypassing Service endpoint/readiness filtering entirely - used by
    dependency_check.py to reach a gateway Pod that the Service itself has
    stopped routing to). Cleans up on exit - on normal completion, on an
    exception, and on SIGTERM."""
    local_port = _free_port()
    cmd = [
        "kubectl",
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
