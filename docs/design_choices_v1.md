# GlucoFlow — Design Choices & Implementation Reasoning

*A living document explaining every architecture and technology decision in this project. Written to demonstrate domain knowledge and engineering judgment, not just implementation steps.*

---

## 1. What This Project Is — and Why It Matters

GlucoFlow is a cloud-native data engineering pipeline built around **Continuous Glucose Monitor (CGM) data**. The premise is simple: real patients with diabetes wear small sensors that measure blood sugar every 15 minutes, around the clock. Those sensors produce a continuous stream of timestamped readings — and that data, when managed well, can help clinicians make better treatment decisions.

The engineering challenge is real. A single patient in hospital generates ~96 readings per day. At any meaningful clinical scale — a ward, a clinic, an EHR system — you have thousands of patients, multiple device brands with inconsistent export formats, and data arriving both in batches (historical files) and streams (live sensor feeds). Add in lab results, medication logs, and dietary records, and you have a genuinely interesting data engineering problem that touches partitioning, schema design, quality flagging, and AI-powered querying.

**This project exists to demonstrate that we can build the full stack of that system**, from raw data landing to an AI layer that lets a clinician ask natural-language questions and get real answers backed by live data. Every decision documented below was made with that goal in mind.

---

## 2. The Datasets — What We're Actually Working With

Before any pipeline decision is meaningful, you have to understand the data. This project works with three distinct data sources, each chosen deliberately to represent a different type of real-world ingestion challenge.

---

### 2.1 The Shanghai T1DM/T2DM Dataset — Real Clinical CGM Data

**What it is:** This is a published academic dataset of real CGM measurements from diabetic inpatients at a hospital in Shanghai, China. It was released in 2023 by Zhao et al. in *Scientific Data* (a Nature Portfolio journal) and is freely available on Figshare under an open-access license. It contains data from patients with both **Type 1 Diabetes (T1DM)** and **Type 2 Diabetes (T2DM)**, making it one of the most clinically complete publicly available CGM datasets in existence.

**Why it matters:** Most publicly available CGM datasets are either synthetic, incomplete, or cover only one type of diabetes. This dataset has both types, real complications data, real medications, and real lab values. It's messy in the ways real clinical data is messy — some readings are missing, some are partial hospitalisation visits, some patients have multiple admission records. That's the point.

**The structure — two layers:**

Every patient has two types of files:

1. **Summary sheet** (`Shanghai_T1DM_Summary.xlsx` / `Shanghai_T2DM_Summary.xlsx`): One row per hospital admission. Contains 33 clinical columns covering:
   - **Demographics:** Patient ID, sex, age, height, weight, BMI
   - **Lifestyle:** Smoking history (pack years), alcohol use
   - **Diabetes profile:** Type, duration of diagnosis (some patients have had it for 26+ years; some are newly diagnosed, under a month)
   - **Complications:** Acute (e.g. diabetic ketoacidosis), macrovascular (coronary heart disease, peripheral arterial disease, cerebrovascular disease), microvascular (neuropathy, retinopathy, nephropathy)
   - **Comorbidities:** Hypertension, hyperlipidemia, thyroid nodules, osteoporosis, fatty liver disease — the full picture of a real hospitalised diabetic patient
   - **Lab values at admission:** Fasting plasma glucose, 2-hour postprandial glucose, HbA1c (long-term glycemic control marker), C-peptide and insulin levels, lipid panel (cholesterol, triglycerides, HDL/LDL), kidney function (creatinine, eGFR, uric acid, BUN)
   - **Medications:** Hypoglycemic agents (insulin types, metformin, SGLT2 inhibitors, GLP-1 agonists, etc.) and other agents
   - **Hypoglycemia flag:** Whether the patient experienced a dangerous low blood sugar event during their stay

