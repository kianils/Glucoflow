.PHONY: setup test simulate ingest-shanghai ingest-sim ingest-dexcom provision

# Set up virtual environment and install dependencies
setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	@echo "\n✅ Setup complete. Activate with: source .venv/bin/activate"

# Run all tests
test:
	pytest tests/ -v

# Generate synthetic CGM data with simglucose
simulate:
	python src/generators/run_simulation.py

# Generate Dexcom-style messy CSV from simglucose output
make-dexcom:
	python src/generators/make_dexcom_csv.py

# Run all generators
generate: simulate make-dexcom

# Ingest Shanghai .xlsx files to Bronze S3
ingest-shanghai:
	python src/ingestion/shanghai.py

# Ingest simglucose CSV to Bronze S3
ingest-sim:
	python src/ingestion/simglucose.py

# Ingest Dexcom-style CSV to Bronze S3
ingest-dexcom:
	python src/ingestion/dexcom.py

# Ingest all sources
ingest-all: ingest-shanghai ingest-sim ingest-dexcom

# Provision S3 buckets (run once)
provision:
	python infra/provision_buckets.py
