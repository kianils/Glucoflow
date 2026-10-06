"""
provision_buckets.py
Python-native equivalent of setup_aws.sh — uses boto3 instead of the AWS CLI.

Why both exist:
  - setup_aws.sh is faster for one-shot manual runs (no Python env needed)
  - provision_buckets.py is useful in CI pipelines or when you want to
    verify infra state programmatically and return a structured result

Usage:
  python infra/provision_buckets.py
  # or via Makefile:
  make provision
"""

import sys
import json
import logging
from pathlib import Path

# Allow running from any working directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from src.common.config import cfg

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

BUCKETS = {
    "bronze":      cfg.bronze_bucket,
    "silver":      cfg.silver_bucket,
    "gold":        cfg.gold_bucket,
    "quarantine":  cfg.quarantine_bucket,
}


def _bucket_exists(s3, name: str) -> bool:
    try:
        s3.head_bucket(Bucket=name)
        return True
    except ClientError:
        return False


def create_bucket(s3, name: str, region: str):
    if _bucket_exists(s3, name):
        log.info(f"[skip]    {name} already exists")
        return

    kwargs = {"Bucket": name}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}

    s3.create_bucket(**kwargs)
    log.info(f"[created] {name}")


def configure_bucket(s3, name: str, add_lifecycle: bool = False):
    # Block all public access
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    log.info(f"[secured] {name} — public access blocked")

    # Enable versioning
    s3.put_bucket_versioning(
        Bucket=name,
        VersioningConfiguration={"Status": "Enabled"},
    )
    log.info(f"[version] {name} — versioning enabled")

    # Bronze-only: 30-day lifecycle expiry (demo cost control)
    if add_lifecycle:
        s3.put_bucket_lifecycle_configuration(
            Bucket=name,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "glucoflow-bronze-30day-expiry",
                        "Filter": {"Prefix": ""},
                        "Status": "Enabled",
                        "Expiration": {"Days": 30},
                        "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
                    }
                ]
            },
        )
        log.info(f"[lifecyc] {name} — 30-day expiry rule set")


def main():
    s3 = boto3.client("s3", region_name=cfg.aws_region)
    region = cfg.aws_region

    log.info("── Creating buckets ────────────────────────────────────────")
    for label, name in BUCKETS.items():
        create_bucket(s3, name, region)

    log.info("")
    log.info("── Configuring buckets ────────────────────────────────────")
    for label, name in BUCKETS.items():
        configure_bucket(s3, name, add_lifecycle=(label == "bronze"))

    log.info("")
    log.info("── Rendering IAM policy ───────────────────────────────────")
    policy_template = Path(__file__).parent / "glucoflow-policy.json"
    policy_rendered = Path(__file__).parent / "glucoflow-policy-rendered.json"

    if policy_template.exists():
        text = policy_template.read_text()
        for placeholder, value in [
            ("BRONZE_BUCKET_PLACEHOLDER",     cfg.bronze_bucket),
            ("SILVER_BUCKET_PLACEHOLDER",     cfg.silver_bucket),
            ("GOLD_BUCKET_PLACEHOLDER",       cfg.gold_bucket),
            ("QUARANTINE_BUCKET_PLACEHOLDER", cfg.quarantine_bucket),
        ]:
            text = text.replace(placeholder, value)
        policy_rendered.write_text(text)
        log.info(f"[policy]  Written to {policy_rendered}")
        log.info("          Attach with: aws iam put-user-policy ...")
    else:
        log.warning("glucoflow-policy.json not found — skipping.")

    log.info("")
    log.info("✅  All done. Verify with: aws s3 ls | grep glucoflow")


if __name__ == "__main__":
    main()
