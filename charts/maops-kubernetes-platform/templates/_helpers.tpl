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
