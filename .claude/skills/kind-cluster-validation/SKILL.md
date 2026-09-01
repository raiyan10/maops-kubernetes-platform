---
name: kind-cluster-validation
description: Create/use the project's kind cluster and prove real Kubernetes behavior - node readiness, server version, Deployment availability, replica/endpoint counts, ConfigMap consumption, bounded port-forward HTTP checks, and controller-reconciliation proof via pod deletion. Use whenever asked to validate against a live cluster, debug a failing rollout, or prove the workload actually runs.
---

# kind cluster validation

Real-cluster validation for the maops-kubernetes-platform project's kind
cluster, `maops-k8s-day1` (context `kind-maops-k8s-day1`). This is the
live-cluster counterpart to `manifest-validation` (static) and
`workload-security-validation` (security-specific) - it proves runtime
behavior via `kubectl -o json` and real HTTP calls, never by re-reading
the manifest.

## Sequence (mirrors the Makefile)

```bash
make cluster-create   # idempotent: kind create cluster --config kind/cluster.yaml
make image-build       # docker build -t maops-kubernetes-platform:0.1.0 -f app/Dockerfile app/
make image-load        # kind load docker-image ... --name maops-k8s-day1
make deploy             # kubectl apply -k k8s/base
make rollout-check     # scripts/cluster_check.py - checks 1-10, 12-13 below
make smoke              # scripts/smoke.py - check 11 below
make controller-check  # scripts/reconcile_check.py - check 14 below
```

`make day1-check` runs the full sequence (plus `tool-check`, `test`, and
`manifest-check`) in order and is the authoritative one-shot validation.

## What each real check proves

`scripts/cluster_check.py` (`make rollout-check`):

1. Cluster has exactly the expected node(s), all Ready.
2. `kubectl version -o json` server `gitVersion` == `v1.36.1` exactly.
3. Namespace `maops-platform` exists.
4. Deployment `maops-app` reaches `status.conditions[Available]=True`
   (bounded wait, not an instant check).
5-6. `spec.replicas == 2` and `status.readyReplicas == 2`.
7. Both pods individually report `Ready=True`.
8-9. Service is `ClusterIP` and has exactly 2 ready endpoint addresses.
10. ConfigMap's `APP_MESSAGE` value matches what `kubectl exec`
    (via the Python interpreter directly - the distroless image has no
    shell) reads from the live process's environment.
12-13. Live pod's actual UID/GID (via `kubectl exec ... os.getuid()`) and
    the API server's recorded `securityContext` fields.

`scripts/smoke.py` (`make smoke`) - check 11: opens a bounded, auto-
cleaned-up port-forward (`scripts/portforward.py` - picks a free local
port, waits for connectability, guarantees termination in `finally`) and
performs real HTTP GETs against `/`, `/livez`, `/readyz`, `/config`,
asserting HTTP 200 and valid JSON on each.

`scripts/reconcile_check.py` (`make controller-check`) - check 14: records
both pod UIDs, deletes **exactly one** pod (never creates a replacement
manually), waits for the Deployment to reconcile back to 2/2 Ready,
confirms one genuinely new UID appeared while the untouched pod's UID
survived, then re-runs the HTTP checks to prove the Service still routes
correctly afterward.

## Debugging a failure

Don't guess - pull real evidence:

```bash
kubectl --context kind-maops-k8s-day1 -n maops-platform describe deployment/maops-app
kubectl --context kind-maops-k8s-day1 -n maops-platform describe pod <name>
kubectl --context kind-maops-k8s-day1 -n maops-platform get events --sort-by=.lastTimestamp
kubectl --context kind-maops-k8s-day1 -n maops-platform logs <pod>
```

Fix the root cause (manifest, image, probe timing, resource sizing) and
re-run only the affected `make` target - don't re-run the whole sequence
unnecessarily, and don't loosen a script's assertions to force a pass.

## Cleanup discipline

- `make cluster-delete` deletes **only** `maops-k8s-day1` - never run
  `docker system prune` or delete unrelated clusters/resources.
- After any manual `kubectl port-forward` debugging session, confirm
  nothing was left running: `ps aux | grep port-forward`.
- Leave the cluster running after validation unless there's a concrete
  safety reason to tear it down - it's expected to be available for
  independent review.
