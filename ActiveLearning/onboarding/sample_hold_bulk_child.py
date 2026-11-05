"""One claimed SampleHold bulk transient; no native import before identity checks."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path


def _pwl(points: list[list[float]]) -> str:
    return "PWL(" + " ".join(f"{t:.17g} {v:.17g}" for t, v in points) + ")"


def _pulse(values: dict) -> str:
    return "PULSE(" + " ".join(f"{values[key]:.17g}" for key in (
        "low_v", "high_v", "delay_s", "rise_s", "fall_s",
        "high_plateau_s", "period_s")) + ")"


def _complement(primary: dict) -> dict:
    period = primary["period_s"]
    edge = primary["rise_s"]
    dead = period * .02
    delay = period * .5 + dead
    plateau = period * .5 - 2 * dead - edge * 2
    if plateau <= 0:
        raise ValueError("SampleHold complementary clock has no non-overlap plateau")
    return {**primary, "delay_s": delay, "high_plateau_s": plateau}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)

    from ActiveLearning.onboarding import build_active_learning_circuit_config
    from ActiveLearning.onboarding.sample_hold_bulk_core import ROOT, derive_plan
    from ActiveLearning.sc_sample_hold_generic import (
        canonical_sha256, evaluate_physical, physical_waveforms,
    )
    from ActiveLearning.onboarding.sample_hold_bulk_native import _durable_json

    candidate = args.candidate.resolve(strict=True)
    stage = candidate.parent.parent
    request = json.loads((candidate / "request.json").read_text(encoding="utf-8"))
    identity = json.loads((stage / "identity.json").read_text(encoding="utf-8"))
    intent = json.loads((candidate / "launch_intent.json").read_text(encoding="utf-8"))
    lines = (stage / "physical_attempt_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    if (candidate.parent.name != "candidates"
            or candidate.name != request["vector_sha256"][:16]
            or canonical_sha256(identity) != request["identity_sha256"]
            or canonical_sha256(request["arguments"]) != request["vector_sha256"]
            or args.token != request["token"] or intent != json.loads(lines[-1])
            or intent.get("event") != "reserved" or intent.get("token") != args.token
            or intent.get("identity_sha256") != request["identity_sha256"]
            or (candidate / "native_result.json").exists()
            or not (stage / ".lock").is_dir()):
        raise RuntimeError("SampleHold bulk durable native claim invalid")
    runtime = build_active_learning_circuit_config("SampleHold", identity["circuit_id"])
    plan = derive_plan(identity["circuit_id"])
    if (plan["source_sha256"] != identity["source_sha256"]
            or plan["fixture_sha256"] != identity["fixture_sha256"]
            or plan["stimulus_sha256"] != identity["stimulus_sha256"]
            or canonical_sha256(runtime.param_template) != identity["parameter_space_sha256"]):
        raise RuntimeError("SampleHold native source/fixture/parameter identity changed")
    arguments = request["arguments"]
    if set(arguments) != set(runtime.candidate_builder.fixed) | set(runtime.candidate_builder.scales):
        raise RuntimeError("SampleHold candidate constructor argument set changed")
    params = {name: float(arguments[name]) / scale
              for name, scale in runtime.candidate_builder.scales.items()}
    rebound = runtime.candidate_builder.expand_candidate(params)
    if set(rebound) != set(arguments) or any(
            not math.isclose(rebound[name], arguments[name], rel_tol=1e-12)
            if type(rebound[name]) in (int, float) else rebound[name] != arguments[name]
            for name in rebound):
        raise RuntimeError("SampleHold physical constructor rebinding changed")
    with (candidate / "worker_claim.json").open("x", encoding="utf-8") as stream:
        json.dump({"token": args.token, "vector_sha256": request["vector_sha256"]}, stream)
        stream.flush()
        os.fsync(stream.fileno())

    # Native boundary: everything above is simulator-free and fail-closed.
    from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice
    configure_ngspice()
    topology = ROOT / f"dataset/{identity['circuit_id']}/topology/{identity['circuit_id']}.py"
    module_spec = importlib.util.spec_from_file_location("sample_hold_bulk_topology", topology)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("SampleHold converted topology cannot be imported")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    stimulus = plan["stimulus"]
    kwargs = dict(arguments)
    for name in plan["fixture"].mapping.inputs:
        kwargs[name] = _pwl(stimulus["input_pwl_points_s_v"][name])
    kwargs[plan["fixture"].mapping.clock] = _pulse(stimulus["clock_pulse"])
    for name in plan["complement_clocks"]:
        kwargs[name] = _pulse(_complement(stimulus["clock_pulse"]))
    if set(kwargs) != plan["constructor_names"]:
        raise RuntimeError("SampleHold constructor binding incomplete")
    circuit = module.build_circuit(**kwargs)
    operating = plan["archive"].get("operating_parameters") or {}
    if "VDD" not in kwargs and "VDD" in operating:
        circuit.V("BULK_VDD", "VDD", circuit.gnd, float(operating["VDD"]))
    for name, definition in plan["models"].items():
        circuit.model(name, definition["type"], **definition["parameters"])
    simulator = circuit.simulator(temperature=27, nominal_temperature=27)
    analysis = simulator.transient(step_time=stimulus["max_timestep_s"],
                                   end_time=stimulus["end_time_s"],
                                   use_initial_condition=False)
    traces = physical_waveforms(analysis, plan["fixture"])
    _durable_json(candidate / "raw_waveforms.json", {
        "mock": False, "fixture_sha256": plan["fixture_sha256"],
        "stimulus_sha256": plan["stimulus_sha256"], "waveforms": traces,
    })
    evaluation = evaluate_physical(plan["fixture"], traces, mock=False)["evaluation"]
    _durable_json(candidate / "native_result.json", {
        "mock": False, "identity_sha256": request["identity_sha256"],
        "vector_sha256": request["vector_sha256"], "evaluation": evaluation,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
