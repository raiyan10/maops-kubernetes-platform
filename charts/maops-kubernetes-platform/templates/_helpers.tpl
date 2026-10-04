{{/*
Day 6: resource NAMES in this chart are stable, un-templated literals
(maops-gateway, maops-app, maops-state, maops-diagnostics) - see
values.yaml's header comment. These helpers only produce the standard
app.kubernetes.io/* LABEL set, mirroring exactly what k8s/base's Day 5
Kustomize objects already rendered, with app.kubernetes.io/instance now
driven by .Release.Name (expected to be "maops-kubernetes-platform-day6")
and app.kubernetes.io/managed-by switched from "kustomize" to "Helm".
*/}}

{{- define "maops.name" -}}
maops-kubernetes-platform
{{- end -}}

{{/*
Common labels, shared by every object this chart renders. Callers pass
"component" as the local scope's own extra dict entry (see the
"maops.componentLabels" wrapper below) - Helm template "define" blocks
can't take multiple named arguments directly, so component-scoped
labels compose this base with one extra line rather than a second
near-duplicate template.
*/}}
{{- define "maops.labels" -}}
app.kubernetes.io/name: {{ include "maops.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: {{ include "maops.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
Component-scoped labels: usage is
  {{ include "maops.componentLabels" (dict "root" $ "component" "gateway") }}
*/}}
{{- define "maops.componentLabels" -}}
{{ include "maops.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
DAY8 (v1.0.0): StatefulSet volumeClaimTemplates labels. volumeClaimTemplates
are IMMUTABLE once a StatefulSet exists - the API server rejects any change
- so these labels must NOT follow .Chart.AppVersion / .Chart.Version (as
maops.componentLabels does). They are frozen at the values the released
v0.7.0 chart created (version 0.7.0, chart maops-kubernetes-platform-0.7.0),
so a 1.0.0 upgrade changes only the Pod template (a normal rolling update of
maops-state-0 onto the SAME PVC) and never the StatefulSet's identity or its
claim. They describe the claim template's origin, not the running version.
*/}}
{{- define "maops.claimTemplateLabels" -}}
app.kubernetes.io/name: {{ include "maops.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: "0.7.0"
app.kubernetes.io/part-of: {{ include "maops.name" .root }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: maops-kubernetes-platform-0.7.0
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Selector labels: the minimal, stable subset every Service/PDB/
NetworkPolicy/topologySpreadConstraints selector keys on - name,
instance, component only, never the version/part-of/managed-by labels
above (which change on every upgrade and must never be part of a
selector). Usage is identical to maops.componentLabels.
*/}}
{{- define "maops.selectorLabels" -}}
app.kubernetes.io/name: {{ include "maops.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Istio AuthorizationPolicy source principal for a ServiceAccount -
usage: {{ include "maops.principal" (dict "namespace" "maops-platform" "serviceAccount" "maops-gateway") }}
*/}}
{{- define "maops.principal" -}}
cluster.local/ns/{{ .namespace }}/sa/{{ .serviceAccount }}
{{- end -}}

{{/*
DAY7: pinned-build guard. Every Day 7 stage file sets
build.requirePinnedTags=true, so a Day 7 render FAILS unless every
workload image tag names one verified build by its content:
<appVersion>-cfg-<64-hex image CONFIG digest>. The tag is written by
scripts/day7_build.py (a values overlay passed as a second -f after the
stage file) and is the digest kind a kind node's containerd reports, so
a changed build ALWAYS changes the gateway/app/state (and candidate) Pod
templates - a rebuilt image can never hide behind the mutable
<appVersion> tag again. Defaults (false) leave the Day 6 render
unchanged. Rendered output is always empty - it only ever calls `fail`.
Usage: {{ include "maops.validatePinnedBuild" . }}
*/}}
{{- define "maops.validatePinnedBuild" -}}
{{- $build := .Values.build | default dict -}}
{{- if $build.requirePinnedTags -}}
{{- $pattern := printf "^%s-cfg-[0-9a-f]{64}$" (regexQuoteMeta .Chart.AppVersion) -}}
{{- range $name := list "gateway" "app" "state" -}}
{{- $tag := toString (index $.Values.images $name).tag -}}
{{- if not (regexMatch $pattern $tag) -}}
{{- fail (printf "build.requirePinnedTags=true: images.%s.tag must be a pinned build tag %s-cfg-<64-hex config digest> (pass the verified build overlay from scripts/day7_build.py as a second -f), got %q" $name $.Chart.AppVersion $tag) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
DAY7: fail-closed validation of the candidate/routing state model. The
values schema (values.schema.json) already rejects every one of these
states; this template-level guard repeats the checks so they still hold
under `helm template --skip-schema-validation`, and adds the two checks
JSON Schema cannot express (weights summing to exactly 100, and the
candidate message differing from the stable message). Rendered output
of this helper is always empty - it only ever calls `fail`.
Usage: {{ include "maops.validateDay7State" . }}
*/}}
{{- define "maops.validateDay7State" -}}
{{- $mode := .Values.routing.mode -}}
{{- $candidate := .Values.candidate | default dict -}}
{{- $enabled := $candidate.enabled | default false -}}
{{- if not (has $mode (list "stable" "candidate" "weighted")) -}}
{{- fail (printf "routing.mode must be exactly one of stable|candidate|weighted (never empty or defaulted), got %q" (toString $mode)) -}}
{{- end -}}
{{- $hasStableWeight := hasKey .Values.routing "stableWeight" -}}
{{- $hasCandidateWeight := hasKey .Values.routing "candidateWeight" -}}
{{- if and (ne $mode "stable") (not $enabled) -}}
{{- fail (printf "routing.mode=%s requires candidate.enabled=true (no candidate Service would exist to route to)" $mode) -}}
{{- end -}}
{{- if eq $mode "weighted" -}}
{{- if not (and $hasStableWeight $hasCandidateWeight) -}}
{{- fail "routing.mode=weighted requires both routing.stableWeight and routing.candidateWeight" -}}
{{- end -}}
{{- $sw := .Values.routing.stableWeight -}}
{{- $cw := .Values.routing.candidateWeight -}}
{{- range $label, $w := dict "routing.stableWeight" $sw "routing.candidateWeight" $cw -}}
{{- if not (or (kindIs "float64" $w) (kindIs "int64" $w) (kindIs "int" $w)) -}}
{{- fail (printf "%s must be an integer, got %v" $label $w) -}}
{{- end -}}
{{- if ne (toString $w) (toString (int $w)) -}}
{{- fail (printf "%s must be an integer, got %v" $label $w) -}}
{{- end -}}
{{- if or (lt (int $w) 1) (gt (int $w) 99) -}}
{{- fail (printf "%s must be between 1 and 99 in weighted mode, got %v" $label $w) -}}
{{- end -}}
{{- end -}}
{{- if ne (add (int $sw) (int $cw)) 100 -}}
{{- fail (printf "routing.stableWeight + routing.candidateWeight must equal exactly 100, got %v + %v" $sw $cw) -}}
{{- end -}}
{{- else if or $hasStableWeight $hasCandidateWeight -}}
{{- fail (printf "routing.stableWeight/routing.candidateWeight are only valid with routing.mode=weighted (mode is %s)" $mode) -}}
{{- end -}}
{{- if $enabled -}}
{{- if eq ($candidate.config.appMessage | default "") .Values.gateway.config.appMessage -}}
{{- fail "candidate.config.appMessage must differ from gateway.config.appMessage - it is how candidate responses are identified" -}}
{{- end -}}
{{- end -}}
{{- if and $candidate.faultInjection $candidate.faultInjection.failReadiness -}}
{{- if not $enabled -}}
{{- fail "candidate.faultInjection.failReadiness requires candidate.enabled=true" -}}
{{- end -}}
{{- if ne $mode "stable" -}}
{{- fail (printf "candidate.faultInjection.failReadiness is only allowed with routing.mode=stable (got %s) - a deliberately unready candidate is never a route backend" $mode) -}}
{{- end -}}
{{- end -}}
{{- end -}}
