#!/usr/bin/env bash
# setup_gcp_hub.sh — provision the Conductor central hub on GCP.
#
# Idempotent: safe to re-run. Each step checks for existing state first.
# Read-only by default (`--check`); pass `--apply` to actually create resources.
#
# Manual steps that cannot be automated are flagged with "MANUAL:" and the
# script will pause + print instructions when it hits one.
#
# Prerequisites on the running machine:
#   - gcloud CLI installed and authenticated as O (ouday@tryloop.ai)
#   - O has the Project Creator role on Loop's GCP org (or is org admin)
#   - Loop's GCP org ID known (set ORG_ID below or pass via env)
#
# Usage:
#   bash scripts/setup_gcp_hub.sh --check       # dry-run, prints what would happen
#   bash scripts/setup_gcp_hub.sh --apply       # actually creates resources
#
# Stage of the plan this satisfies: prerequisite #4 (GCP project provisioned).

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================
PROJECT_ID="${PROJECT_ID:-loop-revops-conductor-hub}"
PROJECT_NAME="Loop RevOps Conductor Hub"
ORG_ID="${ORG_ID:-}"                # set this in env or paste here once known
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"  # `gcloud billing accounts list` to find
REGION="${REGION:-us-central1}"
SQL_INSTANCE="${SQL_INSTANCE:-conductor-hub-db}"
SQL_TIER="${SQL_TIER:-db-f1-micro}"     # ~$10/mo, fine for V1 hub volume
SQL_DB_NAME="${SQL_DB_NAME:-conductor_hub}"
SECRET_PREFIX="${SECRET_PREFIX:-conductor-hub}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-conductor-hub-runner}"

MODE="check"
case "${1:-}" in
  --apply) MODE="apply" ;;
  --check|"") MODE="check" ;;
  *) echo "Unknown arg: $1. Use --check or --apply."; exit 2 ;;
esac

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
do_or_show() {
  if [[ "$MODE" == "apply" ]]; then
    log "RUN: $*"
    eval "$@"
  else
    log "WOULD RUN: $*"
  fi
}
manual() {
  log "MANUAL STEP REQUIRED — $*"
  if [[ "$MODE" == "apply" ]]; then
    read -rp "  Press Enter once done (or Ctrl-C to abort): " _
  fi
}

log "Conductor hub GCP setup — mode: $MODE, project: $PROJECT_ID, region: $REGION"

# ============================================================================
# 0. Sanity: gcloud installed + authed
# ============================================================================
if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud CLI not installed. Install: https://cloud.google.com/sdk/docs/install"
  exit 1
fi

CURRENT_ACCOUNT=$(gcloud config get-value account 2>/dev/null || echo "(none)")
log "Authenticated as: $CURRENT_ACCOUNT"
if [[ "$CURRENT_ACCOUNT" != "ouday@tryloop.ai" ]]; then
  log "WARN: expected ouday@tryloop.ai; got $CURRENT_ACCOUNT. Run 'gcloud auth login' first."
fi

# ============================================================================
# 1. Project creation
# ============================================================================
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  log "Project $PROJECT_ID already exists. Skipping create."
else
  if [[ -z "$ORG_ID" ]]; then
    manual "Set ORG_ID env var to Loop's GCP org ID. Find via: gcloud organizations list"
  fi
  do_or_show "gcloud projects create $PROJECT_ID --name='$PROJECT_NAME' --organization=$ORG_ID"
fi

do_or_show "gcloud config set project $PROJECT_ID"

# ============================================================================
# 2. Billing link (cannot fully automate — billing account ID lookup is manual)
# ============================================================================
BILLING_LINKED=$(gcloud beta billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || echo "false")
if [[ "$BILLING_LINKED" == "True" ]]; then
  log "Billing already linked to $PROJECT_ID."
else
  if [[ -z "$BILLING_ACCOUNT" ]]; then
    manual "Find Loop's billing account ID via: gcloud billing accounts list. Then re-run with BILLING_ACCOUNT=<id>."
  fi
  do_or_show "gcloud beta billing projects link $PROJECT_ID --billing-account=$BILLING_ACCOUNT"
fi

# ============================================================================
# 3. Enable required APIs
# ============================================================================
APIS=(
  run.googleapis.com                # Cloud Run (hub service)
  sqladmin.googleapis.com           # Cloud SQL (hub Postgres)
  secretmanager.googleapis.com      # Secret Manager (HMAC + Fernet keys)
  cloudbuild.googleapis.com         # Cloud Build (container build for deploy)
  artifactregistry.googleapis.com   # Artifact Registry (container images)
  iamcredentials.googleapis.com     # service-account impersonation for deploy
  logging.googleapis.com            # Cloud Logging (default but enable explicit)
  monitoring.googleapis.com         # Cloud Monitoring (alerting on hub)
  sourcerepo.googleapis.com         # Cloud Source Repos (Phase 4 git mirror)
)
for api in "${APIS[@]}"; do
  if gcloud services list --enabled --filter="config.name:$api" --format='value(config.name)' | grep -q "$api"; then
    log "API $api already enabled."
  else
    do_or_show "gcloud services enable $api"
  fi
