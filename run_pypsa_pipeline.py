"""
Kupferzell GridBooster — Transmission Congestion Simulation
===========================================================
Master pipeline runner. Runs all four steps sequentially.

Usage:
    python run_pypsa_pipeline.py [--test] [--step N]

Options:
    --test      Run on 4-week test period only (faster development cycle)
    --step N    Start from step N (1–4), skipping earlier steps
                (requires prior outputs from skipped steps)

Environment setup (one-time):
    pip install pypsa pandas numpy matplotlib requests scipy highspy

Steps:
    1. Download SMARD 2024 hourly data (load + generation by technology)
    2. Build PyPSA-EUR network (128-bus, 2024 calibration)
    3. Run LOPF monthly batches (market dispatch + DC power flow)
    4. Congestion analysis and output generation
"""

import sys
import os
import argparse
import subprocess
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STEPS = {
    1: "data_validation_pypsa.py",
    2: "pypsa_build_network.py",
    3: "pypsa_run_powerflow.py",
    4: "pypsa_count_congestion_occurrence.py",
}

STEP_NAMES = {
    1: "Download SMARD data",
    2: "Build PyPSA-EUR network",
    3: "Run LOPF (market dispatch)",
    4: "Congestion analysis",
}

# Approximate runtimes (minutes) on a modern workstation (16 cores, 32 GB RAM)
STEP_RUNTIMES = {
    1: "~15–30 min (API rate-limited)",
    2: "~5–10 min",
    3: "~4–8 hours (full year, HiGHS solver)",
    4: "~5–10 min",
}


def check_environment():
    """Verify required packages are installed."""
    required = ["pypsa", "pandas", "numpy", "matplotlib", "requests", "scipy"]
    missing = []
    for pkg in required:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[!] Missing packages: {missing}")
        print(f"    Install with: pip install {' '.join(missing)} highspy")
        sys.exit(1)

    # Check HiGHS
    try:
        import highspy
        print("  ✓ HiGHS solver available")
    except ImportError:
        print("  [!] highspy not found. Install with: pip install highspy")
        print("      Alternatively, set SOLVER='glpk' in 00_config.py (slower)")

    print("  ✓ Environment check passed")


def run_step(step_num: int, test_mode: bool = False) -> bool:
    """Run a pipeline step as a subprocess."""
    script = os.path.join(BASE_DIR, STEPS[step_num])
    env = os.environ.copy()
    if test_mode:
        env["KUPFERZELL_TEST_MODE"] = "1"

    print(f"\n{'═'*60}")
    print(f"  STEP {step_num}: {STEP_NAMES[step_num]}")
    print(f"  Expected runtime: {STEP_RUNTIMES[step_num]}")
    print(f"{'═'*60}\n")

    t0 = time.time()
    result = subprocess.run(
        [sys.executable, script],
        env=env,
        capture_output=False   # stream output live
    )
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(f"\n[!] Step {step_num} FAILED (exit code {result.returncode})")
        print(f"    Elapsed: {elapsed/60:.1f} minutes")
        return False

    print(f"\n✓ Step {step_num} completed in {elapsed/60:.1f} minutes")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Kupferzell GridBooster congestion simulation pipeline"
    )
    parser.add_argument("--test", action="store_true",
                        help="Run in test mode (4-week period)")
    parser.add_argument("--step", type=int, default=1,
                        help="Start from step N (1–4)")
    parser.add_argument("--only", type=int, default=None,
                        help="Run only step N")
    args = parser.parse_args()

    print("Kupferzell GridBooster — Congestion Simulation Pipeline")
    print("=" * 60)

    print("\nChecking environment ...")
    check_environment()

    if args.test:
        print("\n[TEST MODE] Simulation restricted to 4-week period")
        # Patch config
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "config_00", os.path.join(BASE_DIR, "00_config.py")
        )
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        cfg.FULL_YEAR = False

    steps_to_run = range(args.step, 5) if args.only is None else [args.only]

    for step_num in steps_to_run:
        if step_num not in STEPS:
            print(f"[!] Invalid step: {step_num}")
            continue
        success = run_step(step_num, test_mode=args.test)
        if not success:
            print(f"\n[!] Pipeline aborted at step {step_num}")
            sys.exit(1)

    print(f"\n{'═'*60}")
    print("  PIPELINE COMPLETE")
    print(f"  Results in: {os.path.join(BASE_DIR, 'outputs/')}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()