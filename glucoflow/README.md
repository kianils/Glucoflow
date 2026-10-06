# GlucoFlow

A data engineering portfolio project that ingests, validates, and analyzes Continuous Glucose Monitor (CGM) data from multiple heterogeneous sources using the AWS data lake medallion architecture (Bronze → Silver → Gold) with an AI query layer powered by Amazon Bedrock.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Data Sources                                                │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Shanghai   │  │ simglucose  │  │  Dexcom-style CSV   │  │
│  │  .xlsx      │  │  .csv       │  │  (messy, generated) │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         └────────────────┴──────────────────────┘            │
│                          │                                   │
│               Bronze S3 (raw, as-is)                        │
│                          │                                   │
│               AWS Glue → Silver S3 (validated, canonical)   │
│                          │                                   │
│               AWS Glue → Gold S3 (aggregated, Athena-ready) │
│                          │                                   │
│               Athena → Query Layer                          │
│                          │                                   │
│               Bedrock (Claude) → Natural Language Interface  │
└──────────────────────────────────────────────────────────────┘
```

---

## Data Sources

| Source | Format | Patients | Purpose |
|--------|--------|----------|---------|
| Shanghai T1DM/T2DM | `.xlsx`, multi-sheet | 12 T1DM, 17+ T2DM | Real clinical CGM data; baseline for all analysis |
| simglucose | `.csv` | Synthetic | Clean, reproducible simulation for gap-filling |
| Dexcom-style messy | `.csv` | Generated | Robustness testing; real consumer device format |

---

## Getting Started

```bash
# 1. Clone and set up virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment variables
cp .env.example .env
# Edit .env with your AWS credentials and bucket names

# 3. Confirm AWS access
aws sts get-caller-identity

# 4. Provision S3 buckets
python infra/provision_buckets.py

# 5. Run tests
pytest tests/
```

---

## Project Structure

```
glucoflow/
├── src/
│   ├── ingestion/          # Parsers for each data source
│   │   ├── shanghai.py     # Reads .xlsx patient files
│   │   ├── simglucose.py   # Reads synthetic CSV output
│   │   └── dexcom.py       # Reads messy Dexcom-style CSV
│   ├── generators/
│   │   ├── run_simulation.py   # Runs simglucose simulator
│   │   └── make_dexcom_csv.py  # Converts sim output → messy Dexcom CSV
│   └── common/
│       ├── schema.py       # Pydantic canonical row model
│       ├── s3.py           # S3 upload helpers
│       └── config.py       # Reads .env into typed config
├── data/
│   ├── raw/shanghai/       # Copy of Shanghai .xlsx files (gitignored)
│   └── samples/            # Small generated samples for testing
│       ├── synthetic/      # simglucose output
│       └── messy/          # Dexcom-style CSVs
├── infra/
│   └── provision_buckets.py
├── tests/
│   ├── test_schema.py
│   └── test_parsers.py
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Tech Stack

| Layer | Tool | Why |
|-------|------|-----|
| Object storage | Amazon S3 | Cheap, durable, Athena-native |
| ETL | AWS Glue (Spark) | Serverless, handles all three source formats |
| Query | Amazon Athena | SQL-on-S3, pay-per-query, no infra |
| AI | Amazon Bedrock (Claude 3) | AWS-native, no cross-cloud auth friction |
| Local validation | Pydantic v2 | Catches schema errors before S3 upload |
| Simulation | simglucose | FDA-accepted T1DM physiological model |
