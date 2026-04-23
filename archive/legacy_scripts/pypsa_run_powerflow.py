"""Compatibility wrapper.

This project now uses pypsa_market_dispatch.py as the single solve step.
Run this file only if older scripts still call pypsa_run_powerflow.py.
"""

from pypsa_market_dispatch import main


if __name__ == "__main__":
    main()
