#!/usr/bin/env bash
# =============================================================================
# teardown_aws.sh — GlucoFlow AWS infrastructure teardown
#
# ⚠️  DESTRUCTIVE — deletes all objects, versions, and the buckets themselves.
# Intended for demo/dev cleanup only. Requires confirmation prompt.
#
# Usage:
#   bash infra/teardown_aws.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env not found at $ENV_FILE"
  exit 1
fi

set -a
# shellcheck disable=SC1090
source <(grep -E '^[A-Z_]+=.+' "$ENV_FILE")
set +a

REQUIRED_VARS=(AWS_REGION BRONZE_BUCKET SILVER_BUCKET GOLD_BUCKET QUARANTINE_BUCKET)
for var in "${REQUIRED_VARS[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var is not set in .env"
    exit 1
  fi
done

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ⚠️   GlucoFlow — AWS Teardown (DESTRUCTIVE)                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  This will PERMANENTLY DELETE:"
echo "    - All objects and all versions in:"
echo "      • $BRONZE_BUCKET"
echo "      • $SILVER_BUCKET"
echo "      • $GOLD_BUCKET"
echo "      • $QUARANTINE_BUCKET"
echo "    - The buckets themselves."
echo ""
echo -n "  Type YES (all caps) to confirm: "
read -r CONFIRM

if [[ "$CONFIRM" != "YES" ]]; then
  echo "  Aborted. Nothing was deleted."
  exit 0
fi

echo ""

# ── Helper: empty bucket (including all versions) and delete ──────────────────
nuke_bucket() {
  local bucket="$1"

  echo -n "  Checking $bucket ... "
  if ! aws s3api head-bucket --bucket "$bucket" --region "$AWS_REGION" 2>/dev/null; then
    echo "does not exist, skipping."
    return 0
  fi
  echo ""

  # Delete all object versions (needed because versioning was enabled)
  echo -n "    Removing all versions ... "
  aws s3api list-object-versions \
    --bucket "$bucket" \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}, Quiet: `true`}' \
    --output json 2>/dev/null | \
  python3 -c "
import sys, json, subprocess
data = json.load(sys.stdin)
objects = data.get('Objects')
if not objects:
    print('none.')
    sys.exit(0)
batch = {'Objects': objects, 'Quiet': True}
out = subprocess.run(
    ['aws', 's3api', 'delete-objects', '--bucket', '$bucket', '--delete', json.dumps(batch)],
    capture_output=True, text=True
)
print(f'deleted {len(objects)} versions.')
"

  # Delete all delete markers
  echo -n "    Removing delete markers ... "
  aws s3api list-object-versions \
    --bucket "$bucket" \
    --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}, Quiet: `true`}' \
    --output json 2>/dev/null | \
  python3 -c "
import sys, json, subprocess
data = json.load(sys.stdin)
objects = data.get('Objects')
if not objects:
    print('none.')
    sys.exit(0)
batch = {'Objects': objects, 'Quiet': True}
out = subprocess.run(
    ['aws', 's3api', 'delete-objects', '--bucket', '$bucket', '--delete', json.dumps(batch)],
    capture_output=True, text=True
)
print(f'removed {len(objects)} markers.')
"

  # Delete the now-empty bucket
  echo -n "    Deleting bucket ... "
  aws s3api delete-bucket \
    --bucket "$bucket" \
    --region "$AWS_REGION" \
    --output text > /dev/null
  echo "deleted."
}

for bucket in "$BRONZE_BUCKET" "$SILVER_BUCKET" "$GOLD_BUCKET" "$QUARANTINE_BUCKET"; do
  nuke_bucket "$bucket"
  echo ""
done

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅  Teardown complete. All GlucoFlow buckets deleted.        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
