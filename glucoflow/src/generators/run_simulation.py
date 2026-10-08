"""
run_simulation.py
=================
Runs the UVA/Padova T1D simulator (simglucose) for a configurable set of
virtual patients and writes one CSV per patient to data/samples/synthetic/.

Output CSV columns (per patient file):
    Time        — simulation time in minutes since midnight of start_date
    BG          — true blood glucose (mg/dL), not observable in real life
    CGM         — what the CGM sensor reports (mg/dL) — this is the "device reading"
    CHO         — carbohydrate intake at this timestep (grams)
    insulin     — insulin delivered (units)
    LBGI        — Low Blood Glucose Index (risk metric)
    HBGI        — High Blood Glucose Index (risk metric)
    Risk        — composite BG risk score

Why these columns matter for the pipeline:
    - BG is the ground truth; CGM has sensor noise on top of it — we ONLY use CGM downstream
    - CHO and insulin are logged alongside glucose so we can later correlate meals/dosing with spikes
    - LBGI/HBGI are pre-computed clinical risk scores; including them in the sample data lets us
      test that our Bronze → Silver parser preserves them without recalculating

Usage:
    python src/generators/run_simulation.py
    # or via Makefile:
    make simulate

Outputs:
    data/samples/synthetic/<patient_name>.csv   (one file per patient)

Requirements:
    simglucose, pandas, numpy (all in requirements.txt)
"""

import logging
import os
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

# ── simglucose is in our deps folder when running via Makefile ──────────────
_DEPS = Path(__file__).resolve().parents[3] / ".." / ".." / ".." / "glucoflow-deps"
# graceful fallback — if the library is already on sys.path (e.g. venv), skip
if str(_DEPS) not in sys.path:
    sys.path.insert(0, str(_DEPS))

warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np

from simglucose.patient.t1dpatient import T1DPatient
from simglucose.sensor.cgm import CGMSensor
from simglucose.actuator.pump import InsulinPump
from simglucose.controller.basal_bolus_ctrller import BBController
from simglucose.simulation.env import T1DSimEnv
from simglucose.simulation.scenario_gen import RandomScenario
from simglucose.simulation.sim_engine import SimObj

import pkg_resources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────────
# All 30 virtual patients from the UVA/Padova simulator:
# 10 adolescents, 10 adults, 10 children.
PATIENTS_TO_SIMULATE = (
    [f"adolescent#{i:03d}" for i in range(1, 11)] +
    [f"adult#{i:03d}"      for i in range(1, 11)] +
    [f"child#{i:03d}"      for i in range(1, 11)]
)

# Simulation length: 24 hours = 1440 minutes (288 CGM readings at 5-min intervals)
SIM_HOURS = 24
SIM_MINUTES = SIM_HOURS * 60

# A fixed calendar date to anchor the simulation timestamps.
# Using a date well inside the Shanghai dataset window for realism.
SIM_START_DATE = datetime(2021, 7, 30, 0, 0, 0)  # midnight

# Random seed — fix for reproducibility; change to None for stochastic runs
SEED = 42

# Output directory (relative to glucoflow/ root)
OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "samples" / "synthetic"


def load_patient(name: str) -> T1DPatient:
    """Load a T1DPatient by display name (e.g. 'adolescent#001')."""
    para_file = pkg_resources.resource_filename("simglucose", "params/vpatient_params.csv")
    params_df = pd.read_csv(para_file)
    row = params_df[params_df["Name"] == name]
    if row.empty:
        available = params_df["Name"].tolist()
        raise ValueError(f"Patient '{name}' not found. Available: {available}")
    return T1DPatient(params=row.squeeze(), random_init_bg=False, seed=SEED)


def load_sensor(name: str = "Dexcom", seed: int = SEED) -> CGMSensor:
    """Load a CGMSensor by model name. Options: 'Dexcom', 'GuardianRT', 'Navigator'."""
    sensor_file = pkg_resources.resource_filename("simglucose", "params/sensor_params.csv")
    sensor_params = pd.read_csv(sensor_file)
    row = sensor_params[sensor_params["Name"] == name]
    if row.empty:
        raise ValueError(f"Sensor '{name}' not in params.")
    return CGMSensor(params=row.squeeze(), seed=seed)


def load_pump(name: str = "Insulet") -> InsulinPump:
    """Load an InsulinPump by model name. Options: 'Cozmo', 'Insulet'."""
    pump_file = pkg_resources.resource_filename("simglucose", "params/pump_params.csv")
    pump_params = pd.read_csv(pump_file)
    row = pump_params[pump_params["Name"] == name]
    if row.empty:
        raise ValueError(f"Pump '{name}' not in params.")
    return InsulinPump(params=row.squeeze())


def simulate_patient(patient_name: str, out_dir: Path) -> Path:
    """
    Run a 24-hour simulation for one patient and save results as CSV.

    Returns:
        Path to the written CSV file.
    """
    logger.info(f"Simulating patient: {patient_name}")

    patient  = load_patient(patient_name)
    sensor   = load_sensor("Dexcom", seed=SEED)
    pump     = load_pump("Insulet")
    scenario = RandomScenario(start_time=SIM_START_DATE, seed=SEED)

    env        = T1DSimEnv(patient=patient, sensor=sensor, pump=pump, scenario=scenario)
    controller = BBController(target=140)
    sim        = SimObj(env=env, controller=controller, sim_time=timedelta(hours=SIM_HOURS), animate=False)

    sim.simulate()
    df = sim.results()

    # ── Post-process: attach a real datetime index ───────────────────────────
    # simglucose returns a DatetimeIndex (pandas Timestamps) already anchored
    # to the start_time we passed in — just reset and rename.
    df = df.reset_index()          # Time becomes a regular column of Timestamps
    df["timestamp"] = pd.to_datetime(df["Time"], utc=True)
    df["patient_id"] = patient_name
    df = df.rename(columns={"CGM": "cgm_mgdl", "BG": "bg_mgdl", "CHO": "cho_grams", "insulin": "insulin_units"})
    df = df[["patient_id", "timestamp", "cgm_mgdl", "bg_mgdl", "cho_grams", "insulin_units", "LBGI", "HBGI", "Risk"]]
    df = df.round(4)

    out_dir.mkdir(parents=True, exist_ok=True)
    # Sanitize patient_name for filename: adolescent#001 → adolescent_001
    safe_name = patient_name.replace("#", "_")
    out_path = out_dir / f"{safe_name}.csv"
    df.to_csv(out_path, index=False)

    n_readings = len(df)
    cgm_mean   = df["cgm_mgdl"].mean()
    cgm_min    = df["cgm_mgdl"].min()
    cgm_max    = df["cgm_mgdl"].max()
    logger.info(
        f"  → {out_path.name}  |  {n_readings} rows  |  "
        f"CGM mean={cgm_mean:.1f}  min={cgm_min:.1f}  max={cgm_max:.1f} mg/dL"
    )
    return out_path


def main():
    logger.info(f"Starting simulation for {len(PATIENTS_TO_SIMULATE)} patients → {OUT_DIR}")
    written = []
    for name in PATIENTS_TO_SIMULATE:
        try:
            path = simulate_patient(name, OUT_DIR)
            written.append(path)
        except Exception as exc:
            logger.error(f"Failed to simulate {name}: {exc}")
    logger.info(f"Done. {len(written)}/{len(PATIENTS_TO_SIMULATE)} files written.")
    for p in written:
        logger.info(f"  {p}")


if __name__ == "__main__":
    main()
