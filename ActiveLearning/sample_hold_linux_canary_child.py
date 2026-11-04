"""Private Linux native worker. Never imported by closure capture or unit tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path


def _source(values: list[list[float]]) -> str:
    return "PWL(" + " ".join(f"{t:.17g} {v:.17g}" for t, v in values) + ")"


def _clock(pulse: dict) -> str:
    return "PULSE(" + " ".join(f"{pulse[key]:.17g}" for key in
                                 ("low_v", "high_v", "delay_s", "rise_s", "fall_s",
                                  "high_plateau_s", "period_s")) + ")"


def _vector(analysis, name: str) -> list[float]:
    for key, vector in analysis.nodes.items():
        if str(key).casefold() == name.casefold():
            return [float(item) for item in vector]
    raise RuntimeError(f"native waveform unavailable: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--closure", required=True, type=Path)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)

    from ActiveLearning.sample_hold_linux_canary import (
        EXPERIMENT_ID, ROOT, _frozen_fixture, _sha, _write_new, verify_closure,
    )

    closure = verify_closure(args.closure, require_frozen=True)
    plan = _frozen_fixture()
    experiment = args.experiment.resolve(strict=True)
    if experiment.name != EXPERIMENT_ID or not (experiment.parent / (EXPERIMENT_ID + ".lock")).is_dir():
        raise RuntimeError("unguarded Linux experiment identity")
    identity = json.loads((experiment / "approval_identity.json").read_text(encoding="utf-8"))
    if identity.get("closure_sha256") != _sha(args.closure):
        raise RuntimeError("approval closure mismatch")
    intent = json.loads((experiment / "launch_intent.json").read_text(encoding="utf-8"))
    lines = (experiment / "physical_attempt_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    if (len(lines) != 1 or json.loads(lines[0]) != intent or
            intent.get("event") != "native_attempt_reserved" or intent.get("attempt") != 1 or
            intent.get("experiment_id") != EXPERIMENT_ID or intent.get("token") != args.token or
            intent.get("closure_sha256") != _sha(args.closure) or
            (experiment / "raw_waveforms.json").exists()):
        raise RuntimeError("missing or consumed physical launch intent")
    _write_new(experiment / "worker_claim.json", {"attempt": 1, "closure_sha256": _sha(args.closure)})

    from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice

    resolved_library = configure_ngspice()
    library_identity = closure["external_environment"]["ngspice_library"]
    if (Path(resolved_library).resolve() != Path(library_identity["path"]) or
            _sha(Path(resolved_library).resolve()) != library_identity["sha256"]):
        raise RuntimeError("configured ngspice library differs from closure")
    converted = ROOT / plan["data"]["source_assets"]["converted_pyspice"]["path"]
    spec = importlib.util.spec_from_file_location("sc_0259_native_topology", converted)
    if spec is None or spec.loader is None:
        raise RuntimeError("converted topology cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixed = plan["data"]["fixed_parameters"]
    stimulus = plan["stimulus"]
    circuit = module.build_circuit(
        VIN1=_source(stimulus["vin_pwl_points_s_v"]), VCLK1=_clock(stimulus["clock_pulse"]),
        NMOS_MODEL="NMOS_IDEAL", W_M0=fixed["W_M0_m"], L_M0=fixed["L_M0_m"], C0=fixed["C0_f"])
    circuit.model("NMOS_IDEAL", "NMOS", **fixed["model"])
    simulator = circuit.simulator(temperature=fixed["temperature_c"],
                                  nominal_temperature=fixed["nominal_temperature_c"])
    analysis = simulator.transient(step_time=stimulus["max_timestep_s"],
                                   end_time=stimulus["end_time_s"],
                                   use_initial_condition=False)
    waveforms = {"time": [float(item) for item in analysis.time]}
    for name in ("VIN1", "VCLK1", "VOUT1"):
        waveforms[name] = _vector(analysis, name)
    _write_new(experiment / "raw_waveforms.json",
               {"mock": False, "backend": "PySpice-ngspice-linux-isolated-child",
                "closure_sha256": _sha(args.closure),
                "fixture_sha256": plan["fixture"].fixture_hash, "waveforms": waveforms})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
