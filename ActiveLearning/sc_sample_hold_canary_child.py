"""Isolated native E1 worker. Importing does not initialize PySpice/ngspice."""

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
            stream.write((json.dumps(value, allow_nan=False) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _pwl(points: list[list[float]]) -> str:
    return "PWL(" + " ".join(f"{time:.17g} {voltage:.17g}" for time, voltage in points) + ")"


def _pulse(clock: dict) -> str:
    return "PULSE(" + " ".join(f"{clock[key]:.17g}" for key in
                                 ("low_v", "high_v", "delay_s", "rise_s", "fall_s",
                                  "high_plateau_s", "period_s")) + ")"


def _constructor_arguments(config: dict, stimulus: dict) -> dict:
    """Map named physical sources and fixed model/device values; no topology rewriting."""
    args = {}
    for name, spec in config["assembly"]["constructor_arguments"].items():
        if spec["kind"] == "input":
            args[name] = _pwl(stimulus["input_pwl_points_s_v"][spec["name"]])
        elif spec["kind"] == "clock":
            if spec["name"] != stimulus["clock_pulse"]["source"]:
                raise ValueError("clock constructor mapping differs from physical clock")
            args[name] = _pulse(stimulus["clock_pulse"])
        elif spec["kind"] == "fixed":
            args[name] = spec["value"]
        else:
            raise ValueError("unknown constructor argument kind")
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)

    # Validate all source/fixture/ledger identity BEFORE any native import.
    from ActiveLearning.sc_sample_hold_canary import load_plan, _native_environment_verified
    from ActiveLearning.sc_sample_hold_generic import ROOT, physical_waveforms

    plan = load_plan(args.candidate)
    if not _native_environment_verified(plan["closure"]):
        raise RuntimeError("strict native environment is not frozen/verified")
    experiment = args.experiment.resolve(strict=True)
    config = plan["config"]
    if (experiment != (Path(config["output_root"]) / config["experiment_id"]).resolve()
            or experiment.parent != Path(config["output_root"]).resolve()):
        raise RuntimeError("worker output root/experiment identity mismatch")
    intent = json.loads((experiment / "launch_intent.json").read_text(encoding="utf-8"))
    ledger = (experiment / "physical_attempt_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    if (intent.get("event") != "native_attempt_reserved" or intent.get("attempt") != 1
            or intent.get("token") != args.token
            or intent.get("candidate_file_sha256") != plan["file_sha256"]
            or len(ledger) != 1 or json.loads(ledger[0]) != intent
            or (experiment / "raw_waveforms.json").exists()):
        raise RuntimeError("missing/consumed guarded native launch intent")
    with (experiment / "worker_claim.json").open("xb") as stream:
        stream.write((json.dumps({"attempt": 1,
                                  "candidate_file_sha256": plan["file_sha256"]}) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())

    # The only native boundary: this module's import and dry-run never enter it.
    from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice

    configure_ngspice()
    converted = plan["paths"]["converted"]
    spec = importlib.util.spec_from_file_location("sc_generic_native_topology", converted)
    if spec is None or spec.loader is None:
        raise RuntimeError("converted topology cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    kwargs = _constructor_arguments(config, plan["stimulus"])
    circuit = module.build_circuit(**kwargs)
    circuit.model("NMOS_IDEAL", "NMOS", **config["assembly"]["model"])
    simulator = circuit.simulator(
        temperature=config["temperature_c"],
        nominal_temperature=config["nominal_temperature_c"])
    stimulus = plan["stimulus"]
    analysis = simulator.transient(step_time=stimulus["max_timestep_s"],
                                   end_time=stimulus["end_time_s"],
                                   use_initial_condition=False)
    waveforms = physical_waveforms(analysis, plan["fixture"])
    _write_json(experiment / "raw_waveforms.json", {
        "mock": False, "backend": "PySpice-ngspice-shared-isolated-child",
        "fixture_sha256": plan["fixture"].fixture_hash,
        "stimulus_sha256": config["stimulus_sha256"],
        "waveforms": waveforms})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
