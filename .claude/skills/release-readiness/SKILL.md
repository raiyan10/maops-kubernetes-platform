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

1. **VERSION file.** Exact match to the day's target version (e.g.
   `0.3.0` for Day 3) - `cat VERSION` and compare byte-for-byte (no
   trailing newline surprises, no `v` prefix). From Day 2 onward, also
   re-run `make version-check` and confirm it independently passes -
   don't just eyeball the file.
2. **Git safety.** `git status` and `git log --oneline -5` should show
   the day's changes present but uncommitted, no new tags
   (`git tag --points-at HEAD`), and no evidence of a push (this is a
   local check, but never assume - report what you actually see). Prior
   days' tags (`v0.1.0`, `v0.2.0`) are expected to already exist and must
   never be moved or recreated.
3. **Authoritative validation actually ran.** Re-run
   `make dayN-check` for the current day (e.g. `make day3-check`) yourself
   and capture its real output - don't accept a prior summary's claimed
   pass count without independent confirmation. Report the exact
   pass/fail counts printed by every script the sequence composes
   (`scripts/manifest_check.py`, `scripts/version_check.py`,
   `scripts/context_check.py`, `scripts/cluster_check.py`,
   `scripts/scheduling_check.py`, `scripts/discovery_check.py`,
   `scripts/secret_check.py`, `scripts/smoke.py`,
   `scripts/dependency_check.py`, `scripts/scaling_check.py`,
   `scripts/rollout_check.py`, `scripts/pdb_check.py`,
   `scripts/final_state_check.py`, as applicable to the current day).
   A restoration failure surfaced by any mutating script (scaling,
   rolling update/rollback, PDB/Eviction, dependency-failure) is more
   serious than an ordinary check failure and must be called out as such
   in the report, never buried in an aggregate pass count.
4. **No leaked processes.** `ps aux | grep port-forward` should show
   nothing after validation completes.
5. **Cluster left running.** Unless there's a concrete safety reason,
   the current day's kind cluster should still exist (`kind get clusters`)
   for independent review to poke at directly - and any earlier day's
   cluster that was already running must be untouched (Day 3: verify
   both `maops-k8s-day1` and `maops-k8s-day2` are still listed).
6. **Documentation matches reality.** `README.md` commands actually work
   as written and clearly distinguish the latest *released* version from
   the current *development target*; `docs/architecture.md` describes
   the probes/resources/security/scheduling/rollout/PDB fields that are
   actually deployed; `docs/roadmap.md` still contains the intact
   seven-stage plan (Day 1 v0.1.0 through Day 7 v1.0.0), with each day's
   status accurately marked (released/frozen, in development, or
   future) - unless the user explicitly asked to change it.
7. **Scope boundaries.** Nothing from a later day leaked in early:
   check for RBAC, NetworkPolicy, PVC/StatefulSet, ServiceAccount, Helm
   files, CI workflow files, observability config, Terraform/Ansible/
   Argo CD files, cloud-provisioning code, or `HorizontalPodAutoscaler`
   (Day 3 scaling is deliberate/manual only) that the current day
   doesn't call for. A *committed Secret object* is still always
   forbidden at any day - but from Day 2 onward, a runtime-bootstrapped
   Secret existing live in the cluster is correct, not a violation.
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