2. **Individual CGM files** (one `.xlsx` per admission in `Shanghai_T1DM/` and `Shanghai_T2DM/` folders): Named using the format `patientID_visitNumber_date.xlsx`. Each row is a 15-minute interval and may contain:
   - **Date/time** (the primary key — every row is anchored to a timestamp)
   - **CGM reading** in mg/dL — the sensor's blood glucose estimate
   - **CBG** (Capillary Blood Glucose) — a finger-prick "ground truth" reading, used for calibration
   - **Blood ketone** level (elevated in diabetic ketoacidosis)
   - **Dietary intake** — meal events logged during the admission
   - **Insulin doses** — subcutaneous injections, IV drips, and CSII (insulin pump) bolus and basal rates
   - **Non-insulin hypoglycemic agents** — other medications given during CGM wear

**What the data tells us:** The combination of the summary sheet (who the patient is and what their baseline looks like) with the time-series file (what their glucose actually did during the admission) is what makes this dataset clinically valuable. We can see, for example, that Patient 1002 — a 68-year-old male, 26 years with T1DM, active ketoacidosis on admission — has neuropathy, retinopathy, and nephropathy simultaneously. We can then look at his CGM trace and see exactly how his glucose behaved across his three separate admissions.

**The patient cohort:**
- T1DM cohort: 12 unique patients (some with multiple admissions = 16 total files), ages ranging from 37 to 73, diabetes durations from 1 month to 26 years
- T2DM cohort: 17+ unique patients (~20+ files), ages 35 to 84, durations from weeks to 25 years
- The dataset was updated in October 2023 to add 188 new CGM measurements to two T2DM patients (2003 and 2029), illustrating how real clinical datasets evolve over time

**Data quality realities we have to handle:**
- Some lab values are recorded as `/` (not measured) rather than NULL — we must parse and coerce these
- Multiple admissions for the same patient create the need for a visit/encounter model, not just a patient model
- CGM readings can be missing for periods when the sensor was removed or ran out of fluid
- Glucose values outside 40–400 mg/dL are typically reported as "Low" or "High" strings rather than numbers

**Regulatory note:** Before using this data in any public or commercial context, verify the specific license terms on the Figshare record. The dataset is published as open-access in an academic journal, but clinical data has additional considerations.

---

### 2.2 simglucose — Synthetic CGM Data from a Physics Simulator

**What it is:** `simglucose` is an open-source Python package that implements the **UVA/Padova Type 1 Diabetes Mellitus Simulator** — the same simulator that the FDA has accepted as a substitute for animal trials in closed-loop insulin pump research. It models glucose-insulin dynamics using differential equations derived from real metabolic physiology.

**Why we include it:** Two reasons. First, the Shanghai dataset has ~30 patients. For a data pipeline demo, we want more volume and we want data we can generate freely without license constraints. Second, simglucose lets us create patients with known characteristics and controlled scenarios — we can generate a patient who goes hypoglycemic at 3 AM, or a patient whose post-meal spikes are exactly 2.5x their baseline, and verify our pipeline handles those cases correctly.

**What it produces:** Five-minute interval readings with realistic sensor noise (using a Dexcom-equivalent CGM sensor model). The output is clean, structured, and consistently formatted — which is precisely the opposite of the next data source.

**What it demonstrates:** Working with a well-established scientific simulation tool, generating synthetic data at scale, and integrating third-party Python packages into a data pipeline.

---

### 2.3 Dexcom-Style "Messy" CSV Exports — Real-World Device Complexity

**What it is:** A programmatically generated dataset that mimics the actual CSV export format produced by Dexcom Clarity — the consumer-facing software that Dexcom CGM users download their data with. This is not official Dexcom data; it's a faithful simulation of the format's quirks.

**Why this matters enormously:** In the real world, a hospital's data team doesn't receive clean, schema-consistent files. They receive whatever the device exported. The Dexcom Clarity CSV export is a perfect example of real-world messiness:
- The first several rows are **metadata headers** (patient name, device serial, transmitter ID, export timestamp) that are not part of the tabular data at all
- The file mixes **event types in a single column**: glucose readings ("EGV"), calibration events, insulin logs, and carbohydrate entries are all interleaved
- Glucose values below 40 mg/dL are written as the string `"Low"` and values above 400 mg/dL as `"High"` rather than as numbers
- There are occasional **duplicate rows** (device firmware quirk) and **blank glucose fields** on rows that should have values

