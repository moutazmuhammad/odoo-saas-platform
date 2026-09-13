{{- define "odoo-operator.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "odoo-operator.fullname" -}}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "odoo-operator.labels" -}}
app.kubernetes.io/name: {{ include "odoo-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "odoo-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "odoo-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "odoo-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "odoo-operator.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
