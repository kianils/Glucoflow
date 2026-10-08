"""
glucoflow_cli.py — GlucoFlow CLI Entry Point

Usage:
  python glucoflow_cli.py           # demo mode (synthetic cohort, no AWS)
  python glucoflow_cli.py --live    # live mode (loads from Gold S3 bucket)
  python glucoflow_cli.py --help

Demo mode is the default and requires no AWS credentials.
Live mode requires a valid .env with AWS credentials and populated S3 buckets.
"""

import argparse
import logging
import os
import sys
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.WARNING,  # Suppress INFO logs during interactive session
    format="%(levelname)s %(name)s — %(message)s",
)

logger = logging.getLogger(__name__)


def load_live_cohort() -> list:
    """
    Load the cohort directly from Gold S3 JSONL files.

    Why S3 directly instead of Athena?
    -----------------------------------
    The REPL only needs a list of Gold metric dicts held in memory for the
    session.  Re-running an Athena query on startup is slow (10-30s), costs
    money, and requires the Glue table to be up to date.  Reading the JSONL
    files directly from S3 is instant, free, and always reflects the latest
    Gold layer state — because gold.py writes the JSONL files directly.

    Returns an empty list (graceful degradation) if S3 is unreachable or
    the Gold bucket has no data yet.
    """
    try:
        import boto3
        from src.common.config import cfg

        s3 = boto3.client("s3", region_name=cfg.aws_region)
        paginator = s3.get_paginator("list_objects_v2")

        cohort = []
        seen = set()

        # Gold keys look like: source=simglucose/date=2026-10-08/adult_001_daily.jsonl
        for page in paginator.paginate(Bucket=cfg.gold_bucket):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".jsonl"):
                    continue
                try:
                    body = s3.get_object(Bucket=cfg.gold_bucket, Key=key)["Body"].read().decode()
                    for line in body.splitlines():
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        pid = row.get("patient_id")
                        if pid and pid not in seen:
                            cohort.append(row)
                            seen.add(pid)
                except Exception as exc:
                    logger.warning(f"Skipping Gold key {key}: {exc}")

        if cohort:
            print(f"  Loaded {len(cohort)} patient(s) from Gold layer.")
        else:
            print("  ⚠  Gold bucket is empty — run the pipeline first.")
            print("     python src/transform/silver.py")
            print("     python src/transform/gold.py")

        return cohort

    except Exception as exc:
        print(f"  ⚠  Could not load live cohort: {exc}")
        print("     Falling back to demo mode.")
        return []


def main():
    parser = argparse.ArgumentParser(
        description="GlucoFlow — Clinical Trial Pre-Screener CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python glucoflow_cli.py               Start in demo mode (no AWS needed)
  python glucoflow_cli.py --live        Start in live mode (reads Gold from S3)
  GLUCOFLOW_DEMO=1 python glucoflow_cli.py    Same as --demo via env var
        """,
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Run in live mode — loads cohort directly from Gold S3 JSONL files",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="Run in demo mode — synthetic cohort, no AWS calls (default)",
    )
    args = parser.parse_args()

    # Env var override
    if os.getenv("GLUCOFLOW_DEMO", "").strip() == "1":
        args.demo = True

    demo_mode = not args.live  # demo is the default unless --live is passed

    from src.repl.repl import GlucoFlowREPL

    if demo_mode:
        repl = GlucoFlowREPL(demo_mode=True)
    else:
        # Load cohort directly from Gold S3 JSONL — no Athena needed
        live_cohort = load_live_cohort()
        if not live_cohort:
            # Graceful degradation: fall back to demo if Gold is empty
            print("  Switching to demo mode.\n")
            repl = GlucoFlowREPL(demo_mode=True)
        else:
            repl = GlucoFlowREPL(demo_mode=False, cohort=live_cohort)

    repl.run()


if __name__ == "__main__":
    main()
