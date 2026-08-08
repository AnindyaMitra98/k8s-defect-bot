{{- define "k8s-defect-bot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k8s-defect-bot.fullname" -}}
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

{{- define "k8s-defect-bot.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "k8s-defect-bot.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "k8s-defect-bot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k8s-defect-bot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "k8s-defect-bot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "k8s-defect-bot.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "k8s-defect-bot.llmSecretName" -}}
{{- if .Values.llm.existingSecret -}}
{{- .Values.llm.existingSecret -}}
{{- else -}}
{{- printf "%s-llm" (include "k8s-defect-bot.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "k8s-defect-bot.usersSecretName" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-users" (include "k8s-defect-bot.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "k8s-defect-bot.smtpSecretName" -}}
{{- if .Values.notifications.smtp.existingSecret -}}
{{- .Values.notifications.smtp.existingSecret -}}
{{- else -}}
{{- printf "%s-smtp" (include "k8s-defect-bot.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Password for the auto-generated admin, used only when auth.users is empty.
Reused from the live Secret on upgrade so a `helm upgrade` never silently
changes the password out from under whoever is using it.
*/}}
{{- define "k8s-defect-bot.defaultAdminPassword" -}}
{{- $name := printf "%s-users" (include "k8s-defect-bot.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace $name -}}
{{- if and $existing $existing.data (index $existing.data "generated-password") -}}
{{- index $existing.data "generated-password" | b64dec -}}
{{- else -}}
{{- randAlphaNum 24 -}}
{{- end -}}
{{- end -}}

{{- define "k8s-defect-bot.agentName" -}}
{{- printf "%s-agent" (include "k8s-defect-bot.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k8s-defect-bot.agentSelectorLabels" -}}
{{ include "k8s-defect-bot.selectorLabels" . }}
app.kubernetes.io/component: node-agent
{{- end -}}

{{- define "k8s-defect-bot.agentTokenSecretName" -}}
{{- if .Values.nodeAgent.existingSecret -}}
{{- .Values.nodeAgent.existingSecret -}}
{{- else -}}
{{- printf "%s-agent-token" (include "k8s-defect-bot.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Resolve the shared agent token. Precedence: explicit value, then the token
already stored in the cluster (so `helm upgrade` doesn't rotate it out from
under running agents), then a fresh random one.

`lookup` returns nothing during `helm template` / `--dry-run`, so rendering
offline always shows a freshly generated token -- that's expected, and is why
the value is only ever read back from the live Secret.
*/}}
{{- define "k8s-defect-bot.agentToken" -}}
{{- if .Values.nodeAgent.token -}}
{{- .Values.nodeAgent.token -}}
{{- else -}}
{{- $name := printf "%s-agent-token" (include "k8s-defect-bot.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace $name -}}
{{- if and $existing $existing.data (index $existing.data "token") -}}
{{- index $existing.data "token" | b64dec -}}
{{- else -}}
{{- randAlphaNum 40 -}}
{{- end -}}
{{- end -}}
{{- end -}}
