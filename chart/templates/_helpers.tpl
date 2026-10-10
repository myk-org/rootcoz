{{/*
Expand the name of the chart.
*/}}
{{- define "rootcoz.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "rootcoz.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "rootcoz.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "rootcoz.labels" -}}
helm.sh/chart: {{ include "rootcoz.chart" . }}
{{ include "rootcoz.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rootcoz
{{- end }}

{{- define "rootcoz.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rootcoz.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "rootcoz.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "rootcoz.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "rootcoz.configMapName" -}}
{{- printf "%s-config" (include "rootcoz.fullname" .) }}
{{- end }}

{{- define "rootcoz.credentialsSecretName" -}}
{{- printf "%s-secret" (include "rootcoz.fullname" .) }}
{{- end }}

{{- define "rootcoz.encryptionSecretName" -}}
{{- printf "%s-encryption-key" (include "rootcoz.fullname" .) }}
{{- end }}

{{- define "rootcoz.gcloudSecretName" -}}
{{- printf "%s-gcloud-credentials" (include "rootcoz.fullname" .) }}
{{- end }}

{{- define "rootcoz.cursorAuthSecretName" -}}
{{- printf "%s-cursor-auth" (include "rootcoz.fullname" .) }}
{{- end }}

{{- define "rootcoz.sidecarAgentSecretName" -}}
{{- printf "%s-sidecar-agent" (include "rootcoz.fullname" .) }}
{{- end }}

{{/*
Environment variable names that sidecar.agentDir.env entries must not shadow
(comma-separated; splitList in consumers). Covers every variable the chart
manages directly (Secrets, ConfigMap, Deployment env) plus sidecar/app control
variables — the credentials Secret is imported via envFrom after the ConfigMap,
so an unchecked entry would override chart-managed configuration. Shared by
validation (rootcoz.validate) and the credentials Secret merge in
rootcoz.credentialsSecretData.
*/}}
{{- define "rootcoz.reservedEnvKeys" -}}
ADMIN_KEY,GEMINI_API_KEY,ANTHROPIC_API_KEY,CURSOR_API_KEY,ROOTCOZ_ENCRYPTION_KEY,AI_PROVIDER,AI_MODEL,PUBLIC_BASE_URL,SECURE_COOKIES,LOG_LEVEL,METADATA_RULES_FILE,XDG_CONFIG_HOME,XDG_DATA_HOME,CLAUDE_CODE_USE_VERTEX,CLOUD_ML_REGION,ANTHROPIC_VERTEX_PROJECT_ID,CLOUDSDK_CONFIG,GOOGLE_APPLICATION_CREDENTIALS,PI_SIDECAR_AGENT_DIR,SIDECAR_PORT,DEV_MODE,PORT
{{- end }}

{{- define "rootcoz.pvcName" -}}
{{- printf "%s-data" (include "rootcoz.fullname" .) }}
{{- end }}

{{/*
Validate chart values: routing, AI config.
*/}}
{{- define "rootcoz.validate" -}}
{{- if and .Values.route.enabled .Values.ingress.enabled -}}
{{- fail "route.enabled and ingress.enabled are mutually exclusive — enable at most one" -}}
{{- end -}}
{{- if and .Values.ingress.enabled (not .Values.ingress.host) -}}
{{- fail "ingress.host is required when ingress.enabled is true" -}}
{{- end -}}
{{- if .Release.IsInstall -}}
{{- if or (not .Values.ai.provider) (not .Values.ai.model) -}}
{{- fail "ai.provider and ai.model are required for install" -}}
{{- end -}}
{{- end -}}
{{- /* Agent-dir env keys must not shadow chart-managed variables (they land in the
       same credentials Secret via envFrom). */ -}}
{{- range $name, $value := .Values.sidecar.agentDir.env -}}
{{- if has $name (splitList "," (include "rootcoz.reservedEnvKeys" $)) -}}
{{- fail (printf "sidecar.agentDir.env key %q collides with a chart-managed environment variable" $name) -}}
{{- end -}}
{{- end -}}
{{- if .Release.IsInstall -}}
{{- /* Lowercase before the built-in alias checks: the application lowercases
       AI_PROVIDER on load, so an uppercase alias must not bypass the
       install-time credential requirements. Custom provider IDs pass through. */ -}}
{{- $aiProvider := lower .Values.ai.provider -}}
{{- if eq $aiProvider "gemini" -}}
{{- if not .Values.ai.geminiApiKey -}}
{{- fail "ai.geminiApiKey is required when ai.provider is gemini" -}}
{{- end -}}
{{- else if eq $aiProvider "claude" -}}
{{- if and (not .Values.ai.anthropicApiKey) (not .Values.ai.vertex.enabled) -}}
{{- fail "ai.anthropicApiKey or ai.vertex.enabled is required when ai.provider is claude" -}}
{{- end -}}
{{- if and .Values.ai.vertex.enabled (not .Values.ai.vertex.projectId) -}}
{{- fail "ai.vertex.projectId is required when ai.vertex.enabled is true" -}}
{{- end -}}
{{- if and .Values.ai.vertex.enabled (not .Values.ai.vertex.serviceAccountKey) -}}
{{- fail "ai.vertex.serviceAccountKey is required when ai.vertex.enabled is true" -}}
{{- end -}}
{{- else if eq $aiProvider "cursor" -}}
{{- if and (not .Values.ai.cursor.apiKey) (not .Values.ai.cursor.authJson) -}}
{{- fail "ai.cursor.apiKey or ai.cursor.authJson is required when ai.provider is cursor" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Auto-derive SECURE_COOKIES unless explicitly set in values.
*/}}
{{- define "rootcoz.secureCookies" -}}
{{- if ne .Values.env.secureCookies "" -}}
{{- .Values.env.secureCookies -}}
{{- else if .Values.route.enabled -}}
true
{{- else if and .Values.ingress.enabled .Values.ingress.tls.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end }}

{{/*
Auto-derive PUBLIC_BASE_URL from route/ingress when not explicitly set.
*/}}
{{- define "rootcoz.publicBaseUrl" -}}
{{- if .Values.env.publicBaseUrl -}}
{{- .Values.env.publicBaseUrl -}}
{{- else if and .Values.route.enabled .Values.route.host -}}
https://{{ .Values.route.host }}
{{- else if and .Values.ingress.enabled .Values.ingress.host -}}
{{- if .Values.ingress.tls.enabled -}}
https://{{ .Values.ingress.host }}
{{- else -}}
http://{{ .Values.ingress.host }}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "rootcoz.imageTag" -}}
{{- default "latest" .Values.image.tag -}}
{{- end }}

{{/*
Container image reference. Uses ImageStream when enabled.
*/}}
{{- define "rootcoz.image" -}}
{{- if .Values.imageStream.enabled -}}
{{- $ns := default .Release.Namespace .Values.imageStream.namespace -}}
{{- printf "image-registry.openshift-image-registry.svc:5000/%s/%s:%s" $ns .Values.imageStream.name (include "rootcoz.imageTag" .) -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (include "rootcoz.imageTag" .) -}}
{{- end -}}
{{- end }}

{{/*
43-char token compatible with secrets.token_urlsafe(32) length.
*/}}
{{- define "rootcoz.generatedEncryptionKey" -}}
{{- randAlphaNum 43 -}}
{{- end }}

{{/*
Resolve ADMIN_KEY: existing secret, values override on install, or generate.
*/}}
{{- define "rootcoz.adminKey" -}}
{{- $secretName := include "rootcoz.credentialsSecretName" . -}}
{{- $secret := lookup "v1" "Secret" .Release.Namespace $secretName -}}
{{- if and $secret (index $secret.data "ADMIN_KEY") -}}
{{- index $secret.data "ADMIN_KEY" | b64dec -}}
{{- else if .Values.admin.key -}}
{{- .Values.admin.key -}}
{{- else if .Release.IsInstall -}}
{{- randAlphaNum 32 -}}
{{- else if not .Release.IsInstall -}}
{{- fail "ADMIN_KEY not found in existing secret and admin.key not set in values. The credentials secret may have been corrupted. Set admin.key in values to restore." -}}
{{- end -}}
{{- end }}

{{/*
Resolve ROOTCOZ_ENCRYPTION_KEY from separate secret or generate on install.
*/}}
{{- define "rootcoz.encryptionKey" -}}
{{- $secretName := include "rootcoz.encryptionSecretName" . -}}
{{- $secret := lookup "v1" "Secret" .Release.Namespace $secretName -}}
{{- if and $secret (index $secret.data "ROOTCOZ_ENCRYPTION_KEY") -}}
{{- index $secret.data "ROOTCOZ_ENCRYPTION_KEY" | b64dec -}}
{{- else if .Values.encryptionKey -}}
{{- .Values.encryptionKey -}}
{{- else if .Release.IsInstall -}}
{{- include "rootcoz.generatedEncryptionKey" . -}}
{{- else if not .Release.IsInstall -}}
{{- fail "ROOTCOZ_ENCRYPTION_KEY not found in existing secret and encryptionKey not set in values. The encryption key secret may have been corrupted. Set encryptionKey in values to restore." -}}
{{- end -}}
{{- end }}

{{/*
Merge credential secret data: AI keys from values (upgrade-safe), ADMIN_KEY via lookup.
*/}}
{{- define "rootcoz.credentialsSecretData" -}}
{{- $secretName := include "rootcoz.credentialsSecretName" . -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace $secretName -}}
{{- $adminKey := include "rootcoz.adminKey" . -}}
{{- if $adminKey }}
ADMIN_KEY: {{ $adminKey | b64enc | quote }}
{{- end -}}
{{- $gemini := .Values.ai.geminiApiKey -}}
{{- if and (not $gemini) $existing (index $existing.data "GEMINI_API_KEY") -}}
{{- $gemini = (index $existing.data "GEMINI_API_KEY" | b64dec) -}}
{{- end -}}
{{- if $gemini }}
GEMINI_API_KEY: {{ $gemini | b64enc | quote }}
{{- end -}}
{{- $anthropic := .Values.ai.anthropicApiKey -}}
{{- if and (not $anthropic) $existing (index $existing.data "ANTHROPIC_API_KEY") -}}
{{- $anthropic = (index $existing.data "ANTHROPIC_API_KEY" | b64dec) -}}
{{- end -}}
{{- if $anthropic }}
ANTHROPIC_API_KEY: {{ $anthropic | b64enc | quote }}
{{- end -}}
{{- $cursor := .Values.ai.cursor.apiKey -}}
{{- if and (not $cursor) $existing (index $existing.data "CURSOR_API_KEY") -}}
{{- $cursor = (index $existing.data "CURSOR_API_KEY" | b64dec) -}}
{{- end -}}
{{- if $cursor }}
CURSOR_API_KEY: {{ $cursor | b64enc | quote }}
{{- end -}}
{{- /* Sidecar agent-dir env vars (API keys referenced as "$VAR" in models.json).
       Values override existing Secret keys; keys present in the existing Secret
       but absent from current values are preserved on upgrade (dropping the map
       must not orphan a models.json that still references one of its keys).
       A key set to an empty string in values is a tombstone: the key is removed
       from the Secret instead of being restored from the existing one.
       Chart-managed keys are excluded; collisions with them fail early in
       rootcoz.validate. */ -}}
{{- $reserved := splitList "," (include "rootcoz.reservedEnvKeys" .) -}}
{{- $envMap := dict -}}
{{- range $name, $value := .Values.sidecar.agentDir.env -}}
{{- $_ := set $envMap $name $value -}}
{{- end -}}
{{- if $existing -}}
{{- range $name, $existingValue := $existing.data -}}
{{- if and (not (has $name $reserved)) (not (hasKey $envMap $name)) -}}
{{- $_ := set $envMap $name ($existingValue | b64dec) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $name, $value := $envMap }}
{{- if $value }}
{{ $name }}: {{ $value | b64enc | quote }}
{{- end }}
{{- end -}}
{{- end }}

{{/*
Generic secret-payload resolver: values override (string or map) → existing secret fallback.
Call via: include "rootcoz.resolveSecretPayload" (dict "value" <val> "secretName" <name> "secretKey" <key> "Release" .Release)
*/}}
{{- define "rootcoz.resolveSecretPayload" -}}
{{- $resolved := "" -}}
{{- if kindIs "string" .value -}}
{{- $resolved = .value -}}
{{- else if kindIs "map" .value -}}
{{- if .value -}}
{{- $resolved = .value | toJson -}}
{{- end -}}
{{- end -}}
{{- if $resolved -}}
{{- $resolved -}}
{{- else -}}
{{- $secret := lookup "v1" "Secret" .Release.Namespace .secretName -}}
{{- if and $secret (index $secret.data .secretKey) -}}
{{- index $secret.data .secretKey | b64dec -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Resolve Vertex SA JSON: values override, else preserve existing secret on upgrade.
Accepts a non-empty string or map in values.
*/}}
{{- define "rootcoz.gcloudServiceAccountKey" -}}
{{- include "rootcoz.resolveSecretPayload" (dict "value" .Values.ai.vertex.serviceAccountKey "secretName" (include "rootcoz.gcloudSecretName" .) "secretKey" "application_default_credentials.json" "Release" .Release) -}}
{{- end }}

{{/*
Resolve Cursor auth.json: values override, else preserve existing secret on upgrade.
*/}}
{{- define "rootcoz.cursorAuthJson" -}}
{{- include "rootcoz.resolveSecretPayload" (dict "value" .Values.ai.cursor.authJson "secretName" (include "rootcoz.cursorAuthSecretName" .) "secretKey" "auth.json" "Release" .Release) -}}
{{- end }}

{{/*
Sidecar agent dir secret data: models.json / auth.json / settings.json payloads
(values override, string or YAML object, → existing secret fallback) as `data:`
lines. Empty when no file is configured — the Deployment gates
PI_SIDECAR_AGENT_DIR and the volume mount on this being non-empty.
*/}}
{{- define "rootcoz.sidecarAgentSecretData" -}}
{{- $secretName := include "rootcoz.sidecarAgentSecretName" . -}}
{{- $files := list "models.json" "auth.json" "settings.json" -}}
{{- $sources := dict "models.json" .Values.sidecar.agentDir.modelsJson "auth.json" .Values.sidecar.agentDir.authJson "settings.json" .Values.sidecar.agentDir.settingsJson -}}
{{- range $file := $files -}}
{{- $payload := include "rootcoz.resolveSecretPayload" (dict "value" (index $sources $file) "secretName" $secretName "secretKey" $file "Release" $.Release) -}}
{{- if $payload }}
{{ $file }}: {{ $payload | b64enc | quote }}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Non-sensitive ConfigMap entries.
*/}}
{{- define "rootcoz.configMapData" -}}
{{- $existing := lookup "v1" "ConfigMap" .Release.Namespace (include "rootcoz.configMapName" .) -}}
{{- $aiProvider := .Values.ai.provider -}}
{{- if and (not $aiProvider) $existing (get $existing.data "AI_PROVIDER") -}}
{{- $aiProvider = get $existing.data "AI_PROVIDER" -}}
{{- end -}}
{{- $aiModel := .Values.ai.model -}}
{{- if and (not $aiModel) $existing (get $existing.data "AI_MODEL") -}}
{{- $aiModel = get $existing.data "AI_MODEL" -}}
{{- end -}}
{{- if $aiProvider }}
AI_PROVIDER: {{ $aiProvider | quote }}
{{- end }}
{{- if $aiModel }}
AI_MODEL: {{ $aiModel | quote }}
{{- end }}
{{- $publicBaseUrl := include "rootcoz.publicBaseUrl" . }}
{{- if $publicBaseUrl }}
PUBLIC_BASE_URL: {{ $publicBaseUrl | quote }}
{{- end }}
SECURE_COOKIES: {{ include "rootcoz.secureCookies" . | quote }}
{{- if .Values.tuning.logLevel }}
LOG_LEVEL: {{ .Values.tuning.logLevel | quote }}
{{- end }}
{{- if .Values.ai.vertex.enabled }}
CLAUDE_CODE_USE_VERTEX: "1"
{{- if .Values.ai.vertex.region }}
CLOUD_ML_REGION: {{ .Values.ai.vertex.region | quote }}
{{- end }}
{{- if .Values.ai.vertex.projectId }}
ANTHROPIC_VERTEX_PROJECT_ID: {{ .Values.ai.vertex.projectId | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Pod-level security context for restricted PodSecurity admission.
*/}}
{{- define "rootcoz.podSecurityContext" -}}
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/*
Container-level security context for restricted PodSecurity admission.
*/}}
{{- define "rootcoz.containerSecurityContext" -}}
allowPrivilegeEscalation: false
runAsNonRoot: true
capabilities:
  drop:
    - ALL
{{- end }}