**What this tests in our pipeline:** The ingestion layer must be robust enough to handle all of this without crashing. It must strip the metadata preamble, filter to EGV rows only, parse the Low/High strings into proper flags, deduplicate, and normalize the result into the canonical schema. This is the kind of preprocessing that takes up 70% of real data engineering work — and demonstrating it cleanly is the point.

---

## 3. Why a Medallion Architecture?

The pipeline uses a three-zone data lake pattern called the **Medallion Architecture** (popularised by Databricks but now an industry standard):

| Zone | S3 Prefix | What Lives Here |
|------|-----------|-----------------|
| **Bronze** | `/bronze/` | Raw, untouched files exactly as they arrived. Never modified. |
| **Silver** | `/silver/` | Validated, deduplicated, schema-normalized readings. |
| **Gold** | `/gold/` | Aggregated, analytics-ready outputs (e.g. daily TIR per patient). |
| **Quarantine** | `/quarantine/` | Records that failed validation — kept for debugging, not discarded. |

**Why not just dump everything into one bucket?** Because the moment you overwrite raw data during processing, you lose the ability to reprocess. If your normalization logic has a bug and you've already transformed the source, you have no recovery path. Bronze is the immutable record of what we received. Everything downstream is derived and reproducible.

**The quarantine zone is not an afterthought.** In healthcare data, a "bad" record isn't just a pipeline inconvenience — it could be a sensor calibration issue, a device firmware bug, or a genuine clinical event (a glucose of 450 mg/dL is an emergency, not an outlier to be dropped). Every rejected record lands in quarantine with its rejection reason and the original raw payload, so it can be reviewed and reprocessed if appropriate.

**Versioning on all buckets:** S3 object versioning is enabled on all four buckets. This means if a file is overwritten, the previous version is retained. Combined with bronze immutability, this gives us a full audit trail — important for any healthcare-adjacent application.

---

## 4. AWS vs Snowflake: Choosing the Right Platform

This question deserves a direct answer because both are legitimate choices and the reasoning matters.

### The Case for Snowflake

Snowflake is a fully managed cloud data platform with a strong reputation in analytics engineering. Its advantages for a project like this would be:

- **SQL-first analytics:** Snowflake makes querying structured data trivially easy. You'd run `SELECT patient_id, AVG(glucose_mgdl) FROM readings GROUP BY patient_id` with essentially zero infrastructure setup.
- **Time Travel:** Snowflake's built-in time travel lets you query data as it existed at any point in the past — useful for audit trails.
- **Snowpark:** Python-native execution inside Snowflake, so you can write transformation logic in Python and run it close to the data.
- **Managed everything:** No cluster sizing, no storage management, auto-scaling compute.

### The Case for AWS

AWS wins for this project for five concrete reasons:

**1. The AI layer is non-negotiable — and it's AWS-native.** Amazon Bedrock (our chosen LLM engine) is an AWS service. Having the AI layer, the data storage, and the querying infrastructure all within the same AWS account means authentication is handled by IAM, data doesn't cross a network boundary to reach the model, and the latency for tool calls is minimised. With Snowflake, you'd need to either run a Bedrock call from outside Snowflake (extra network hop, credential management, latency) or use a Snowflake-native LLM (which would be a different AI provider, not Bedrock).

**2. Amazon Athena gives us SQL on S3 without moving data.** Athena is AWS's serverless query engine that runs SQL directly against S3 files using schema-on-read. This means we get the "run SQL on our data" capability of Snowflake without copying data into a separate system. With our medallion architecture already on S3, Athena is a natural fit — define a schema, point it at the silver bucket, and query. At the scale of this project, Athena costs pennies per query.

**3. This project is a portfolio of AWS skills.** A meaningful goal of this project is to demonstrate hands-on AWS knowledge: S3, IAM, Athena, Lambda (for future streaming), Glue (for future ETL automation), and Bedrock. Mixing in Snowflake would dilute that focus and introduce a second billing account, a second set of credentials, and a second mental model without proportional benefit at this scale.

