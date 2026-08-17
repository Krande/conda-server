{{/*
Standard helpers — name truncation per Helm-chart-best-practices,
labels block reused by every workload, secret-name resolver so the rest
of the templates don't have to ternary every time.
*/}}

{{- define "conda-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "conda-server.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "conda-server.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "conda-server.labels" -}}
helm.sh/chart: {{ include "conda-server.chart" . }}
{{ include "conda-server.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "conda-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "conda-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "conda-server.dbSelectorLabels" -}}
app.kubernetes.io/name: {{ include "conda-server.name" . }}-db
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Resolve which Secret to mount. If the operator handed us an existing
one we trust the keys match secretKeys.*; otherwise we'll create one
in templates/secret.yaml from the inline values.
*/}}
{{- define "conda-server.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "conda-server.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Image reference — falls back to the chart's appVersion if no tag is set,
which keeps a freshly-installed chart pinned to a known-good image.
*/}}
{{- define "conda-server.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{/*
Compose the database URL. Bundled-postgres path uses the in-namespace
StatefulSet's headless service hostname; external takes the URL verbatim
from values. Either way the password lands via $(DB_PASSWORD) env-sub
so the raw secret never appears in a ConfigMap or rendered manifest.
*/}}
{{- define "conda-server.dbUrl" -}}
{{- if .Values.postgresql.enabled -}}
postgresql+asyncpg://conda-server:$(DB_PASSWORD)@{{ include "conda-server.fullname" . }}-db:5432/conda-server
{{- else -}}
{{- required "Set externalDatabase.url or postgresql.enabled" .Values.externalDatabase.url -}}
{{- end -}}
{{- end -}}