done

# ============================================================================
# 4. Cloud SQL Postgres instance (small tier — V1 hub workload is light)
# ============================================================================
if gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  log "Cloud SQL instance $SQL_INSTANCE already exists."
else
  do_or_show "gcloud sql instances create $SQL_INSTANCE \
    --database-version=POSTGRES_15 \
    --tier=$SQL_TIER \
    --region=$REGION \
    --storage-size=10GB \
    --storage-auto-increase \
    --backup-start-time=07:00 \
    --maintenance-window-day=SUN \
    --maintenance-window-hour=08"
fi

if gcloud sql databases describe "$SQL_DB_NAME" --instance="$SQL_INSTANCE" >/dev/null 2>&1; then
  log "DB $SQL_DB_NAME already exists on $SQL_INSTANCE."
else
  do_or_show "gcloud sql databases create $SQL_DB_NAME --instance=$SQL_INSTANCE"
fi

# DB user — password generated, stored in Secret Manager (next section)
if gcloud sql users list --instance="$SQL_INSTANCE" --format='value(name)' | grep -q '^conductor_hub_app$'; then
  log "DB user conductor_hub_app already exists on $SQL_INSTANCE."
else
  manual "Generate a 32-char password and store it in Secret Manager as $SECRET_PREFIX-db-password BEFORE running this. Then run: gcloud sql users create conductor_hub_app --instance=$SQL_INSTANCE --password=<paste>"
fi

# ============================================================================
# 5. Secret Manager — secrets the hub needs
# ============================================================================
SECRETS=(
  "$SECRET_PREFIX-db-password"               # Cloud SQL conductor_hub_app password
  "$SECRET_PREFIX-fernet-master-key"         # encrypts oauth_tokens at rest on team VMs (mirrored here for restore)
  "$SECRET_PREFIX-hmac-leadership"           # HMAC key for leadership team VM telemetry signing
  "$SECRET_PREFIX-anthropic-api-key"         # for analyst Claude calls
  "$SECRET_PREFIX-google-oauth-client-id"
  "$SECRET_PREFIX-google-oauth-client-secret"
  "$SECRET_PREFIX-slack-bot-token-leadership"
  "$SECRET_PREFIX-slack-app-token-leadership"
)
for secret in "${SECRETS[@]}"; do
  if gcloud secrets describe "$secret" >/dev/null 2>&1; then
    log "Secret $secret already exists. Versions managed manually."
  else
    do_or_show "gcloud secrets create $secret --replication-policy=automatic"
    log "  -> Add a value via: echo -n '<value>' | gcloud secrets versions add $secret --data-file=-"
  fi
done

manual "Populate secret values now if MODE=apply. The script does NOT write secret values — values must be pasted manually for audit reasons."

# ============================================================================
# 6. Service accounts
# ============================================================================
SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  log "Service account $SA_EMAIL already exists."
else
  do_or_show "gcloud iam service-accounts create $SERVICE_ACCOUNT_NAME --display-name='Conductor Hub Cloud Run Runner'"
fi

ROLES=(
  roles/cloudsql.client
  roles/secretmanager.secretAccessor
  roles/logging.logWriter
  roles/monitoring.metricWriter
)
for role in "${ROLES[@]}"; do
  do_or_show "gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$SA_EMAIL --role=$role --condition=None"
done

# ============================================================================
# 7. Artifact Registry repo for hub images
# ============================================================================
if gcloud artifacts repositories describe conductor-hub --location="$REGION" >/dev/null 2>&1; then
  log "Artifact Registry repo conductor-hub already exists."
else
  do_or_show "gcloud artifacts repositories create conductor-hub --repository-format=docker --location=$REGION --description='Conductor hub container images'"
fi

# ============================================================================
# 8. Done
# ============================================================================
log "GCP hub provisioning complete (mode: $MODE)."

cat <<EOF

Next steps O still owns manually:
  1. Verify billing is linked and budget alerts are set:
     https://console.cloud.google.com/billing/$PROJECT_ID
  2. Populate Secret Manager values listed above (values pasted, not scripted, for audit).
  3. Cloud Run service is NOT deployed by this script — that happens at Stage 5
     of the Conductor plan when 'agents/hub/main.py' exists.
  4. Cloud SQL is reachable via Cloud SQL Auth Proxy from your laptop:
     gcloud sql connect $SQL_INSTANCE --user=conductor_hub_app --database=$SQL_DB_NAME
  5. Costs to watch — set a budget alert at \$100/mo as a tripwire:
       Cloud SQL db-f1-micro:  ~\$10/mo
       Cloud Run (idle):        ~\$0/mo (scales to zero)
       Cloud Run (active V1):   ~\$5–20/mo
       Secret Manager:          ~\$1/mo
       Artifact Registry:       ~\$1/mo

Abort if any of the above feels off.
EOF
