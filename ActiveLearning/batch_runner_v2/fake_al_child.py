"""Deterministic offline child. It never imports the AL runtime or an SDK."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", required=True)
    parser.add_argument("--attempt-dir", required=True)
    parser.add_argument("--scenario", choices=("success", "invalid", "terminal", "nonzero",
                                               "crash", "partial", "corrupt", "sleep"), required=True)
    args = parser.parse_args()
    identity = json.loads(args.identity)
    attempt = Path(args.attempt_dir).resolve(strict=True)
    if args.scenario == "sleep":
        time.sleep(60)
        return 0
    if args.scenario == "crash":
        os._exit(7)
    run_dir = attempt / "al_run" / identity["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "al_summary.json"
    if args.scenario in {"partial", "corrupt"}:
        result_path.write_text("{", encoding="utf-8")
        return 0
    outcome = {**identity, "al_completed": args.scenario in {"success", "invalid"},
               "simulation_valid": args.scenario == "success",
               "meets_spec": False, "iterations_completed": 2,
               "stop_reason": "max_iterations" if args.scenario in {"success", "invalid"} else None,
               "error_code": "SYNTHETIC_CIRCUIT_FAILURE" if args.scenario == "terminal" else None}
    result_path.write_text(json.dumps(outcome, sort_keys=True), encoding="utf-8")
    return 2 if args.scenario in {"terminal", "nonzero"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
