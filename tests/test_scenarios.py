#!/usr/bin/env python3
"""
Simulate grid power scenarios by pushing values to the mock HA sensors.
HA connection settings are read from config.yaml.

Usage:
    python test_scenarios.py                  # run all scenarios in sequence
    python test_scenarios.py --set-grid 500   # set grid power to 500 W and exit
    python test_scenarios.py --set-soc1 80 --set-soc2 30

While this runs, watch the controller react:
    HA → Settings → Add-ons → AppDaemon → Log
"""

import argparse
import time
from pathlib import Path

import requests
import yaml


def load_ha_config() -> tuple[str, str]:
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    ha = cfg["homeassistant"]
    return ha["url"].rstrip("/"), ha["token"]


HA_URL, HA_TOKEN = load_ha_config()
HEADERS = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def set_value(entity_id: str, value: float, unit: str = "W") -> None:
    """Set an input_number helper via the HA service call API."""
    resp = requests.post(
        f"{HA_URL}/api/services/input_number/set_value",
        headers=HEADERS,
        json={"entity_id": entity_id, "value": value},
    )
    resp.raise_for_status()
    print(f"  SET {entity_id} = {value} {unit}")


def run_scenario(name: str, grid_w: float, soc1: float, soc2: float,
                 wait: int = 15) -> None:
    print(f"\n{'='*60}")
    print(f"Scenario: {name}")
    print(f"  Grid: {grid_w:+.0f} W | SoC1: {soc1:.0f}% | SoC2: {soc2:.0f}%")

    if abs(grid_w) < 50:
        print("  Expected: both batteries STOP (within deadband)")
    elif grid_w > 0:
        print(f"  Expected: DISCHARGE ~{grid_w:.0f} W total (split by SoC ratio)")
    else:
        print(f"  Expected: CHARGE ~{abs(grid_w):.0f} W total (split by headroom ratio)")

    set_value("input_number.mock_grid_power", grid_w)
    set_value("input_number.mock_soc_1", soc1, "%")
    set_value("input_number.mock_soc_2", soc2, "%")

    for remaining in range(wait, 0, -5):
        print(f"  [{remaining}s] watching logs...")
        time.sleep(min(5, remaining))

    print("  → Check AppDaemon logs for controller output.")


SCENARIOS = [
    # (name,                             grid_w,  soc1, soc2)
    ("Solar excess — charge both",        -800,    50,   50),
    ("Unbalanced SoC — more to bat2",     -600,    80,   30),
    ("Grid import — discharge both",       700,    60,   60),
    ("Unbalanced SoC — more from bat1",    500,    80,   30),
    ("Within deadband — stop",              30,    50,   50),
    ("Battery 1 at max SoC — only bat2",  -400,    95,   50),
    ("Battery 2 at min SoC — only bat1",   600,    60,   15),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Test battery controller scenarios")
    parser.add_argument("--set-grid", type=float, metavar="W",
                        help="Set mock grid power (W) and exit")
    parser.add_argument("--set-soc1", type=float, metavar="PCT",
                        help="Set battery 1 SoC (%%)")
    parser.add_argument("--set-soc2", type=float, metavar="PCT",
                        help="Set battery 2 SoC (%%)")
    args = parser.parse_args()

    if any(v is not None for v in (args.set_grid, args.set_soc1, args.set_soc2)):
        if args.set_grid is not None:
            set_value("input_number.mock_grid_power", args.set_grid)
        if args.set_soc1 is not None:
            set_value("input_number.mock_soc_1", args.set_soc1, "%")
        if args.set_soc2 is not None:
            set_value("input_number.mock_soc_2", args.set_soc2, "%")
        print("\nDone. Watch AppDaemon logs in HA.")
        return

    print("Marstek Battery Controller — Test Scenarios")
    print("Watch logs: HA → Settings → Add-ons → AppDaemon → Log\n")

    for name, grid_w, soc1, soc2 in SCENARIOS:
        run_scenario(name, grid_w, soc1, soc2, wait=15)

    set_value("input_number.mock_grid_power", 0)
    print("\nAll scenarios complete — grid reset to 0 W.")


if __name__ == "__main__":
    main()
