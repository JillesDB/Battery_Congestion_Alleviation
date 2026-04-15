"""Minimal step-by-step runner for the rebuilt analysis workflow."""

from __future__ import annotations

import argparse
import subprocess
import sys

STEPS = {
    10: "pypsa_market_dispatch.py",
    11: "pypsa_count_congestion_occurrence.py",
}


def run_step(step: int) -> None:
    script = STEPS[step]
    print(f"\n=== Running step {step}: {script} ===")
    subprocess.run([sys.executable, script], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run rebuilt research workflow steps 10-11")
    parser.add_argument("--from-step", type=int, default=10, choices=sorted(STEPS))
    parser.add_argument("--only-step", type=int, choices=sorted(STEPS))
    args = parser.parse_args()

    if args.only_step is not None:
        run_step(args.only_step)
        return

    for step in sorted(STEPS):
        if step >= args.from_step:
            run_step(step)


if __name__ == "__main__":
    main()
