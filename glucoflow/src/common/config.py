"""
config.py
Loads environment variables from .env and exposes a single typed Config object.
All other modules import `cfg` from here — never read os.environ directly.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    aws_region: str
    bronze_bucket: str
    silver_bucket: str
    gold_bucket: str
    quarantine_bucket: str


def load_config() -> Config:
    missing = []
    keys = ["AWS_REGION", "BRONZE_BUCKET", "SILVER_BUCKET", "GOLD_BUCKET", "QUARANTINE_BUCKET"]
    for key in keys:
        if not os.getenv(key):
            missing.append(key)
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your values."
        )
    return Config(
        aws_region=os.environ["AWS_REGION"],
        bronze_bucket=os.environ["BRONZE_BUCKET"],
        silver_bucket=os.environ["SILVER_BUCKET"],
        gold_bucket=os.environ["GOLD_BUCKET"],
        quarantine_bucket=os.environ["QUARANTINE_BUCKET"],
    )


# Module-level singleton — import this everywhere
cfg = load_config()
