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
   `0.1.0` for Day 1) - `cat VERSION` and compare byte-for-byte (no
   trailing newline surprises, no `v` prefix).
2. **Git safety.** `git status` and `git log --oneline -5` should show
   the day's changes present but uncommitted, no new tags
   (`git tag --points-at HEAD`), and no evidence of a push (this is a
   local check, but never assume - report what you actually see).
3. **Authoritative validation actually ran.** Re-run
   `make day1-check` (or the current day's equivalent target) yourself
   and capture its real output - don't accept a prior summary's claimed
   pass count without independent confirmation. Report the exact
   pass/fail counts printed by `scripts/manifest_check.py`,
   `scripts/cluster_check.py`, `scripts/smoke.py`, and
   `scripts/reconcile_check.py`.
4. **No leaked processes.** `ps aux | grep port-forward` should show
   nothing after validation completes.
5. **Cluster left running.** Unless there's a concrete safety reason,
   the kind cluster should still exist (`kind get clusters`) for
   independent review to poke at directly.
6. **Documentation matches reality.** `README.md` commands actually work
   as written; `docs/architecture.md` describes the probes/resources/
   security fields that are actually deployed; `docs/roadmap.md` still
   contains the intact seven-stage plan (Day 1 v0.1.0 through Day 7
   v1.0.0) unless the user explicitly asked to change it.
7. **Scope boundaries.** Nothing from a later day leaked in early:
   check for Secret, RBAC, NetworkPolicy, PVC/StatefulSet, Helm files,
   CI workflow files, observability config, Terraform/Ansible/Argo CD
   files, or cloud-provisioning code that the current day doesn't call
   for.
8. **Claimed counts are verifiable.** Re-count agents
   (`ls .claude/agents/*.md | wc -l`), skills
   (`ls -d .claude/skills/*/ | wc -l`), and unit tests
   (`python3 -m unittest discover -s tests -v 2>&1 | tail -5`) rather
   than repeating a previously stated number.

## Reporting

Produce a PASS/FAIL line per checklist item, list anything intentionally
deferred to a later day (name the day/version it belongs to per
`docs/roadmap.md`), and close with an explicit statement that the work is
**ready for independent review** - never that it has been released,
shipped, tagged, or merged. If any item fails, say so plainly and do not
round up to "ready."
