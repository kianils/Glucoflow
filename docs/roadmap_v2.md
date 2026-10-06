# GlucoFlow — Project Roadmap v2

*Updated: October 5, 2026 | Replaces the Day-by-Day outline in the original execution pack*

---

## Today's Priority List (October 5 — Day 1 Execution)

These are the concrete tasks to complete today, in order. Do not move to the next item until the current one is done.

| # | Task | Done-when |
|---|------|-----------|
| 1 | **Project scaffold** — Run Prompt 1 in Cursor/Claude Code to generate the full `glucoflow/` directory structure | `ls glucoflow/src` shows all four subdirectories; `pip install -r requirements.txt` exits clean |
| 2 | **Shanghai data** — Confirm you have `Shanghai_T1DM_Summary.xlsx`, `Shanghai_T2DM_Summary.xlsx`, and the per-patient folders. Verify the license on the Figshare record. Move files to `glucoflow/data/raw/shanghai/` | Files visible at that path; you've read the license and it permits presentation use |
| 3 | **Canonical schema** — Run Prompt 2 to build `CanonicalReading` with pydantic v2 and the five `glucose_flag` values | `pytest tests/test_schema.py` passes all 5 test cases — zero failures |
| 4 | **Synthetic data** — Run Prompt 3 to generate simglucose data for 5 patients × 14 days | 5 CSV files in `data/samples/synthetic/`; in-range % printed to console looks like 65–90% |
| 5 | **Messy Dexcom data** — Run Prompt 4 to generate messy exports from the synthetic CSVs | Files in `data/samples/messy/`; open one and visually confirm metadata rows at top, `Low`/`High` strings, mixed event types |
| 6 | **AWS infrastructure** — Set unique bucket names in `.env`, run `aws configure`, then run Prompt 5 | `aws s3 ls` shows all 4 buckets; versioning confirmed via console or CLI |
| 7 | **Bronze upload** — Run Prompt 6 to upload all three source types to S3 | Script prints `PASS` for all 3 prefixes: `shanghai/`, `simglucose/`, `dexcom_messy/` |
| 8 | *(Stretch)* **Commit to GitHub** — Push the scaffold, schema, generators, infra scripts, and sample data to a private repo | Repo exists; README is accurate; `.env` is not committed |

**Day 1 success = you can `aws s3 ls s3://<BRONZE_BUCKET>/` and see all three source prefixes.**

---

## Revised Full Schedule

### Week 1 — Foundation

#### Day 1 — Landing Zone ✦ (Today)
*Goal: Raw data on S3, three sources, infrastructure ready.*

- Project scaffold (structure, dependencies, Makefile)
- CanonicalReading schema with validation + tests
- Shanghai data verified and staged
- simglucose synthetic data generated (5 patients × 14 days)
- Dexcom-style messy CSV export generated
- 4 S3 buckets with versioning + public access blocking
- Bronze upload with per-source prefix + object metadata

---

#### Day 2 — Normalization & Quality
*Goal: All raw data flows through the schema, validated records in silver, rejects in quarantine.*

- **Shanghai ingestion module** (`src/ingestion/shanghai_ingestor.py`):
  - Parse both T1DM and T2DM summary sheets
  - Load per-patient CGM files, join on patient ID
  - Handle the `/` null strings in lab value columns
  - Handle multiple admissions per patient (patient 1002 has 3 visits)
  - Apply CanonicalReading validation; write silver or quarantine
- **simglucose ingestion module** — clean CSVs, minimal transformation needed
- **Dexcom ingestion module** — strip preamble rows, filter to EGV events, parse Low/High strings
- Write all three source types to `s3://<SILVER_BUCKET>/` partitioned by `source_system/year/month/`
- Quarantine rejects land in `s3://<QUARANTINE_BUCKET>/` with rejection reason and raw payload
- Quality report: row counts per source, % quarantine rate, distribution of `glucose_flag` values
- **Decision point:** Confirm Athena schema matches silver output before proceeding

---

#### Day 3 — Query Layer
*Goal: SQL-queryable data lake via Athena; first aggregate metrics computable.*

- AWS Glue Data Catalog: register silver + gold as Athena tables
- Define partitions: `source_system`, `year`, `month` (for cost control on scans)
- Verify: `SELECT COUNT(*) FROM silver_readings WHERE source_system = 'shanghai'` returns correct count
- Build first gold-layer aggregation: **daily Time In Range per patient**
  - TIR definition: % of readings between 70–180 mg/dL
  - Output to `s3://<GOLD_BUCKET>/daily_tir/`
- Build second gold aggregation: **hypoglycemia event log** (readings < 70 mg/dL with patient, timestamp, prior value)
- Run baseline analysis: mean TIR by diabetes type (T1DM vs T2DM), using Shanghai patients
- **Verify:** Query the gold table directly from the Athena console

---

#### Day 4 — MCP Tool Layer
*Goal: Typed, auditable tool functions that the AI can call to access data.*

- Design the MCP tool schema — what tools does a clinician need?
  - `get_patient_readings(patient_id, start_date, end_date)` → time-series list
  - `get_patient_summary(patient_id)` → demographics + clinical profile (from Shanghai summary)
  - `calculate_tir(patient_id, start_date, end_date)` → numeric TIR percentage
  - `calculate_tir_by_period(patient_id, period="daytime"/"overnight")` → period-specific TIR
  - `flag_hypoglycemia_events(patient_id, threshold=70)` → list of hypoglycemia events
  - `compare_patients(patient_id_list, metric)` → cross-patient comparison
  - `get_glucose_distribution(patient_id)` → bucket histogram of readings
