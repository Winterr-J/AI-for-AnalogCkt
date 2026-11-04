"""Private isolated native worker for the SC-0259 canary; never import in dry-run/tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import uuid
from pathlib import Path


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write((json.dumps(value, allow_nan=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _source(name: str, values: list[list[float]]) -> str:
    if name != "VIN1":
        raise ValueError("only one approved VIN source")
    return "PWL(" + " ".join(f"{t:.17g} {v:.17g}" for t, v in values) + ")"


def _clock(pulse: dict) -> str:
    return "PULSE(" + " ".join(f"{pulse[key]:.17g}" for key in
                                 ("low_v", "high_v", "delay_s", "rise_s", "fall_s",
                                  "high_plateau_s", "period_s")) + ")"


def _vector(analysis, name: str) -> list[float]:
    for key, vector in analysis.nodes.items():
        if str(key).casefold() == name.casefold():
            return [float(item) for item in vector]
    raise ValueError(f"required native waveform unavailable: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)

    # Static contract and consumed launch intent are verified before importing PySpice.
    from ActiveLearning.sc_0259_canary import ROOT, load_plan

    plan = load_plan(args.fixture)
    experiment = args.experiment.resolve(strict=True)
    if experiment != (experiment.parent / plan["config"]["experiment_id"]).resolve():
        raise RuntimeError("experiment directory identity mismatch")
    if experiment.parent != Path(plan["config"]["output_root"]).resolve():
        raise RuntimeError("worker output root differs from frozen one-time ledger root")
    intent = json.loads((experiment / "launch_intent.json").read_text(encoding="utf-8"))
    lines = (experiment / "physical_attempt_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    if (intent.get("event") != "native_attempt_reserved" or intent.get("attempt") != 1 or
            intent.get("token") != args.token or
            intent.get("fixture_file_sha256") != plan["file_sha256"] or
            len(lines) != 1 or json.loads(lines[0]) != intent or
            (experiment / "raw_waveforms.json").exists()):
        raise RuntimeError("missing or consumed guarded launch intent")
    # A direct second invocation cannot replay the parent's one physical attempt.
    with (experiment / "worker_claim.json").open("xb") as stream:
        stream.write((json.dumps({"attempt": 1, "fixture_file_sha256": plan["file_sha256"]}) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())

    from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice

    configure_ngspice()
    converted = ROOT / plan["config"]["source_assets"]["converted_pyspice"]["path"]
    spec = importlib.util.spec_from_file_location("sc_0259_native_topology", converted)
    if spec is None or spec.loader is None:
        raise RuntimeError("converted topology cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fixed = plan["config"]["fixed_parameters"]
    stimulus = plan["stimulus"]
    circuit = module.build_circuit(
        VIN1=_source("VIN1", stimulus["vin_pwl_points_s_v"]),
        VCLK1=_clock(stimulus["clock_pulse"]), NMOS_MODEL="NMOS_IDEAL",
        W_M0=fixed["W_M0_m"], L_M0=fixed["L_M0_m"], C0=fixed["C0_f"])
    circuit.model("NMOS_IDEAL", "NMOS", **fixed["model"])
    simulator = circuit.simulator(temperature=fixed["temperature_c"],
                                  nominal_temperature=fixed["nominal_temperature_c"])
    analysis = simulator.transient(step_time=stimulus["max_timestep_s"],
                                   end_time=stimulus["end_time_s"],
                                   use_initial_condition=False)
    waveforms = {"time": [float(item) for item in analysis.time]}
    for name in ("VIN1", "VCLK1", "VOUT1"):
        waveforms[name] = _vector(analysis, name)
    _write_json(experiment / "raw_waveforms.json",
                {"mock": False, "backend": "PySpice-ngspice-shared-isolated-child",
                 "fixture_sha256": plan["fixture"].fixture_hash,
                 "waveforms": waveforms})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