**4. Cost at this scale favors AWS.** Snowflake has a minimum compute credit consumption and charges for both storage and compute separately. For a portfolio project processing a few thousand CGM readings, the AWS bill will be near zero — S3 storage at this volume is cents per month, Athena charges by data scanned (fractions of a cent for our dataset), and Bedrock charges per API call. Snowflake's minimum viable usage has a higher floor.

**5. The data doesn't benefit from Snowflake's columnar storage at this scale.** Snowflake's architectural advantage (micro-partitioned, columnar, auto-clustered storage) shines at petabyte scale with concurrent analytical queries. For tens of thousands of rows and a handful of concurrent users, it's irrelevant. We're not leaving performance on the table by staying on S3.

### The Verdict

**AWS is the right choice here.** Snowflake would be the right answer if: (a) we were building a BI-heavy platform that needed to serve dozens of analysts running ad-hoc SQL, or (b) the AI layer was agnostic to cloud provider, or (c) the project wasn't specifically about AWS skills. None of those are true here. We use AWS end-to-end, and we use Athena when we need SQL.

**An important note for the future:** If this project scaled to a production clinical environment with hundreds of analysts, a dedicated analytics engineering team, and dbt-managed transformation models, revisiting Snowflake would be warranted. The decision isn't "Snowflake is bad" — it's "AWS is the right fit for this specific project at this specific scope."

---

## 5. Why Conversational Analytics Instead of a Dashboard

The original scope included a Streamlit dashboard. We replaced it with an **AI-driven conversational analytics layer**, and this decision deserves explanation because it's one of the most architecturally interesting choices in the project.

**The problem with dashboards:** A dashboard answers the questions you thought to ask when you built it. A clinician looking at a pre-built panel of charts for patient 3 sees their time-in-range, maybe a 7-day glucose trend, and whatever KPIs were deemed important at design time. But the question the clinician actually has at 9 AM on a Tuesday might be: *"Patient 7 had a bad night — what does their glucose pattern look like in the 2 hours after their evening insulin dose compared to last week?"* That question can't be answered by a pre-built chart without additional work.

**What conversational analytics enables:** A clinician types a natural-language question. A Bedrock-powered AI agent interprets the intent, selects the right tools from the MCP layer (more on this below), queries the data, and returns a direct answer — with a generated chart or table if appropriate. The interface adapts to the question rather than forcing the question to fit the interface.

**Why this is more technically interesting:** A dashboard is a solved problem. Building one demonstrates competence with a charting library. An agentic analytics layer demonstrates understanding of: LLM tool-calling patterns, prompt engineering, data access design (what tools should the agent have?), latency management (tool calls are slow — how do you make it feel responsive?), and safety (what queries should the agent be allowed to run?). This is where modern data engineering and AI engineering intersect.

---

## 6. The MCP Layer — How the AI Accesses Data

**MCP (Model Context Protocol)** is an open standard for giving AI models structured access to external tools and data sources. Instead of the AI having direct database access (which would be a security nightmare), we define a set of **typed tool functions** — each one does exactly one thing, has defined inputs and outputs, and can be audited.

For this project, the MCP tools will cover things like:
- `get_patient_readings(patient_id, date_range)` — fetch CGM time-series for a patient
- `get_patient_summary(patient_id)` — return the clinical summary (demographics, comorbidities, lab values)
- `calculate_tir(patient_id, date_range)` — compute Time In Range (the key CGM quality metric)
- `flag_hypoglycemia_events(patient_id)` — identify readings below 70 mg/dL

**Why this matters for safety:** Every data access goes through a defined, auditable tool. The AI can't run arbitrary SQL against the database. It can only call tools that we explicitly designed and approved. This is the same principle as a least-privilege IAM policy — the agent has access to exactly what it needs, nothing more.

**Why this matters for the project:** Implementing an MCP layer is genuinely current practice in production AI systems. It demonstrates understanding of how to architect an AI system that interacts with data safely and predictably, which is a different (and more in-demand) skill than just "calling an API with a prompt."

