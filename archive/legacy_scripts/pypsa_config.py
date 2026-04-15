"""Configuration for step-10+ workflow (solve + congestion post-processing)."""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Inputs from completed PyPSA-Eur setup (steps 1-9) ---
# Use solved network if available; otherwise point INPUT_NETWORK_PATH to the
# pre-solve network and run pypsa_market_dispatch.py first.
INPUT_NETWORK_PATH = BASE_DIR / "data" / "networks" / "base_s_256_elec_.nc"

# --- Output locations for this repository ---
OUTPUT_DIR = BASE_DIR / "outputs"
RESULTS_DIR = OUTPUT_DIR / "step10_12"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

SOLVED_NETWORK_PATH = RESULTS_DIR / "network_2025_solved.nc"
LINE_LOADING_PATH = RESULTS_DIR / "line_loading_hourly_2025.csv"

VALIDATION_SUMMARY_PATH = RESULTS_DIR / "model_validation_summary.csv"
CONGESTION_BY_LINE_PATH = RESULTS_DIR / "congestion_by_line_2025.csv"
CONGESTION_HOURLY_PATH = RESULTS_DIR / "congestion_hourly_flags_2025.csv"
KUPFERZELL_LINE_HOURLY_PATH = RESULTS_DIR / "kupferzell_line_proximity_hourly_2025.csv"

CONGESTION_FIGURE_PATH = RESULTS_DIR / "figure_congestion_occurrence_per_line_2025.png"
KUPFERZELL_FIGURE_PATH = RESULTS_DIR / "figure_kupferzell_line_loading_2025.png"

# --- Study assumptions (from SETUP_GUIDE methodology) ---
SIM_YEAR = 2025
CONGESTION_THRESHOLD = 0.98
KUPFERZELL_LAT = 49.2333
KUPFERZELL_LON = 9.6833
KUPFERZELL_RADIUS_DEG = 0.8

# Solver for step-10 solve
SOLVER_NAME = "highs"
SOLVER_OPTIONS = {
    "presolve": "on",
    "parallel": "on",
}
