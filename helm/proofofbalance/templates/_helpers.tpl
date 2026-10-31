{{/* Resource names are "<release>-<component>", e.g. pob-service, pob-postgres. */}}
{{- define "pob.name" -}}
{{- printf "%s-%s" .ctx.Release.Name .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "pob.labels" -}}
app.kubernetes.io/name: proofofbalance
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: proofofbalance
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .ctx.Chart.Name .ctx.Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "pob.selector" -}}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "pob.image" -}}
{{- printf "%s:%s" .repository (toString .tag) -}}
{{- end -}}

{{- define "pob.postgresSecret" -}}
{{- if .Values.postgres.existingSecret -}}
{{ .Values.postgres.existingSecret }}
{{- else -}}
{{ include "pob.name" (dict "ctx" . "component" "postgres") }}
{{- end -}}
{{- end -}}

{{/* Env vars shared by every container that talks to the ledger. */}}
{{- define "pob.dbEnv" -}}
- name: DB_USER
  value: {{ .Values.postgres.user | quote }}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "pob.postgresSecret" . }}
      key: POSTGRES_PASSWORD
- name: DATABASE_URL
  value: "postgresql://$(DB_USER):$(DB_PASSWORD)@{{ include "pob.name" (dict "ctx" . "component" "postgres") }}:5432/{{ .Values.postgres.database }}"
{{- end -}}

{{- define "pob.appEnv" -}}
{{ include "pob.dbEnv" . }}
- name: RATES_FILE
  value: /etc/pob/rates.yaml
- name: ROUNDING_MODE
  value: {{ .Values.settlement.roundingMode | quote }}
- name: SETTLEMENT_TZ
  value: {{ .Values.settlement.timezone | quote }}
- name: MAX_USD_AMOUNT
  value: {{ .Values.settlement.maxUsdAmount | quote }}
- name: LOG_LEVEL
  value: {{ .Values.settlement.logLevel | quote }}
{{- end -}}

{{- define "pob.ratesVolume" -}}
- name: rates
  configMap:
    name: {{ include "pob.name" (dict "ctx" . "component" "rates") }}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "pob.ratesMount" -}}
- name: rates
  mountPath: /etc/pob
  readOnly: true
- name: tmp
  mountPath: /tmp
{{- end -}}

{{- define "pob.podSpecCommon" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- end -}}

{{/* Pod template of the payout worker, shared by the Deployment and the Rollout. */}}
{{- define "pob.workerPodTemplate" -}}
metadata:
  labels:
    {{- include "pob.labels" (dict "ctx" . "component" "worker") | nindent 4 }}
  annotations:
    checksum/rates: {{ include (print $.Template.BasePath "/configmap-rates.yaml") . | sha256sum }}
spec:
  {{- include "pob.podSpecCommon" . | nindent 2 }}
  terminationGracePeriodSeconds: {{ add (div .Values.worker.railTimeoutMs 1000) 20 }}
  containers:
    - name: worker
      image: {{ include "pob.image" .Values.images.service }}
      imagePullPolicy: {{ .Values.images.service.pullPolicy }}
      command: ["python", "-m", "app.worker"]
      env:
        {{- include "pob.appEnv" . | nindent 8 }}
        - name: RAIL_URL
          value: "http://{{ include "pob.name" (dict "ctx" . "component" "rail") }}:8081"
        - name: RAIL_TIMEOUT_MS
          value: {{ .Values.worker.railTimeoutMs | quote }}
        - name: WORKER_THREADS
          value: {{ .Values.worker.threads | quote }}
        - name: MAX_ATTEMPTS
          value: {{ .Values.worker.maxAttempts | quote }}
        - name: RETRY_BASE_MS
          value: {{ .Values.worker.retryBaseMs | quote }}
        - name: METRICS_PORT
          value: "9100"
      ports:
        - { name: metrics, containerPort: 9100 }
      readinessProbe:
        tcpSocket: { port: metrics }
        periodSeconds: 5
      livenessProbe:
        httpGet: { path: /metrics, port: metrics }
        initialDelaySeconds: 10
        periodSeconds: 20
      resources:
        {{- toYaml .Values.worker.resources | nindent 8 }}
      securityContext:
        {{- toYaml .Values.containerSecurityContext | nindent 8 }}
      volumeMounts:
        {{- include "pob.ratesMount" . | nindent 8 }}
  volumes:
    {{- include "pob.ratesVolume" . | nindent 4 }}
{{- end -}}
