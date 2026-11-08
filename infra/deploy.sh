#!/usr/bin/env bash
# Provisionamento mínimo no GCP (rodar à mão, uma vez, depois da primeira execução local).
# Projeto próprio do recibo, separado de qualquer outro.
set -euo pipefail

PROJECT="${PROJECT:?defina PROJECT=<id do projeto gcp>}"
REGION="${REGION:-southamerica-east1}"      # IP brasileiro para as fontes gov.br
BUCKET="${BUCKET:?defina BUCKET=<nome do bucket raw>}"
BQ_LOCATION="${BQ_LOCATION:-US}"            # mesma location da Base dos Dados (cross-check sem cópia)
JOB="recibo-run"
REPO="recibo"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/recibo:latest"
SA="recibo-job@${PROJECT}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT"

gcloud services enable run.googleapis.com cloudscheduler.googleapis.com artifactregistry.googleapis.com \
  storage.googleapis.com bigquery.googleapis.com cloudbuild.googleapis.com

# Bucket raw: versionamento ligado = objetos nunca sobrescritos de fato.
# Atenção: o bucket precisa estar na mesma location do dataset do BigQuery para load jobs (por isso US).
gcloud storage buckets create "gs://${BUCKET}" --location="$BQ_LOCATION" --uniform-bucket-level-access || true
gcloud storage buckets update "gs://${BUCKET}" --versioning

# Service account do job: só o que precisa.
gcloud iam service-accounts create recibo-job --display-name="recibo job" || true
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="serviceAccount:${SA}" --role=roles/storage.objectAdmin
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${SA}" --role=roles/bigquery.jobUser
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${SA}" --role=roles/bigquery.dataEditor

# Imagem
gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" || true
gcloud builds submit --tag "$IMAGE" --region="$REGION" .

# Job
gcloud run jobs create "$JOB" --image="$IMAGE" --region="$REGION" --service-account="$SA" \
  --set-env-vars="RECIBO_STORE=gcs,RECIBO_GCS_BUCKET=${BUCKET}" \
  --task-timeout=3600 --max-retries=1 --memory=2Gi || \
gcloud run jobs update "$JOB" --image="$IMAGE" --region="$REGION" --service-account="$SA" \
  --set-env-vars="RECIBO_STORE=gcs,RECIBO_GCS_BUCKET=${BUCKET}" \
  --task-timeout=3600 --max-retries=1 --memory=2Gi

# Agenda semanal (segunda, 09:17 em Brasília). Idempotente por hash: sem mudança na fonte, não grava nada.
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${SA}" --role=roles/run.invoker
gcloud scheduler jobs create http recibo-semanal --location="$REGION" \
  --schedule="17 9 * * 1" --time-zone="America/Sao_Paulo" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST --oauth-service-account-email="$SA" || true

echo "ok. execução manual: gcloud run jobs execute ${JOB} --region ${REGION}"
