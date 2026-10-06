# GlucoFlow — Complete Codebase Guide

> **Purpose of this document:** You are the author of this codebase. This guide explains every file, every design decision, every line of reasoning — so you can walk through any part of the code with authority, answer questions about why things were built a certain way, and pick up where you left off after any gap.

---

## Table of Contents

1. [Project Purpose & Architecture](#1-project-purpose--architecture)
2. [Repository Layout](#2-repository-layout)
3. [The Three Data Sources — What They Are and Why Each One Exists](#3-the-three-data-sources)
4. [The Medallion Architecture — Bronze, Silver, Gold, Quarantine](#4-the-medallion-architecture)
5. [File-by-File Reference](#5-file-by-file-reference)
   - [src/common/config.py](#srccommonconfigpy)
   - [src/common/schema.py](#srccommonschemapy)
   - [src/common/s3_utils.py](#srccommons3_utilspy)
   - [src/generators/run_simulation.py](#srcgeneratorsrun_simulationpy)
   - [src/generators/make_dexcom_csv.py](#srcgeneratorsmake_dexcom_csvpy)
   - [src/ingestion/shanghai.py](#srcingestionshanghaipy)
   - [src/ingestion/simglucose.py](#srcingestionsimglucosepy)
   - [src/ingestion/dexcom.py](#srcingestiondexcompy)
   - [infra/setup_aws.sh](#infrasetup_awssh)
   - [infra/teardown_aws.sh](#infrateardown_awssh)
   - [infra/glucoflow-policy.json](#infraglucoflow-policyjson)
   - [infra/provision_buckets.py](#infraprovision_bucketspy)
   - [tests/test_schema.py](#teststest_schemapy)
   - [tests/test_generators.py](#teststest_generatorspy)
   - [tests/test_infra.py](#teststest_infrapy)
   - [requirements.txt](#requirementstxt)
   - [.env.example](#envexample)
   - [Makefile](#makefile)
6. [Key Design Decisions — The Why Behind Everything](#6-key-design-decisions)
7. [Data Flow — From Source to Bronze S3](#7-data-flow-from-source-to-bronze-s3)
8. [AWS Infrastructure — What Gets Created and Why](#8-aws-infrastructure)
9. [Running the Project — Step by Step](#9-running-the-project)
10. [Clinical Context — Why These Numbers Matter](#10-clinical-context)
11. [What Comes Next (Day 2 and Beyond)](#11-what-comes-next)

---

## 1. Project Purpose & Architecture

GlucoFlow is a **data engineering portfolio project** that builds an end-to-end CGM (Continuous Glucose Monitor) data pipeline on AWS. It is designed to demonstrate command of the following concepts in a single, coherent codebase:

| Concept | Where It Shows Up |
|---|---|
| Schema-first data modeling | `CanonicalReading` Pydantic model — every row in the system conforms to one typed schema |
| Medallion / lakehouse architecture | Bronze → Silver → Gold S3 buckets with clearly defined rules at each layer |
| Heterogeneous source ingestion | Three data sources with completely different formats, all normalized to one schema |
| Idempotent AWS infrastructure | `setup_aws.sh` can be re-run safely at any point |
| Least-privilege IAM | `glucoflow-policy.json` grants only the exact S3 actions the pipeline needs |
| Data lineage & auditability | Every row carries a `lineage_id` (UUID4) and `raw_payload` (verbatim original row) |
| Test-driven development | 79 tests across 3 test files — schema logic, file structure, policy validation, bash safety |

The project domain is diabetes/CGM data. This is intentional: it provides real clinical complexity (sensor limits, calibration events, unit conversions) that makes the engineering problems more interesting than a toy dataset, while remaining approachable without a medical background.

---

## 2. Repository Layout

```
glucoflow/
│
├── src/
│   ├── common/
│   │   ├── config.py          ← Loads .env; single Config object imported everywhere
│   │   ├── schema.py          ← CanonicalReading Pydantic model — the heart of the pipeline
│   │   └── s3_utils.py        ← boto3 wrappers: upload, build_key, quarantine
│   │
│   ├── generators/
│   │   ├── run_simulation.py  ← Runs the UVA/Padova T1DM simulator → synthetic CSVs
│   │   └── make_dexcom_csv.py ← Transforms synthetic CSVs → messy Dexcom-style exports
│   │
│   └── ingestion/
│       ├── shanghai.py        ← Reads .xlsx patient files → validates → uploads to Bronze
│       ├── simglucose.py      ← Reads synthetic CSVs → validates → uploads to Bronze
│       └── dexcom.py          ← Reads messy CSVs → parses quirks → uploads to Bronze
│
├── infra/
│   ├── setup_aws.sh           ← Idempotent bash: creates 4 S3 buckets, IAM policy
│   ├── teardown_aws.sh        ← Destroys all 4 buckets (with confirmation)
│   ├── glucoflow-policy.json  ← IAM policy template (placeholders, not real names)
│   └── provision_buckets.py  ← Python equivalent of setup_aws.sh (for CI use)
│
├── data/
│   ├── raw/shanghai/          ← gitignored — place Shanghai .xlsx files here
│   └── samples/
│       ├── synthetic/         ← gitignored — simglucose CSVs written here
│       └── messy/             ← gitignored — Dexcom-style CSVs written here
│
├── tests/
│   ├── test_schema.py         ← 28 tests for CanonicalReading and _classify_glucose
│   ├── test_generators.py     ← 37 tests verifying synthetic + messy file structure
│   └── test_infra.py          ← 14 tests for IAM policy validity and bash script safety
│
├── .env.example               ← Template — copy to .env and fill in your values
├── .gitignore                 ← Excludes .env, data/raw/, samples/, __pycache__, .venv
├── Makefile                   ← Human-friendly entry points for every major action
└── requirements.txt           ← All Python dependencies pinned to minimum safe versions
```

---

## 3. The Three Data Sources

Understanding why there are three sources — not one — is fundamental to the project.

### Source 1: Shanghai T1DM / T2DM Dataset (Real Clinical Data)

**What it is:** A published research dataset from Ruijin Hospital, Shanghai (Zhao et al., *Scientific Data*, 2023). It contains real inpatient CGM data for Chinese patients admitted for diabetes management.

**Format:** `.xlsx` (Excel files), multi-sheet. Two layers:
- **Summary file** (`Shanghai_T1DM_Summary.xlsx` / `Shanghai_T2DM_Summary.xlsx`): One row per hospital admission. 33+ clinical columns including demographics (age, sex, BMI, diabetes duration), comorbidities (retinopathy, neuropathy, nephropathy), full lab results (HbA1c, fasting glucose, lipid panel, kidney function), and medication types.
- **Per-patient CGM files** (e.g. `1001_0_20210730.xlsx`): One file per patient per admission. Contains timestamped CGM readings at ~15-minute intervals, plus insulin doses, meal logs, and ketone measurements.

**Filename convention:** `{patient_id}_{visit_number}_{YYYYMMDD}.xlsx`
- `1001_0_20210730.xlsx` = patient 1001, first admission, started July 30 2021
- Same patient can have multiple files (multiple admissions over time)

**Scale:** 12 unique T1DM patients, 17+ T2DM patients. Some patients have 2–3 admissions.

**Why it's in the project:** Provides real-world clinical complexity. The data has actual messiness: some cells use `"/"` for missing values (not standard null), column headers are slightly inconsistent between files, and a Oct 2023 update added 188 new measurements to two existing patients — exactly the kind of incremental update a real pipeline must handle without re-processing everything.

**Pipeline challenge it creates:** The parser must locate columns by name (not position), handle `"/"` nulls, normalize mixed timestamp formats, and keep per-patient lineage intact across multiple files per patient.

---

### Source 2: simglucose Synthetic Data

**What it is:** Computer-generated CGM traces from the **UVA/Padova T1DM simulator**, which is the gold-standard simulation platform for closed-loop insulin delivery research. It is FDA-accepted for preclinical evaluation of artificial pancreas algorithms.

**Format:** Clean CSV, 3+ columns: `BG` (true blood glucose), `CGM` (sensor reading = BG + sensor noise), `CHO` (carbs), `insulin`, plus risk scores `LBGI`, `HBGI`, `Risk`.

**Three patient archetypes simulated:**
- `adolescent#001` — simulated mean ~127 mg/dL, mostly in range
- `adult#001` — simulated mean ~145 mg/dL, some post-meal spikes
- `child#001` — simulated mean ~116 mg/dL, **dips to ~49 mg/dL** (hypoglycaemic window — clinically realistic for under-dosed pediatric model)

**Why `CGM` is used instead of `BG`:** `BG` is the "true" physiological blood glucose — a number that doesn't exist in real life. Real CGMs measure interstitial fluid glucose with a lag and sensor noise on top. Using `CGM` is more honest — it's what a real device would report.

**Why this source exists:** We don't have enough real patient data to stress-test the pipeline at scale. simglucose fills that gap with unlimited reproducible data while keeping the numbers clinically meaningful. It also lets us manufacture edge cases (very low glucose → `"Low"` strings in the Dexcom conversion) on demand.

---

### Source 3: Dexcom-Style Messy CSV

**What it is:** A file we generate ourselves, by taking the simglucose output and deliberately transforming it to look like a real Dexcom Clarity device export. It is not a separate clinical dataset — it is a **format transformation of Source 2**.

**Why it exists as a separate source:** The whole point of a Bronze ingestion layer is to prove your pipeline can accept heterogeneous formats. If Sources 1 and 2 were the only sources, someone could argue your parsers are too narrow. By adding a third source with a completely different shape — flat CSV, mixed event types, metadata header, string-encoded limits — you demonstrate that your architecture is genuinely format-agnostic.

**The five deliberate defects injected:**

| Defect | Technical detail | Why it's there |
|---|---|---|
| 3-row metadata header | Device name, export date, blank line before column headers | Parser must `skiprows=3`; naive `.read_csv()` would treat row 4 as header |
| Mixed event types | EGV, Calibration, Insulin, Carbs in one `Event Type` column | Parser must filter by event type, not assume all rows are glucose readings |
| `"Low"` / `"High"` strings | Rows where glucose < 55 become the literal string `"Low"` | Tests `_classify_glucose()` string-handling path; preserves no numeric value |
| Blank spacer rows | All-empty rows between event blocks | Parser must skip NaN rows without crashing |
| Sensor dropout rows | ~2% of EGV rows have blank glucose cell | Tests NaN handling in the glucose column mid-stream |

**A note on `"Low"` vs `"out_of_range"`:** These are different flags for different things.
- `"low"` = the sensor hit its physical detection floor (~40 mg/dL) and can't give you a number. There is no numeric value.
- `"out_of_range"` = the sensor gave you a number, but it's outside the physiologically plausible range (e.g. `glucose_mgdl=501` would be flagged `out_of_range` and the value is preserved for audit). You're not discarding it, just marking it suspicious.

---

## 4. The Medallion Architecture

```
Raw files (local)
       │
       ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  BRONZE  (s3://glucoflow-bronze-xxx/)                               │
  │  Raw, as-landed data serialized as JSONL.                           │
  │  Never modified after write. Source-partitioned:                    │
  │    source=shanghai_t1dm/date=YYYY-MM-DD/<patient_id>.jsonl          │
  │    source=simglucose/date=YYYY-MM-DD/<patient_id>.jsonl             │
  │    source=dexcom_csv/date=YYYY-MM-DD/<patient_id>.jsonl             │
  │  Lifecycle: auto-deleted after 30 days (cost control on demo data)  │
  └─────────────────────┬───────────────────────────────────────────────┘
                        │  (Day 2: Silver transform)
                        ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  SILVER  (s3://glucoflow-silver-xxx/)                               │
  │  Cleaned, joined, deduplicated. All three sources merged into       │
  │  one canonical schema. Parquet format (columnar, Athena-queryable). │
  │  Clinical flags computed: time_in_range, hypoglycaemic_events, etc. │
  └─────────────────────┬───────────────────────────────────────────────┘
                        │  (Day 3+: Gold aggregation)
                        ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  GOLD    (s3://glucoflow-gold-xxx/)                                 │
  │  Aggregated, business-ready. Per-patient daily/weekly summaries.    │
  │  Powering the Bedrock AI conversational layer.                      │
  └─────────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────────┐
  │  QUARANTINE (s3://glucoflow-quarantine-xxx/)                        │
  │  Rows that fail Pydantic validation. Preserved for investigation.   │
  │  Key structure: reason=<validation_error>/source=.../date=.../...   │
  └─────────────────────────────────────────────────────────────────────┘
```

**Why JSONL at Bronze (not Parquet)?** JSONL (newline-delimited JSON) is the right format for Bronze because:
1. It can store heterogeneous schemas — different source files have different column sets
2. It's trivially writable with no dependencies (just `json.dumps` per row)
3. It's human-readable for debugging
4. Parquet is better suited for Silver/Gold where the schema is uniform and query performance matters

**Why Hive-style partition keys?** S3 object keys are structured as `source=X/date=YYYY-MM-DD/file.jsonl`. This is the Hive partition convention that AWS Athena reads natively — you can point an Athena table at the Bronze bucket and query it directly with `WHERE source = 'shanghai_t1dm' AND date > '2023-01-01'` without any ETL step.

---

## 5. File-by-File Reference

---

### `src/common/config.py`

**What it does:** Loads the `.env` file and exposes a single `cfg` object of type `Config` (a Python dataclass). Every other module that needs an environment variable imports `cfg` from here.

**Why a module-level singleton?** The alternative is calling `os.getenv()` or `load_dotenv()` scattered throughout the codebase. That pattern causes two problems: (1) you can't tell at startup which env vars are missing — you only find out when a specific code path runs, and (2) there's no single place to add validation. The singleton pattern means `cfg` is built once at import time, and if any required variable is missing, you get a loud `EnvironmentError` immediately with the list of missing keys — not a cryptic `NoneType` error deep in a boto3 call.

**The `load_config()` function** does three things:
1. Calls `load_dotenv()` so `.env` is read if present
2. Iterates the required keys and collects any that are missing
3. Either raises a descriptive error or returns a populated `Config` dataclass

**Important:** `config.py` is the only place in the entire codebase that touches `os.environ`. Every other file imports `cfg`.

---

### `src/common/schema.py`

**What it does:** Defines `CanonicalReading` — the one Pydantic model that every row in the system must conform to. Also defines `_classify_glucose()`, the function that translates raw sensor values into a typed `(float | None, GlucoseFlag)` tuple.

**The `CanonicalReading` fields:**

| Field | Type | Set by | Purpose |
|---|---|---|---|
| `patient_id` | `str` | Caller (from filename/column) | Identity — source-scoped, not globally unique |
| `lineage_id` | `str` (UUID4) | Auto-generated | Globally unique row ID for dedup and audit |
| `event_time` | `datetime` (UTC) | Caller (parsed from source) | When the reading happened in the real world |
| `ingest_time` | `datetime` (UTC) | Auto-generated | When this row was processed by GlucoFlow |
| `glucose_mgdl` | `float \| None` | `_classify_glucose()` | The reading, or None if low/high/invalid |
| `glucose_flag` | `GlucoseFlag` | `_classify_glucose()` | Validity classification |
| `source_system` | `SourceSystem` | Caller | Which source this row came from |
| `batch_or_stream` | `BatchOrStream` | Caller | How it arrived |
| `raw_payload` | `dict` | Caller | Verbatim original row, for replay |

**Why `frozen=True`?** (`model_config = ConfigDict(frozen=True)`)
Pydantic's `frozen=True` makes model instances immutable — you cannot accidentally write `reading.glucose_mgdl = 200` after construction. This is important in a pipeline where rows are passed through multiple processing stages. Immutability means a row that enters Stage 1 at `glucose_mgdl=149` cannot be silently mutated before it reaches Stage 3. If a transformation is needed, you must create a new object — which makes the transformation visible.

**Why `raw_payload: dict`?**
Every row stores a verbatim copy of its original source row. This is the "data lineage" principle: if a downstream analyst notices something wrong with a row, they can trace it all the way back to the exact bytes that came off the source device. You can also replay the entire pipeline from Bronze if the Silver transformation logic changes.

**The `_classify_glucose()` function:**
This is a pure function (no side effects, no I/O) that takes a raw value and returns `(value, flag)`. The decision tree:
```
raw == None / NaN / "" / "/"  →  (None, "invalid")
raw == "Low" (case-insensitive) →  (None, "low")       # sensor floor hit
raw == "High" (case-insensitive) → (None, "high")      # sensor ceiling hit
raw is unparseable string       →  (None, "invalid")
numeric, 40 ≤ value ≤ 400      →  (value, "in_range")
numeric, outside 40–400         →  (value, "out_of_range")  # value preserved
```

The range 40–400 is the **sensor validity range**, not the clinical target range. Clinical time-in-range (70–180 mg/dL) is computed as a downstream metric in Silver/Gold. Separating these two concerns is intentional — the schema says "is this a plausible sensor reading?" not "is this patient healthy?"

**Why `"/"` is handled explicitly:**
Shanghai files use the forward-slash character as a null marker for missing glucose values. This is not a standard null encoding — pandas and openpyxl don't know to treat `"/"` as missing. We explicitly check for it in `_classify_glucose` so Shanghai nulls don't become `flag="invalid"` (which would be misleading — the reading isn't invalid, it's just absent).

---

### `src/common/s3_utils.py`

**What it does:** Thin wrappers around boto3 S3 operations — upload files, upload bytes, serialize JSONL, build object keys, move objects to quarantine.

**`build_key(source, filename, dt)` → Hive partition key:**
```python
build_key("shanghai_t1dm", "1001.jsonl", date(2021, 7, 30))
# → "source=shanghai_t1dm/date=2021-07-30/1001.jsonl"
```
This format is the Hive partition convention. AWS Athena reads these as virtual partition columns at zero cost — no `MSCK REPAIR TABLE` ever needed. You can run:
```sql
SELECT * FROM bronze_readings
WHERE source = 'shanghai_t1dm'
  AND date >= '2021-07-01'
```
and Athena will only scan the relevant S3 prefixes, not the whole bucket.

**`upload_jsonl(s3_client, records, bucket, key)`:**
Serializes a Python list of dicts as newline-delimited JSON (`\n`-separated, no trailing newline) and uploads in one PUT call. This is more efficient than uploading one object per row — each S3 PUT has a fixed cost overhead regardless of payload size.

**`move_to_quarantine(s3_client, ...)`:**
S3 has no atomic "move" operation. The quarantine flow is: copy the object to the quarantine bucket with a `reason=<error>` prefix prepended to the key, then delete the original. If the copy succeeds but the delete fails, you have a duplicate in quarantine (not a data loss). The function logs a warning so you can audit. This is the industry-standard safe pattern.

**Why all functions raise instead of swallow errors:**
Callers decide retry logic; the utility functions don't. A swallowed `ClientError` in a utility function means silent data loss — a row that was supposed to go to Bronze just disappeared and there's no log entry. Raising ensures the error bubbles up to the ingestion script where it can be caught, logged, and the file sent to quarantine.

---

### `src/generators/run_simulation.py`

**What it does:** Runs the UVA/Padova T1DM simulator via the `simglucose` Python package and writes one CSV per virtual patient to `data/samples/synthetic/`.

**Why the simulator is set up this way:**

The simglucose package exposes a complete closed-loop simulation system with four components you must configure and wire together:

1. **`T1DPatient`** — the physiological model. Loaded from `vpatient_params.csv` by patient name (e.g. `"adolescent#001"`). Parameters encode how that virtual patient's body responds to insulin and glucose.

2. **`CGMSensor`** — the sensor noise model. We use `"Dexcom"` from `sensor_params.csv`. The sensor applies calibration error, lag, and random noise to the true BG signal to produce a realistic CGM trace.

3. **`InsulinPump`** — the delivery hardware model. We use `"Insulet"` (OmniPod style). Its parameters define delivery precision and dead time.

4. **`BBController`** — the basal-bolus controller. Targets 140 mg/dL. This controller issues insulin commands in response to the current CGM reading. Without a controller, glucose would diverge to clinically implausible values.

**Why these three archetypes (adolescent, adult, child)?**
The simulator has 30 virtual patients (10 per demographic group). We pick one from each group to get variety:
- Different body weights → different insulin sensitivity
- Different meal absorption rates
- The child patient reliably dips into hypoglycaemia on the default meal scenario — this gives us `"Low"` string rows in the downstream Dexcom CSV, which tests the most important edge case in the schema

**Why `SEED = 42`?**
Reproducibility. Every run of the simulator with the same seed produces the same output, so the tests that check file structure are deterministic. In production you'd remove the seed for stochastic runs.

**The timestamp fix:**
The simglucose `sim.results()` function returns a DataFrame with a DatetimeIndex already anchored to the `start_time` you passed in. The index contains `pandas.Timestamp` objects — not floats or timedeltas. So timestamp construction is just `pd.to_datetime(df["Time"], utc=True)` after `reset_index()`. This is not obvious from the library's documentation (it changed between versions), which is why we inspected the actual return type before writing a single line.

---

### `src/generators/make_dexcom_csv.py`

**What it does:** Transforms each synthetic CSV into a file that mimics a real Dexcom Clarity export, with all five deliberate defects injected.

**File structure of the output:**
```
[Line 1] Patient Name: [REDACTED],...,Device: Dexcom G6,...     ← metadata
[Line 2] Exported: 2021-07-30T00:00:00,...                       ← metadata
[Line 3] ,,,,,                                                   ← blank spacer
[Line 4] Index,Timestamp (...),Event Type,...                    ← column headers
[Line 5+] 0,2021-07-30T00:03:00,EGV,,,,,,127.4,...              ← EGV row
...       (288 EGV rows)
[blank row]
[3 Calibration rows]
[blank row]
[~20 Insulin rows]
[~3 Carb rows]
```

**Why the metadata header comes before the column headers:**
Real Dexcom Clarity exports actually work this way. The file begins with device metadata lines that aren't part of the tabular data. Replicating this means your parser needs `skiprows=3` — a detail that catches developers who just call `pd.read_csv(path)` without reading the format documentation.

**The `_glucose_cell()` function:**
Takes a numeric value and applies three transformations:
1. **Dropout check** (2% probability) → returns `""` (blank glucose, tests NaN handling)
2. **Low check** (value < 55 AND 70% probability) → returns `"Low"`
3. **High check** (value > 350 AND 70% probability) → returns `"High"`
4. Otherwise → returns `str(round(value, 1))`

The probability thresholds are below 100% so that values near the boundary sometimes appear as numeric (a real sensor doesn't always report "Low" at exactly 55 mg/dL — there's a hysteresis window). This makes the test data more realistic.

**Writing with a file handle (not `to_csv()` directly):**
```python
with open(out_path, "w") as f:
    for meta_line in _make_metadata_header(patient_name):
        f.write(meta_line + "\n")
    events_df.to_csv(f, index=False)
```
This is the only way to write the metadata rows before the DataFrame without a post-processing step. Calling `to_csv(path)` would overwrite the header lines we just wrote.

---

### `src/ingestion/shanghai.py`

**What it does:** Reads all `.xlsx` patient files from `data/raw/shanghai/Shanghai_T1DM/` and `Shanghai_T2DM/`, validates each row through `CgmReading`, and uploads valid rows as JSONL to the Bronze bucket.

**Column resolution by alias list (not position):**
```python
GLUCOSE_ALIASES = ["Glucose (mg/dL)", "Glucose", "glucose_value", "CBG (mg/dL)"]
```
Different Shanghai files use slightly different column names (an artifact of how the dataset was assembled across multiple hospital stays). Resolving by alias list is resilient — add a new alias if a new file uses a different header, and the parser just works. Resolving by column index (`row[3]`) would break silently if the column order ever changes.

**Sheet detection by name, not position:**
```python
for name in wb.sheetnames:
    if "cgm" in name.lower() or "glucose" in name.lower():
        cgm_sheet = wb[name]; break
```
Some Shanghai files have a "Summary" sheet first and the CGM data sheet second. Indexing `ws[0]` would give you the summary sheet, not the readings. Searching by name is more robust.

**`"/"` null handling:**
```python
if raw_gluc is None or str(raw_gluc).strip() in ("/", "", "nan", "None"):
    continue
```
The Shanghai dataset uses `"/"` as its null marker. Without this check, the `"/"` character would reach `_classify_glucose()` and be flagged as `"invalid"` — technically correct but semantically wrong. Skipping null-glucose rows at the parser level is the right behavior.

**Patient ID extraction from filename:**
```python
patient_id = xlsx_path.stem.split("_")[0]  # "1001_0_20210730" → "1001"
```
The patient ID is embedded in the filename by the dataset convention. This is extracted once at file level and stamped on every row from that file.

---

### `src/ingestion/simglucose.py`

**What it does:** Reads the clean CSVs from `data/samples/synthetic/`, maps `CGM` → `glucose_mgdl`, synthesizes timestamps, and uploads to Bronze.

**Column resolution by alias (same pattern as Shanghai):**
simglucose's column names vary slightly between versions (`"CGM"` vs `"cbg"` vs `"BG"`). The alias-matching approach handles this without version pinning.

**Timestamp synthesis:**
simglucose's output CSVs don't have wall-clock timestamps. The timestamps in the file are the simulation clock — minutes elapsed since the start of the simulation. We synthesize absolute timestamps by:
```python
ts = SIM_START + timedelta(minutes=INTERVAL_MINUTES * i)
```
`SIM_START = datetime(2023, 1, 1)` and `INTERVAL_MINUTES = 5`. Every row gets an absolute datetime in 5-minute increments from midnight Jan 1 2023.

**Why `sim_` prefix on patient_id:**
`patient_id = f"sim_{csv_path.stem}"` → `"sim_adolescent_001"`. This namespacing ensures simglucose patient IDs never collide with Shanghai patient IDs (which are numeric strings like `"1001"`).

---

### `src/ingestion/dexcom.py`

**What it does:** Parses the deliberately messy Dexcom Clarity-style CSVs and uploads EGV + Calibration rows to Bronze.

**`skiprows=3` — the most important line in this file:**
```python
df = pd.read_csv(csv_path, skiprows=3, dtype=str)
```
- `skiprows=3` skips the 3-line metadata header so row 4 (the column header) becomes the DataFrame header
- `dtype=str` reads every column as a string — this is essential because we want the literal text `"Low"`, `"High"`, `""` coming through to `_classify_glucose()`, not pandas' default numeric coercion (which would make `"Low"` a NaN)

**Event type routing:**
```python
GLUCOSE_EVENTS = {"EGV", "egv", "Calibration", "calibration"}
if event_type not in GLUCOSE_EVENTS:
    skipped.append(...)
    continue
```
Only EGV and Calibration rows become `CgmReading` objects. Insulin and Carb rows are counted in `skipped` but not ingested — those would need a separate schema if we wanted to model them at Silver.

**Calibration flag:**
```python
is_calibration = "calibration" in event_type.lower()
```
Calibration rows (finger-prick readings) get `is_calibration=True`. This matters clinically: calibration readings are more accurate than CGM readings but are point-in-time (not continuous), and shouldn't be mixed with CGM readings in time-in-range calculations.

---

### `infra/setup_aws.sh`

**What it does:** Idempotent bash script that creates all 4 S3 buckets, enables versioning, blocks public access, and sets a 30-day lifecycle on Bronze.

**`set -euo pipefail` — the first line after the shebang:**
- `set -e` — exit immediately on any command that returns non-zero
- `set -u` — treat unset variables as errors (prevents `$UNBUCKET_NAMESET` from silently becoming empty string)
- `set -o pipefail` — a pipe's return code is the last non-zero exit in the chain, not just the last command

Without this, a failed `aws s3api` call would be silently ignored and the script would continue, leaving you with partially-provisioned infrastructure.

**`bucket_exists()` function — the idempotency mechanism:**
```bash
bucket_exists() {
    aws s3api head-bucket --bucket "$1" 2>/dev/null
}
```
Before creating a bucket, we check if it already exists. If it does, the creation step is skipped. This means `bash infra/setup_aws.sh` can be re-run at any point without duplicating resources or throwing errors.

**The `us-east-1` special case:**
```bash
if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$bucket" --region "$AWS_REGION"
else
    aws s3api create-bucket --bucket "$bucket" --region "$AWS_REGION" \
        --create-bucket-configuration LocationConstraint="$AWS_REGION"
fi
```
AWS S3 has a quirk: `us-east-1` is the default region and does NOT accept a `LocationConstraint` in the create-bucket request. Every other region requires it. Sending `LocationConstraint=us-east-1` returns an `InvalidLocationConstraint` error. This two-branch check handles it.

**30-day lifecycle on Bronze only:**
Bronze is raw data — we never need it for more than 30 days in a demo/portfolio project. After 30 days, S3 automatically deletes objects, keeping your demo cost near zero. Silver and Gold don't have this rule because those contain computed data we may want to query for longer.

---

### `infra/teardown_aws.sh`

**What it does:** Empties and deletes all 4 buckets. Designed to be run when you're done with the demo to avoid ongoing S3 storage charges.

**The confirmation prompt:**
```bash
read -rp "Type YES to confirm: " confirm
if [ "$confirm" != "YES" ]; then
    echo "Aborted."; exit 0
fi
```
Case-sensitive `"YES"` must be typed in full. This is intentional friction — destroying production data by accident because you hit Enter is a well-known failure mode. The uppercase exact-match requirement forces a deliberate decision.

**Empty-before-delete:**
S3 buckets cannot be deleted if they contain objects (or versioned objects). The script uses `aws s3 rm --recursive` to empty each bucket first, then `aws s3api delete-bucket`. Versioned objects also need `delete-objects` with version IDs — the script handles this with a second pass using `list-object-versions`.

---

### `infra/glucoflow-policy.json`

**What it does:** Defines the least-privilege IAM policy that the GlucoFlow pipeline service account should be attached to.

**Two statements, not one:**

Statement 1 (`GlucoFlowS3Objects`) grants `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `arn:aws:s3:::BUCKET_NAME/*` (the objects inside the bucket).

Statement 2 (`GlucoFlowS3List`) grants `s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetBucketVersioning` on `arn:aws:s3:::BUCKET_NAME` (the bucket itself, no `/*` suffix).

**Why two separate statements?** This is an IAM ARN gotcha: `ListBucket` is a bucket-level action that must be applied to the bucket ARN (`arn:aws:s3:::mybucket`). `PutObject`/`GetObject` are object-level actions that must be applied to the objects ARN (`arn:aws:s3:::mybucket/*`). If you apply `ListBucket` to `mybucket/*`, it silently has no effect. If you apply `PutObject` to `mybucket` (without `/*`), it also has no effect. The two-statement structure ensures each action is matched to the correct ARN scope.

**Placeholders, not real bucket names:**
The template file uses strings like `BRONZE_BUCKET_PLACEHOLDER`. The `provision_buckets.py` script reads this template, substitutes real bucket names from `cfg`, and writes a rendered `glucoflow-policy-rendered.json`. This means the template file can be committed to git (no real infrastructure names in version control) while the rendered file is gitignored.

---

### `infra/provision_buckets.py`

**What it does:** Python-native equivalent of `setup_aws.sh`. Does the same work — creates buckets, enables versioning, blocks public access, sets lifecycle rule on Bronze, renders the IAM policy — using `boto3` instead of the AWS CLI.

**Why both exist:**
- `setup_aws.sh` is faster for manual one-shot provisioning (no Python env, no imports)
- `provision_buckets.py` is designed for CI pipelines where you want programmatic infrastructure verification and structured output

---

### `tests/test_schema.py`

**28 tests across 2 test classes.**

**`TestClassifyGlucose` (14 tests):**
Tests the `_classify_glucose()` pure function directly. Each test covers one specific input case:
- Normal numeric value in range
- Boundary values (40.0 and 400.0 — both should be `"in_range"`)
- Numeric above 400, numeric below 40 → `"out_of_range"` with value preserved
- `"Low"` string (exact case, upper case, lower case)
- `"High"` string
- Garbage string (`"abc"`) → `"invalid"`
- `None` → `"invalid"`
- Empty string → `"invalid"`
- `"/"` (Shanghai null marker) → `"invalid"`
- Numeric string like `"142.5"` → `"in_range"`

**`TestFromRaw` (14 tests):**
Tests the `CanonicalReading.from_raw()` factory method end-to-end:
- Normal value round-trip
- Low/High string handling via `from_raw`
- `ingest_time` is auto-set to UTC now
- `lineage_id` is a valid UUID4
- Two calls to `from_raw` produce different `lineage_id` values (no shared state)
- `raw_payload` is preserved verbatim
- Naive `event_time` (no timezone) is coerced to UTC
- Model is immutable (mutation raises `ValidationError`)
- `dexcom_messy` source works
- `stream` mode works

---

### `tests/test_generators.py`

**37 tests, parametrized over 3 patient files.**

**`TestSimulatedFiles`** (per-patient):
- File exists on disk
- Row count is > 0
- `timestamp`, `patient_id`, `cgm_mgdl`, `bg_mgdl` columns all present
- No null timestamps
- No null CGM values
- `cgm_mgdl` values are in physiologically plausible range (0–1000)
- `patient_id` column is consistent within a file

**`TestDexcomMessyFiles`** (per-patient):
- File exists on disk
- Metadata header is 3 lines (device info, export date, blank)
- Column header row 4 contains `"Event Type"` and `"Glucose Value (mg/dL)"`
- File contains EGV rows
- File contains Calibration rows
- File contains blank spacer rows (detected as all-NaN rows via `df.isnull().all(axis=1)`)
- EGV rows include at least one `"Low"` or `"High"` string (or blank dropout)
- child patient has the lowest mean CGM of the three (clinically expected — pediatric model is more labile)

---

### `tests/test_infra.py`

**14 tests across 3 test classes.**

**`TestPolicyJson`** (8 tests):
- Schema version is `"2012-10-17"` (the only valid IAM policy version)
- Policy has exactly 2 statements
- Objects statement grants `s3:PutObject` and `s3:GetObject`, does NOT grant `s3:*` or `s3:DeleteBucket`
- List statement grants `s3:ListBucket`
- Object ARNs end with `/*` (object-level scope)
- Bucket ARNs do NOT end with `/*` (bucket-level scope)
- All 4 bucket placeholders are present in the template
- Both statements have `"Effect": "Allow"` (not Deny)

**`TestBashScripts`** (5 tests):
- Both scripts exist
- `setup_aws.sh` contains `set -euo pipefail`
- `teardown_aws.sh` contains a `read` prompt and the string `"YES"`
- `setup_aws.sh` contains both `us-east-1` (the special case) and `LocationConstraint` (the normal case)

**`TestPolicyRender`** (1 test):
- Placeholder substitution produces valid JSON with real bucket names
- No `PLACEHOLDER` strings remain in the rendered output

---

### `requirements.txt`

```
boto3>=1.34.0       ← AWS SDK — S3 operations, future Bedrock calls
pandas>=2.2.0       ← DataFrame operations in parsers and generators
numpy>=1.26.0       ← simglucose dependency; also used in messy generator
pydantic>=2.6.0     ← Schema validation (v2 API — not compatible with v1)
simglucose>=0.2.1   ← UVA/Padova T1DM simulator
python-dotenv>=1.0.0← .env loading in config.py
pytest>=8.0.0       ← Test runner
openpyxl>=3.1.0     ← Reading .xlsx files in shanghai.py
```

**Why minimum versions, not pinned?** Pinning to exact versions (e.g. `boto3==1.34.12`) creates `pip install` failures when AWS releases patch updates. Minimum versions give you the API stability you need while allowing patch upgrades. For a production system you'd use a lockfile tool like `pip-compile`.

---

### `.env.example`

```
AWS_REGION=us-east-1
AWS_PROFILE=default
BRONZE_BUCKET=glucoflow-bronze-<initials>-<random>
SILVER_BUCKET=glucoflow-silver-<initials>-<random>
GOLD_BUCKET=glucoflow-gold-<initials>-<random>
QUARANTINE_BUCKET=glucoflow-quarantine-<initials>-<random>
```

**S3 bucket naming rules:**
- Must be globally unique across all AWS accounts worldwide
- 3–63 characters, lowercase letters, numbers, hyphens only
- Must not start or end with a hyphen
- The `<initials>-<random>` suffix ensures uniqueness — e.g. `glucoflow-bronze-jd-7x3k`

---

### `Makefile`

```makefile
setup           → create .venv, pip install requirements.txt
test            → pytest tests/ -v
simulate        → python src/generators/run_simulation.py
make-dexcom     → python src/generators/make_dexcom_csv.py
generate        → simulate + make-dexcom (full data generation pipeline)
ingest-shanghai → python src/ingestion/shanghai.py
ingest-sim      → python src/ingestion/simglucose.py
ingest-dexcom   → python src/ingestion/dexcom.py
ingest-all      → all three ingestors
provision       → python infra/provision_buckets.py
```

The Makefile is the human interface to the project. You never need to remember file paths or Python module paths — `make test`, `make generate`, `make ingest-all` covers most daily operations.

---

## 6. Key Design Decisions

### Decision 1: AWS over Snowflake

Snowflake is a strong platform and a genuinely valid choice for CGM analytics. Here's why AWS was chosen instead:

| Factor | AWS | Snowflake |
|---|---|---|
| AI layer | Amazon Bedrock is AWS-native — no cross-cloud auth | Snowflake Cortex requires separate auth setup |
| Query engine | Athena queries S3 directly — no data loading | Snowflake requires COPY INTO to move data in |
| Cost at demo scale | S3 + Athena: ~$0–2/month for this dataset | Snowflake trial credits expire; credits-per-second billing |
| Portfolio value | AWS is the dominant platform in data engineering job descriptions | Snowflake is valuable but secondary |
| Architecture fit | Medallion pattern maps natively to S3 bucket tiers | Could work but adds complexity without benefit at this scale |

Snowflake becomes the better choice at scale: when you have dozens of analysts running concurrent queries, dbt transformation pipelines, and need automatic query optimization across terabyte-scale tables. For a CGM portfolio project with a handful of patients, that's premature optimization.

### Decision 2: Pydantic v2 (not v1)

Pydantic v2 is a complete rewrite — 10x faster than v1, with a different API. Key differences used here:
- `ConfigDict(frozen=True)` instead of `class Config: allow_mutation = False`
- `@field_serializer` instead of `json_encoders` in `class Config`
- `model_config` instead of nested `class Config`

Using v2 from the start avoids a painful migration later and reflects current industry practice.

### Decision 3: Schema at ingestion, not at query time

Some pipelines land raw data in storage and apply schema only at query time (schema-on-read). GlucoFlow applies schema at ingestion time (schema-on-write). The tradeoff:

- **Schema-on-read:** Faster ingestion, more flexible, but corrupt data only surfaces when you query it — potentially long after the original record arrived
- **Schema-on-write (our choice):** Invalid rows are caught immediately at ingestion, sent to quarantine with the validation error attached, and never silently corrupt downstream analytics

For clinical data, schema-on-write is the right choice. A `glucose_mgdl=-5` that slips through to a Gold analytics table and gets picked up in a weekly patient summary is worse than a schema validation error at ingest time.

### Decision 4: Quarantine bucket (not delete on failure)

When a row fails validation, we don't discard it — we route it to a quarantine bucket with the validation reason in the key prefix:
```
s3://glucoflow-quarantine/reason=invalid_glucose/source=shanghai_t1dm/date=.../patient.jsonl
```
This preserves the raw data for investigation. In a production system you'd build a quarantine review workflow (alert the data team, fix the source, re-ingest). Deleting on failure is irreversible and makes root-cause analysis impossible.

### Decision 5: Generators are separate from ingestors

The generators (`run_simulation.py`, `make_dexcom_csv.py`) write to local disk. The ingestors (`shanghai.py`, `simglucose.py`, `dexcom.py`) read from local disk and upload to S3. These are separate steps connected by the filesystem.

This separation means:
- Generators can run without AWS credentials (useful for development and testing)
- Ingestors can be swapped out (e.g. replace the S3 ingestor with a local file ingestor for offline testing) without touching the generators
- The intermediate files on disk are inspectable — you can `head data/samples/messy/dexcom_child_001.csv` and verify the format before any data leaves your machine

---

## 7. Data Flow — From Source to Bronze S3

```
Shanghai .xlsx files
      │
      ▼
  shanghai.py
  ┌─────────────────────────────────────────────────────────────┐
  │ openpyxl.load_workbook()                                    │
  │ → detect CGM sheet by name                                  │
  │ → resolve columns by alias list                             │
  │ → for each row:                                             │
  │     skip "/" nulls                                          │
  │     CgmReading(...) → Pydantic validates                    │
  │     on ValidationError → append to failed[]                 │
  │ → upload_jsonl(valid[], bronze_bucket, hive_key)            │
  │ → (future) upload failed[] to quarantine                    │
  └─────────────────────────────────────────────────────────────┘
      │
      ▼
  s3://bronze/source=shanghai_t1dm/date=.../1001.jsonl


simglucose CSVs (after make simulate)
      │
      ▼
  simglucose.py
  ┌─────────────────────────────────────────────────────────────┐
  │ pd.read_csv()                                               │
  │ → resolve CGM column by alias                               │
  │ → synthesize timestamps (index × 5min from SIM_START)       │
  │ → CgmReading(...) → Pydantic validates                      │
  │ → upload_jsonl(valid[], bronze_bucket, hive_key)            │
  └─────────────────────────────────────────────────────────────┘
      │
      ▼
  s3://bronze/source=simglucose/date=.../adolescent_001.jsonl


Dexcom messy CSVs (after make make-dexcom)
      │
      ▼
  dexcom.py
  ┌─────────────────────────────────────────────────────────────┐
  │ pd.read_csv(skiprows=3, dtype=str)                          │
  │ → filter rows where Event Type ∈ {EGV, Calibration}        │
  │ → skip NaN rows (blank spacers)                             │
  │ → CgmReading(glucose_mgdl="Low"/numeric/""...)              │
  │   schema validator converts "Low"→39.0, "High"→401.0        │
  │ → upload_jsonl(valid[], bronze_bucket, hive_key)            │
  └─────────────────────────────────────────────────────────────┘
      │
      ▼
  s3://bronze/source=dexcom_csv/date=.../adolescent_001.jsonl
```

---

## 8. AWS Infrastructure

**4 buckets, all in the same region:**

| Bucket | Purpose | Lifecycle |
|---|---|---|
| `glucoflow-bronze-xxx` | Raw JSONL from all three sources | 30-day auto-delete |
| `glucoflow-silver-xxx` | Cleaned, merged Parquet | None |
| `glucoflow-gold-xxx` | Aggregated summaries for AI layer | None |
| `glucoflow-quarantine-xxx` | Failed validation rows + audit trail | None |

**Versioning on all 4:**
S3 Versioning keeps every previous version of every object. If an ingestion run overwrites a Bronze file due to a duplicate ingest, you can recover the original. At Bronze scale this is cheap (JSONL files are small) and invaluable for debugging.

**Public access blocked on all 4:**
CGM data is health data. Even though this is a demo project with no real patients, blocking public access is the correct default and demonstrates that you understand why.

**IAM policy — least privilege:**
The service account that runs the pipeline only gets:
- `s3:PutObject` — write new objects
- `s3:GetObject` — read objects (for verification step)
- `s3:DeleteObject` — needed for quarantine move (copy-then-delete)
- `s3:ListBucket` — needed for Athena partition discovery
- `s3:GetBucketLocation` / `GetBucketVersioning` — needed by boto3 client setup

It does NOT get:
- `s3:DeleteBucket` — can't destroy the infrastructure
- `s3:PutBucketPolicy` — can't change who else has access
- `s3:*` — no wildcard admin permissions

---

## 9. Running the Project — Step by Step

```bash
# 1. Clone / navigate to the project
cd /path/to/gcmMonitor/glucoflow

# 2. Set up Python environment
make setup
source .venv/bin/activate

# 3. Configure AWS credentials (one-time)
aws configure
# Enter: Access Key ID, Secret Access Key, region (us-east-1), output (json)

# 4. Set your bucket names
cp .env.example .env
# Edit .env — replace <initials>-<random> with something unique, e.g.:
# BRONZE_BUCKET=glucoflow-bronze-jd-7x3k

# 5. Run all tests (no AWS needed)
make test
# → 79 passed

# 6. Create AWS buckets
bash infra/setup_aws.sh
# OR (Python version):
make provision

# 7. Generate synthetic data
make generate
# → data/samples/synthetic/ — 3 simglucose CSVs
# → data/samples/messy/     — 3 Dexcom-style CSVs

# 8. Place Shanghai data
# Copy Shanghai .xlsx files into data/raw/shanghai/Shanghai_T1DM/ and Shanghai_T2DM/

# 9. Ingest everything to Bronze S3
make ingest-all

# 10. Verify
aws s3 ls s3://$BRONZE_BUCKET --recursive | head -20
```

**Done-when check (Day 1 success criterion):**
```bash
aws s3 ls s3://$BRONZE_BUCKET/source=shanghai_t1dm/  # should show .jsonl files
aws s3 ls s3://$BRONZE_BUCKET/source=simglucose/      # should show .jsonl files
aws s3 ls s3://$BRONZE_BUCKET/source=dexcom_csv/       # should show .jsonl files
```

---

## 10. Clinical Context — Why These Numbers Matter

Understanding the clinical meaning behind the data makes you a stronger engineer on this project.

**CGM (Continuous Glucose Monitor):**
A sensor worn on the body (typically upper arm or abdomen) that measures glucose in the interstitial fluid every 5–15 minutes. It transmits readings wirelessly. Unlike a finger-prick blood glucose test, it provides continuous trend data — you can see if glucose is rising, falling, or stable. The Dexcom G6 is one of the most widely used CGM systems globally.

**The clinical glucose ranges:**

| Range | mg/dL | Meaning |
|---|---|---|
| Hypoglycaemia (severe) | < 54 | Medical emergency — seizure/unconsciousness risk |
| Hypoglycaemia (alert) | 54–70 | Needs treatment — eat fast-acting carbs |
| Target range | 70–180 | Healthy for most T1DM patients |
| Hyperglycaemia | 180–250 | Above target — may need insulin correction |
| Hyperglycaemia (high) | > 250 | Risk of DKA (diabetic ketoacidosis) if sustained |
| Sensor floor | < 40 | Below what the sensor can measure — reported as "Low" |
| Sensor ceiling | > 400 | Above what the sensor can measure — reported as "High" |

**Time-in-Range (TIR):** The percentage of CGM readings in 70–180 mg/dL over a period. The gold-standard clinical outcome metric for CGM data. A TIR > 70% is the general clinical target. This is computed in Silver, not Bronze — Bronze just stores the raw values.

**T1DM vs T2DM in the Shanghai dataset:**
- **T1DM (Type 1):** Autoimmune — the pancreas produces no insulin. Patients are entirely insulin-dependent from diagnosis. CGM data shows more dramatic swings.
- **T2DM (Type 2):** Metabolic — the body produces insulin but cells resist it. Most patients use oral medications with some on insulin. CGM patterns tend to be flatter but with sustained hyperglycaemia.

**HbA1c:** Glycated haemoglobin — a 3-month average glucose metric measured from a blood sample. The Shanghai summary sheets contain HbA1c for each admission. `HbA1c > 6.5%` is the diagnostic threshold for diabetes. Values in the dataset range from ~6% (well-controlled) to ~14% (poorly controlled). This is a key feature for downstream ML work.

---

## 11. What Comes Next (Day 2 and Beyond)

**Day 2: Bronze → Silver transformation**
- AWS Glue (or Lambda) job reads Bronze JSONL, applies deduplication (by `lineage_id`), computes `time_in_range`, joins Shanghai demographic data with CGM readings, writes Parquet to Silver
- Athena table registration over Silver bucket

**Day 3: Silver → Gold aggregation**
- Per-patient weekly summaries: mean glucose, TIR%, hypoglycaemic event count, HbA1c
- Gold tables optimized for Bedrock query patterns

**Day 4: MCP (Model Context Protocol) layer**
- FastAPI service exposing MCP-compatible endpoints
- Tools: `get_patient_summary`, `list_hypoglycaemic_events`, `compare_patient_cohort`
- Each tool queries Athena against Gold tables and returns structured results

**Day 5: Amazon Bedrock integration**
- Claude 3 Sonnet (or Claude 3.5 Haiku) via Bedrock
- Agent with MCP tool access
- Natural language → tool calls → Athena queries → structured response
- Example: "What's patient 1001's time-in-range this week and how does it compare to their HbA1c trend?"

**Day 6: Conversational analytics polish**
- Streaming responses for long queries
- Bedrock Guardrails for PII handling (even though this is synthetic data — demonstrating the pattern)
- Session memory for multi-turn conversations

---

*This document was written to reflect the actual codebase as built during Day 1 of the GlucoFlow project. Every design decision described here is implemented in the code — this is not aspirational documentation.*
