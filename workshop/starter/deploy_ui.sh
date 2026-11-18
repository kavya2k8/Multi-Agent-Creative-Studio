#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy the Streamlit UI from workshop/starter/ui to Cloud Run.
# Expected files in ui/: app.py, Dockerfile, requirements.txt
#
# Required Secret Manager secrets:
#   creative-studio-username
#   creative-studio-password

SERVICE_NAME="creative-studio-ui"
REGION="us-central1"
PROJECT_ID="ai-creative-studio-capstone"

USERNAME_SECRET="creative-studio-username"
PASSWORD_SECRET="creative-studio-password"
RUNTIME_SA_NAME="creative-studio-ui"

# Resolve project and UI directories.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_DIR="${ROOT_DIR}/ui"
cd "$ROOT_DIR"

echo "Checking UI application files..."
for file in app.py Dockerfile requirements.txt; do
    if [[ ! -f "${UI_DIR}/${file}" ]]; then
        echo "ERROR: ${UI_DIR}/${file} was not found."
        exit 1
    fi
done

# Read deployment configuration from starter/.env.
# Do not print this file; it may contain sensitive values.
if [[ ! -f "${ROOT_DIR}/.env" ]]; then
    echo "ERROR: ${ROOT_DIR}/.env was not found."
    exit 1
fi

set -a
# shellcheck disable=SC1091
source "${ROOT_DIR}/.env"
set +a

: "${AGENT_ENGINE_RESOURCE_NAME:?Missing AGENT_ENGINE_RESOURCE_NAME in starter/.env}"
: "${AGENT_ENGINE_LOCATION:=us-central1}"
: "${ADK_USER_ID:=creative-studio-user}"

if [[ "${GOOGLE_CLOUD_PROJECT:-$PROJECT_ID}" != "$PROJECT_ID" ]]; then
    echo "ERROR: GOOGLE_CLOUD_PROJECT in .env does not match ${PROJECT_ID}."
    exit 1
fi

echo "Checking gcloud authentication and project..."
gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q . || {
    echo "ERROR: Run 'gcloud auth login' first."
    exit 1
}
gcloud config set project "$PROJECT_ID" >/dev/null

echo "Checking required Secret Manager secrets..."
for secret in "$USERNAME_SECRET" "$PASSWORD_SECRET"; do
    if ! gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1; then
        echo "ERROR: Secret '$secret' does not exist."
        echo "Create the secret and add a version before deploying."
        exit 1
    fi
done

echo "Creating/checking Cloud Run service account..."
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe "$RUNTIME_SA" \
    --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
        --display-name="Creative Studio UI Cloud Run runtime" \
        --project "$PROJECT_ID"
fi

echo "Granting Secret Manager access..."
for secret in "$USERNAME_SECRET" "$PASSWORD_SECRET"; do
    gcloud secrets add-iam-policy-binding "$secret" \
        --member="serviceAccount:${RUNTIME_SA}" \
        --role="roles/secretmanager.secretAccessor" \
        --project="$PROJECT_ID" \
        --quiet >/dev/null
done

echo "Granting Vertex AI access..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/aiplatform.user" \
    --quiet >/dev/null

echo "Deploying ${SERVICE_NAME} from ${UI_DIR} using its Dockerfile..."
gcloud run deploy "$SERVICE_NAME" \
    --source "$UI_DIR" \
    --clear-base-image \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --service-account "$RUNTIME_SA" \
    --allow-unauthenticated \
    --port 8080 \
    --timeout 1000s \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},AGENT_ENGINE_LOCATION=${AGENT_ENGINE_LOCATION},AGENT_ENGINE_RESOURCE_NAME=${AGENT_ENGINE_RESOURCE_NAME},ADK_USER_ID=${ADK_USER_ID}" \
    --set-secrets "APP_USERNAME=${USERNAME_SECRET}:latest,APP_PASSWORD=${PASSWORD_SECRET}:latest"

echo
echo "Deployment command completed. Fetching service URL..."
SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --format="value(status.url)")"

echo
echo "========================================"
echo "Creative Studio UI deployment complete"
echo "========================================"
echo "Service URL: ${SERVICE_URL}"
