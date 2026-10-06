#!/usr/bin/env bash
# =============================================================================
# setup_aws.sh — GlucoFlow Day 1 AWS infrastructure provisioning
#
# Creates 4 S3 buckets (bronze, silver, gold, quarantine), enables versioning,
# blocks all public access, and adds a 30-day lifecycle expiry on bronze.
# Idempotent: safe to re-run if a resource already exists.
#
# Usage:
#   1. Copy .env.example → .env and fill in your unique bucket names + region.
#   2. Run: bash infra/setup_aws.sh
#
# Pre-requisites:
#   - AWS CLI installed and `aws configure` completed (or AWS_PROFILE set)
#   - Bucket names in .env must be globally unique on S3
# =============================================================================

set -euo pipefail

# ── Load .env ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env not found at $ENV_FILE"
  echo "       Copy .env.example to .env and fill in your values."
  exit 1
fi

# Source only the lines that look like VAR=value (skip comments, blank lines)
set -a
# shellcheck disable=SC1090
source <(grep -E '^[A-Z_]+=.+' "$ENV_FILE")
set +a

# ── Validate required variables ───────────────────────────────────────────────
REQUIRED_VARS=(AWS_REGION BRONZE_BUCKET SILVER_BUCKET GOLD_BUCKET QUARANTINE_BUCKET)
for var in "${REQUIRED_VARS[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: Required variable $var is not set in .env"
    exit 1
  fi
done

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║        GlucoFlow — AWS Infrastructure Setup                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Region  : $AWS_REGION"
echo "  Bronze  : $BRONZE_BUCKET"
echo "  Silver  : $SILVER_BUCKET"
echo "  Gold    : $GOLD_BUCKET"
echo "  Quarant : $QUARANTINE_BUCKET"
echo ""

# ── Helper: create bucket if it doesn't exist ─────────────────────────────────
create_bucket() {
  local bucket="$1"
  local region="$2"

  echo -n "  [bucket] $bucket ... "

  if aws s3api head-bucket --bucket "$bucket" --region "$region" 2>/dev/null; then
    echo "already exists, skipping."
    return 0
  fi

  if [[ "$region" == "us-east-1" ]]; then
    # us-east-1 does NOT accept a LocationConstraint — omit it
    aws s3api create-bucket \
      --bucket "$bucket" \
      --region "$region" \
      --output text > /dev/null
  else
    aws s3api create-bucket \
      --bucket "$bucket" \
      --region "$region" \
      --create-bucket-configuration LocationConstraint="$region" \
      --output text > /dev/null
  fi
  echo "created."
}

# ── Helper: block all public access ──────────────────────────────────────────
block_public_access() {
  local bucket="$1"
  echo -n "  [public-access] $bucket ... "
  aws s3api put-public-access-block \
    --bucket "$bucket" \
    --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
    --output text > /dev/null
  echo "blocked."
}

# ── Helper: enable versioning ─────────────────────────────────────────────────
enable_versioning() {
  local bucket="$1"
  echo -n "  [versioning] $bucket ... "
  aws s3api put-bucket-versioning \
    --bucket "$bucket" \
    --versioning-configuration Status=Enabled \
    --output text > /dev/null
  echo "enabled."
}

# ── Helper: add lifecycle rule on bronze ─────────────────────────────────────
add_lifecycle_rule() {
  local bucket="$1"
  echo -n "  [lifecycle] $bucket (expire objects after 30 days) ... "
  aws s3api put-bucket-lifecycle-configuration \
    --bucket "$bucket" \
    --lifecycle-configuration '{
      "Rules": [
        {
          "ID": "glucoflow-bronze-30day-expiry",
          "Filter": { "Prefix": "" },
          "Status": "Enabled",
          "Expiration": { "Days": 30 },
          "NoncurrentVersionExpiration": { "NoncurrentDays": 7 }
        }
      ]
    }' \
    --output text > /dev/null
  echo "set."
}

# ── Create all buckets ────────────────────────────────────────────────────────
echo "── Step 1: Creating buckets ─────────────────────────────────────"
for bucket in "$BRONZE_BUCKET" "$SILVER_BUCKET" "$GOLD_BUCKET" "$QUARANTINE_BUCKET"; do
  create_bucket "$bucket" "$AWS_REGION"
done
echo ""

# ── Block public access ───────────────────────────────────────────────────────
echo "── Step 2: Blocking public access ───────────────────────────────"
for bucket in "$BRONZE_BUCKET" "$SILVER_BUCKET" "$GOLD_BUCKET" "$QUARANTINE_BUCKET"; do
  block_public_access "$bucket"
done
echo ""

# ── Enable versioning ─────────────────────────────────────────────────────────
echo "── Step 3: Enabling versioning ──────────────────────────────────"
for bucket in "$BRONZE_BUCKET" "$SILVER_BUCKET" "$GOLD_BUCKET" "$QUARANTINE_BUCKET"; do
  enable_versioning "$bucket"
done
echo ""

# ── Add lifecycle rule on bronze only ─────────────────────────────────────────
echo "── Step 4: Adding lifecycle rule ────────────────────────────────"
add_lifecycle_rule "$BRONZE_BUCKET"
echo ""

# ── Render IAM policy with real bucket names ──────────────────────────────────
echo "── Step 5: Rendering IAM policy ─────────────────────────────────"
POLICY_TEMPLATE="$SCRIPT_DIR/glucoflow-policy.json"
POLICY_RENDERED="$SCRIPT_DIR/glucoflow-policy-rendered.json"

if [[ -f "$POLICY_TEMPLATE" ]]; then
  sed \
    -e "s|BRONZE_BUCKET_PLACEHOLDER|$BRONZE_BUCKET|g" \
    -e "s|SILVER_BUCKET_PLACEHOLDER|$SILVER_BUCKET|g" \
    -e "s|GOLD_BUCKET_PLACEHOLDER|$GOLD_BUCKET|g" \
    -e "s|QUARANTINE_BUCKET_PLACEHOLDER|$QUARANTINE_BUCKET|g" \
    "$POLICY_TEMPLATE" > "$POLICY_RENDERED"
  echo "  Rendered policy written to: infra/glucoflow-policy-rendered.json"
  echo "  Attach this to your IAM user/role with:"
  echo "    aws iam put-user-policy --user-name <YOUR_USER> \\"
  echo "      --policy-name GlucoFlowS3Policy \\"
  echo "      --policy-document file://infra/glucoflow-policy-rendered.json"
else
  echo "  WARNING: glucoflow-policy.json not found — skipping policy render."
fi
echo ""

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅  Setup complete. Verify with:                            ║"
echo "║                                                              ║"
echo "║    aws s3 ls | grep glucoflow                                ║"
echo "║    aws s3api get-bucket-versioning --bucket \$BRONZE_BUCKET   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
