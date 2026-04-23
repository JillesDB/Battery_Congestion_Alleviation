"""Step 10: Solve the full-year electricity model and export line loading."""

from __future__ import annotations

import pypsa
import pandas as pd

from pypsa_config import (
    INPUT_NETWORK_PATH,
    SOLVED_NETWORK_PATH,
    LINE_LOADING_PATH,
    SOLVER_NAME,
    SOLVER_OPTIONS,
)


def main() -> None:
    print(f"Loading network: {INPUT_NETWORK_PATH}")
    n = pypsa.Network(INPUT_NETWORK_PATH)

    print(f"Snapshots: {len(n.snapshots)}")
    status, termination = n.optimize(
        solver_name=SOLVER_NAME,
        solver_options=SOLVER_OPTIONS,
    )
    print(f"Solve status: {status} | termination: {termination}")

    n.export_to_netcdf(SOLVED_NETWORK_PATH)
    print(f"Saved solved network: {SOLVED_NETWORK_PATH}")

    loading = n.lines_t.p0.abs().div(n.lines.s_nom, axis=1)
    loading.to_csv(LINE_LOADING_PATH)
    print(f"Saved hourly line loading: {LINE_LOADING_PATH}")


if __name__ == "__main__":
    main()
