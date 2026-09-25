---
name: release-readiness
description: Assess whether a day's implementation of maops-kubernetes-platform is genuinely ready for independent review - VERSION correctness, git safety (no commit/tag/push), full day-N-check evidence, no leaked processes, documentation accuracy, and scope boundaries. Never performs the commit/tag/push/release itself. Use at the end of a day's implementation, or when asked "is this ready."
---

# Release readiness (Day N)

Readiness assessment for a day's deliverable in the maops-kubernetes-
platform portfolio project. This skill produces a report; it never
commits, tags, pushes, or creates a release - those steps are explicitly
reserved for the user, and every day's task instructions in this project
say so directly.

## Checklist

1. **VERSION file.** Exact match to the day's target version (current
   active target: `0.6.0` for Day 6, merged to `main` as a local kind
   reference platform but NOT yet tagged or released -
   `0.5.0` remains the latest RELEASED baseline; use whatever
   `docs/roadmap.md` names for the day actually under review) -
   `cat VERSION` and compare byte-for-byte (no trailing newline
   surprises, no `v` prefix). Also re-run `make version-check` and
   confirm it independently passes - as of Day 6 this checks BOTH the
   frozen k8s/base target (must stay `0.5.0`) and the live release
   target (VERSION, Helm chart version/appVersion, image tags, Day 6
   identities, pinned infrastructure versions, all `0.6.0`/their
   pinned value) - don't just eyeball the file.
2. **Git safety.** `git status` and `git log --oneline -5` should show
   the day's changes present but uncommitted, no new tags
   (`git tag --points-at HEAD`), and no evidence of a push (this is a
   local check, but never assume - report what you actually see). Prior
   days' tags (as of the current released baseline: `v0.1.0` through
   `v0.5.0`) are expected to already exist and must never be moved or
   recreated; Day 6 has no tag yet, and this skill never creates one.
3. **Authoritative validation actually ran.** Re-run
   `make dayN-check` for the current day (e.g. `make day6-check`) yourself
   and capture its real output - don't accept a prior summary's claimed
   pass count without independent confirmation. Report the exact
   pass/fail counts printed by every script the sequence composes
   (`scripts/manifest_check.py`, `scripts/version_check.py`,
   `scripts/helm_check.py`, `scripts/context_check.py`,
   `scripts/ambient_workload_check.py`, `scripts/cluster_check.py`, `scripts/scheduling_check.py`,
   `scripts/discovery_check.py`, `scripts/secret_check.py`,
   `scripts/rbac_check.py`, `scripts/networkpolicy_check.py`,
   `scripts/cni_check.py`, `scripts/mesh_status.py`,
   `scripts/mesh_check.py`, `scripts/gateway_check.py`,
   `scripts/storage_hardening_check.py`, `scripts/smoke.py`,
   `scripts/dependency_check.py`, `scripts/scaling_check.py`,
   `scripts/rollout_check.py`, `scripts/pdb_check.py`,
   `scripts/state_check.py`, `scripts/persistence_check.py`,
   `scripts/retention_check.py`, `scripts/helm_lifecycle_check.py`,
   `scripts/final_state_check.py`, as applicable to the current day).
   **If the task scope explicitly authorized static validation only**
   (no live cluster contact), the equivalent evidence is `make
   ci-check`'s real output, and the report must say plainly that
   `day6-check`/every live-cluster target was deliberately deferred,
   never silently omitted or implied to have passed. A restoration
   failure surfaced by any mutating script (scaling, rolling update/
   rollback, PDB/Eviction, dependency-failure, persistence/retention,
   the Helm upgrade/rollback lifecycle check) is more serious than an
   ordinary check failure and must be called out as such in the report,
   never buried in an aggregate pass count.
4. **No leaked processes.** `ps aux | grep port-forward` should show
   nothing after validation completes.