- Implement each tool as a Python function, calling Athena via `boto3`
- Add **output schema validation** — every tool returns a typed response, not a raw dict
- Add **audit logging** — every tool call is logged with: tool name, parameters, caller, timestamp, row count returned
- Write unit tests: mock the Athena responses, verify tool output shape and edge cases (patient not found, empty date range)
- **Decision point:** Review tool set with fresh eyes — is there an obvious missing tool? Add it now before it becomes expensive to retrofit.

---

#### Day 5 — Bedrock AI Integration
*Goal: Amazon Bedrock agent with access to MCP tools; first real natural-language queries working.*

- Create Amazon Bedrock agent in the AWS console (or via CLI)
- Attach the MCP tools as Bedrock Action Groups
- Write a **system prompt** that tells the agent:
  - What it is (a clinical data assistant)
  - What data it has access to (patient IDs, date ranges)
  - What it should NOT do (no diagnosis, no treatment recommendations)
  - How to handle data not found (acknowledge gaps honestly)
- Test natural-language queries against the Shanghai patients:
  - *"How many days of CGM data does patient 1002 have across all admissions?"*
  - *"What was patient 2000's time in range during their December 2020 admission?"*
  - *"Which patient in the T2DM cohort had the most hypoglycemia events?"*
- Debug tool call failures — Bedrock's reasoning trace is your primary debugging tool
- Document the prompt engineering choices: what worked, what didn't, and why

---

#### Day 6 — Conversational Interface
*Goal: A working end-to-end experience where a user can ask questions and get data-backed answers.*

- Build a thin Python interface (no UI required — a command-line REPL is fine at this stage)
  - Input: natural language question
  - Process: call Bedrock agent, stream the response
  - Output: text answer + any structured data (table or chart if generated)
- Handle multi-turn context: *"What about their TIR last week?" → agent knows "their" = previous patient*
- Implement **graceful degradation**: if Athena returns an error, the agent acknowledges it rather than hallucinating a number
- Run through 10 representative queries and record: tool call chain, latency, answer quality
- **Portfolio artifact:** Record a short screen capture of the conversational interface in action

---

### Week 2 — Enhancement & Polish

#### Day 7 — Streaming Simulation
*Goal: Add a near-real-time data path alongside the batch path.*

- Build a simple AWS Lambda function that accepts a CGM reading POST and writes it to a `streaming/` prefix in bronze
- Use an S3 event notification or EventBridge to trigger downstream processing
- Simulate a "live patient" by replaying historical readings from patient 1002 at 15-minute intervals
- Verify: new readings appear in silver and the AI can query them

#### Day 8 — Data Quality Monitoring
*Goal: Automated alerts when data quality drops.*

- Add a data quality check step that runs after each ingestion run
- Checks: missing rate > 10%, quarantine rate > 5%, gap in time-series > 4 hours
- Log results to a DynamoDB table (or a dedicated S3 prefix)
- If any check fails, send an SNS notification (email alert)
- This mirrors what a real-world data SLA looks like

#### Day 9 — Documentation & Presentation Polish
*Goal: Project is presentable to a technical reviewer.*

- README walkthrough: clear setup instructions, architecture diagram, example queries
- Architecture diagram (draw.io or Excalidraw): source systems → bronze → silver → gold → Athena → MCP → Bedrock
- Design choices document (already done — review and refine)
- Recorded demo video: 3-5 minutes showing the full flow from raw data to AI query answer
- Ensure all code has docstrings and the key modules have inline comments explaining *why*, not just *what*

#### Day 10 — Review & Future Roadmap
*Goal: Honest assessment of what was built and what would come next.*

- Code review pass: identify anything that would not survive a professional review
- Cost audit: actual AWS costs incurred during the project (should be < $5 for demo scale)
- Retrospective: what took longer than expected, what was faster, what would I change?
- Future roadmap section in README: what would production-scale version look like?
  - Real patient data integration (EHR standards: HL7 FHIR)
  - Multi-tenant architecture (patient isolation)
  - HIPAA compliance considerations
  - Automated model retraining as new data arrives
  - Snowflake as the analytics layer if team scales to > 10 analysts

---

## Key Decision Log

| Decision | Alternative Considered | Rationale |
|---|---|---|
| AWS-native (Bedrock, S3, Athena) | Snowflake | Bedrock integration, portfolio focus, cost at this scale |
| Conversational interface | Streamlit dashboard | More technically interesting, future-proof pattern |
| Medallion architecture | Single-zone lake | Reproducibility, audit trail, reprocessing capability |
| MCP tool layer | Direct Athena access from AI | Safety, auditability, least-privilege for AI |
| pydantic v2 schemas | ad-hoc pandas transforms | Type safety, validation logic centralised, testable |
| UTC normalization at ingest | Preserve source timezone | Prevents cross-timezone query errors downstream |

---

## What to Prioritize if Time is Short

If you have fewer sessions available than the 10-day schedule, protect these in order:

1. **Days 1–2** — Non-negotiable. A pipeline with no data or no quality layer isn't a pipeline.
2. **Day 3 (Athena setup)** — Getting SQL working against S3 is the foundation for everything else.
3. **Day 4 (MCP tools)** — Designing the tool layer is the most architecturally significant day.
4. **Day 5 (Bedrock)** — One working AI query demonstrating the full chain is worth more than 5 polished charts.
5. **Days 6–10** — Polish, streaming, and monitoring are valuable but the core story is told by Day 5.

---

*This roadmap is a living document. Update it as decisions change and as days are completed.*
