"""
config.py
Loads environment variables from .env and exposes a single typed Config object.
All other modules import `cfg` from here — never read os.environ directly.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

#define the config class 
@dataclass
class Config:
    aws_region: str
    bronze_bucket: str
    silver_bucket: str
    gold_bucket: str
    quarantine_bucket: str
    athena_results_bucket: str  # bucket where Athena writes temp query results

#initialization method
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
    # ATHENA_RESULTS_BUCKET is optional — falls back to bronze bucket.
    # Athena must write its temp CSV results somewhere; bronze is always
    # guaranteed to exist so this avoids needing a 5th bucket.
    athena_results = os.getenv("ATHENA_RESULTS_BUCKET") or os.environ["BRONZE_BUCKET"]
    return Config(
        aws_region=os.environ["AWS_REGION"],
        bronze_bucket=os.environ["BRONZE_BUCKET"],
        silver_bucket=os.environ["SILVER_BUCKET"],
        gold_bucket=os.environ["GOLD_BUCKET"],
        quarantine_bucket=os.environ["QUARANTINE_BUCKET"],
        athena_results_bucket=athena_results,
    )


# Module-level singleton — import this everywhere
cfg = load_config()
