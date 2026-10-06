{{/* Labels shared by every object */}}
{{- define "telecom-app.labels" -}}
app.kubernetes.io/part-of: telecom-observability
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
environment: {{ .Values.global.environment | quote }}
{{- end }}

{{/* Full image reference. Usage: include "telecom-app.image" (dict "root" . "svc" .Values.usageApi) */}}
{{- define "telecom-app.image" -}}
{{- $tag := .svc.image.tag | default .root.Chart.AppVersion -}}
{{- if .root.Values.image.registry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" .root.Values.image.registry) .svc.image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .svc.image.repository $tag -}}
{{- end -}}
{{- end }}