5. **Cluster left running (only applies once a live sequence was
   actually run).** Unless there's a concrete safety reason, the
   current day's kind cluster should still exist (`kind get clusters`)
   for independent review to poke at directly. An earlier day's cluster
   that was already running must be untouched by the current day's
   tooling - but as of Day 5, running several multi-node kind clusters
   concurrently can exceed a WSL2 host's available capacity
   (`docs/engineering-reviews/day-05-post-release-verification.md`), so
   an earlier-day cluster being *stopped* (not deleted) is an accepted,
   disclosed limitation, not itself a readiness failure - report what
   you actually observe rather than assuming every earlier cluster must
   still be listed as running. For an implementation pass explicitly
   scoped to static validation only, this item is simply "no cluster
   was created" - confirm that with `kind get clusters` rather than
   assuming.
6. **Documentation matches reality.** `README.md` commands actually work
   as written and clearly distinguish the latest *released* version from
   the current *development target* (if any is in progress);
   `docs/architecture.md` describes the probes/resources/security/
   identity/network/scheduling/rollout/PDB/storage fields that are
   actually deployed; `docs/roadmap.md` still contains the intact
   seven-stage plan (Day 1 v0.1.0 through Day 7 v1.0.0), with each day's
   status accurately marked (released/frozen, in development, or
   future) - unless the user explicitly asked to change it.
7. **Scope boundaries.** Nothing from a later day leaked in early, and
   nothing required for the current day is missing. As of the frozen
   `v0.5.0` baseline, `k8s/base` should still show dedicated
   ServiceAccounts, the `maops-diagnostics` `Role`/`RoleBinding`, seven
   NetworkPolicy objects, and be byte-for-byte untouched (`git diff`
   against it must be empty). As of the Day 6 implementation
   (`v0.6.0`, merged, not yet released), a review should find: the Helm chart
   (`charts/maops-kubernetes-platform`) as the SOLE application
   deployment source; `k8s/day6/`'s cluster/platform support objects;
   the Gateway API with Istio as the sole controller (never a second
   Ingress-based path); Istio ambient mesh (PeerAuthentication STRICT,
   three AuthorizationPolicy objects, no waypoint); Cilium
   reconfigured for ambient coexistence; and the cluster-free GitHub
   Actions workflow - these are now **required**, not scope violations.
   Flag as violations: any Day 6 application object duplicated between
   `k8s/base` and the Helm chart, or between the Helm chart and
   `k8s/day6/`; an Ingress object or second ingress controller; a
   waypoint proxy or Cilium L7 policy; an unpinned infrastructure
   version. Nothing from Day 7 should appear yet:
   `HorizontalPodAutoscaler`, Argo Rollouts, or a Recreate/Blue-Green/
   Canary deployment strategy demonstration. Generic evergreen
   exclusions at any day: an observability stack, TLS/cert-manager, a
   cloud LoadBalancer, Terraform/Ansible/Argo CD files, and
   cloud-provisioning code. A *committed Secret object* is still always
   forbidden at any day, in `k8s/base`, the Helm chart's rendered
   output, AND `values.yaml` - a runtime-bootstrapped Secret existing
   live in the cluster is correct, not a violation.
8. **Claimed counts are verifiable.** Re-count agents
   (`ls .claude/agents/*.md | wc -l`), skills
   (`ls -d .claude/skills/*/ | wc -l`), and unit tests
   (`python3 -m unittest discover -s tests -v 2>&1 | tail -5`) rather
   than repeating a previously stated number.
9. **Earlier-day evidence is untouched.** Nothing under
   `docs/engineering-reviews/day-0N-*` for an already-released day was
   modified by the current day's work - those are historical/frozen.

## Reporting

Produce a PASS/FAIL line per checklist item, list anything intentionally
deferred to a later day (name the day/version it belongs to per
`docs/roadmap.md`), and close with an explicit statement that the work is
**ready for independent review** - never that it has been released,
shipped, tagged, or merged. If any item fails, say so plainly and do not
round up to "ready."