---

## 7. The CanonicalReading Schema — Why a Unified Data Model Matters

Every data source in this project has a different format. The Shanghai data uses Excel files with a specific 11-column schema. simglucose produces simple CSVs. The Dexcom export mixes event types, uses string flags for out-of-range values, and has metadata rows.

The `CanonicalReading` is the unified schema that all three sources normalize to before entering the silver layer. Every reading in silver has:

- **`patient_id`** — who the reading belongs to
- **`event_time`** — UTC timestamp (all times converted to UTC on ingest)
- **`glucose_mgdl`** — numeric glucose value, or `null` if out of range/missing
- **`glucose_flag`** — controlled vocabulary: `in_range`, `low`, `high`, `invalid`, `out_of_range`
- **`source_system`** — which of the three sources this came from
- **`ingest_time`** — when our pipeline processed it (for latency auditing)
- **`lineage_id`** — a unique UUID generated at ingest, so every reading can be traced back
- **`raw_payload`** — the original row as received, before any transformation

**Why store the raw payload?** Because transformation logic changes. If we later decide our out-of-range threshold (40–400 mg/dL) was wrong, we need to be able to re-derive the correct value from the original number without going back to bronze. The raw payload in the silver record is a middle-ground between full re-ingestion and trusting transformed values.

**Why UTC?** The Shanghai data is collected in China (UTC+8). simglucose generates timestamps in simulation time. Dexcom exports can carry the user's local timezone. If we don't normalize to UTC at ingest, every downstream query has to reason about timezones — and midnight hypoglycemia events will appear in the wrong day depending on where you're looking from. UTC at the border, local time at the presentation layer.

---

## 8. Security Design — Least Privilege from Day One

**IAM policy scope:** The pipeline's IAM role has `s3:PutObject`, `s3:GetObject`, and `s3:ListBucket` permissions — and only on the four project buckets. It cannot create or delete buckets, cannot access other S3 resources in the account, and cannot call any non-S3 services beyond Bedrock and Athena. This is the principle of least privilege applied from the first line of infrastructure code.

**No credentials in code:** All sensitive values (bucket names, AWS region, account IDs) live in environment variables loaded from a `.env` file that is explicitly excluded from version control. The `.env.example` file shows the structure without any real values.

**Public access blocked:** All four S3 buckets have `BlockPublicAccess` enabled at the bucket level. CGM data — even the research dataset used here — carries privacy expectations that we treat seriously.

**Versioning as an accident buffer:** S3 versioning on all buckets means an accidental overwrite or deletion can be recovered. In a real clinical context this is non-negotiable; in a portfolio project it demonstrates awareness of data integrity best practices.

---

## 9. What This Portfolio Demonstrates

At a glance, this project covers:

| Skill Area | Demonstrated By |
|---|---|
| **Data modeling** | CanonicalReading schema, medallion zones, lineage design |
| **Multi-source ingestion** | Three sources with fundamentally different formats, unified via schema normalization |
| **Cloud infrastructure** | S3 (4 buckets, versioning, lifecycle), IAM (least-privilege policy), Athena (SQL on S3) |
| **Data quality engineering** | Validation rules, quarantine zone, flag vocabulary, raw payload preservation |
| **AI/ML integration** | Amazon Bedrock, agentic query pattern, MCP tool layer |
| **Healthcare domain knowledge** | CGM physiology, T1DM vs T2DM distinction, Time In Range metric, clinical complication categories |
| **Real-world messiness handling** | Dexcom format quirks, string Low/High flags, duplicate rows, metadata preambles |
| **Security posture** | Least-privilege IAM, no hardcoded credentials, public access blocked, versioning |

The project is intentionally a **starter project** — it is not designed to be a production clinical system. It is designed to demonstrate that the builder understands the full landscape: the data, the infrastructure, the engineering patterns, the AI integration, and the domain context. Every decision documented here is a decision that matters at scale, even if at this stage we're working with a few dozen patients rather than thousands.

---

*Last updated: October 2026 | Author: Project architect*
