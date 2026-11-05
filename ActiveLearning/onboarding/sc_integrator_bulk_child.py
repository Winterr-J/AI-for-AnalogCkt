"""One durable SCIntegrator candidate claim, containing exactly two transients."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path

from ActiveLearning.sc_sample_hold_generic import canonical_sha256
from .sample_hold_bulk_native import _durable_json


def _pwl(points):
    ordered = sorted((float(t), float(v)) for t, v in points)
    if any(ordered[i][0] >= ordered[i + 1][0] for i in range(len(ordered) - 1)):
        raise ValueError("non-increasing SCIntegrator PWL time")
    return "PWL(" + " ".join(f"{t:.17g} {v:.17g}" for t, v in ordered) + ")"


def _clock(plan: dict, name: str) -> str:
    stimulus = plan["stimulus"]
    start, stop = stimulus["clock_phase_windows"][name]
    period, edge = stimulus["period_s"], stimulus["rise_fall_s"]
    low = plan["fixture"]["clock_low_v"]
    high = plan["fixture"]["supply_v"]
    startup = plan["fixture"]["startup_high_until_s"]
    points = ([(0, high), (startup, high), (startup + edge, low)]
              if startup > 0 else [(0, low)])
    for cycle in range(15):
        base = plan["fixture"]["cycle_zero_s"] + cycle * period
        points += [(base + start - edge, low), (base + start, high),
                   (base + stop, high), (base + stop + edge, low)]
    points.append((stimulus["end_time_s"], low))
    return _pwl(points)


def _input(plan: dict, perturbed: bool) -> str:
    end = plan["stimulus"]["end_time_s"]
    zero = plan["fixture"]["zero_input_v"]
    if not perturbed:
        return _pwl([(0, zero), (end, zero)])
    transition = (plan["fixture"]["cycle_zero_s"]
                  + 8.9 * plan["fixture"]["period_s"])
    delta = plan["fixture"]["delta_v"]
    return _pwl([(0, zero), (transition, zero),
                 (transition + 5e-9, zero + delta), (end, zero + delta)])


def _trace(analysis, node: str) -> list[float]:
    if node in {"VSS", "0"}:
        return [0.0 for _ in analysis.time]
    target = node.casefold()
    for name, values in analysis.nodes.items():
        if str(name).casefold() == target:
            return [float(value) for value in values]
    raise ValueError(f"required physical trace missing: {node}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    from ActiveLearning.onboarding import build_active_learning_circuit_config
    from .sc_integrator_bulk_core import ROOT, derive_plan, evaluate_traces

    candidate = args.candidate.resolve(strict=True)
    stage = candidate.parent.parent
    request = json.loads((candidate / "request.json").read_text(encoding="utf-8"))
    identity = json.loads((stage / "identity.json").read_text(encoding="utf-8"))
    intent = json.loads((candidate / "launch_intent.json").read_text(encoding="utf-8"))
    lines = (stage / "physical_attempt_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    if (candidate.parent.name != "candidates"
            or candidate.name != request["vector_sha256"][:16]
            or identity.get("schema") != "sc_integrator_bulk_native_v1"
            or canonical_sha256(identity) != request["identity_sha256"]
            or canonical_sha256(request["arguments"]) != request["vector_sha256"]
            or args.token != request["token"] or intent != json.loads(lines[-1])
            or intent.get("event") != "reserved" or intent.get("token") != args.token
            or intent.get("identity_sha256") != request["identity_sha256"]
            or (candidate / "native_result.json").exists()
            or not (stage / ".lock").is_dir()):
        raise RuntimeError("SCIntegrator durable native claim invalid")
    runtime = build_active_learning_circuit_config("SCIntegrator", identity["circuit_id"])
    plan = derive_plan(identity["circuit_id"])
    if (plan["source_sha256"] != identity["source_sha256"]
            or plan["fixture_sha256"] != identity["fixture_sha256"]
            or plan["stimulus_sha256"] != identity["stimulus_sha256"]
            or canonical_sha256(runtime.param_template) != identity["parameter_space_sha256"]):
        raise RuntimeError("SCIntegrator native source/fixture/parameter identity changed")
    arguments = request["arguments"]
    if set(arguments) != set(runtime.candidate_builder.fixed) | set(runtime.candidate_builder.scales):
        raise RuntimeError("SCIntegrator candidate constructor argument set changed")
    params = {name: float(arguments[name]) / scale
              for name, scale in runtime.candidate_builder.scales.items()}
    rebound = runtime.candidate_builder.expand_candidate(params)
    if set(rebound) != set(arguments) or any(
            not math.isclose(float(rebound[name]), float(arguments[name]), rel_tol=1e-12)
            if type(rebound[name]) in (int, float) else rebound[name] != arguments[name]
            for name in rebound):
        raise RuntimeError("SCIntegrator physical constructor rebinding changed")
    with (candidate / "worker_claim.json").open("x", encoding="utf-8") as stream:
        json.dump({"token": args.token, "vector_sha256": request["vector_sha256"]}, stream)
        stream.flush()
        os.fsync(stream.fileno())

    # Native boundary. No simulator is imported before all durable claims pass.
    from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice
    configure_ngspice()
    topology = ROOT / f"dataset/{identity['circuit_id']}/topology/{identity['circuit_id']}.py"
    module_spec = importlib.util.spec_from_file_location("sc_integrator_bulk_topology", topology)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("SCIntegrator converted topology cannot be imported")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    cases = {}
    for case_id, perturbed in (("baseline", False), ("perturbed", True)):
        kwargs = dict(arguments)
        kwargs[plan["fixture"]["input"]] = _input(plan, perturbed)
        for clock in plan["fixture"]["clocks"]:
            kwargs[clock] = _clock(plan, clock)
        if set(kwargs) != plan["constructor_names"]:
            raise RuntimeError("SCIntegrator constructor binding incomplete")
        circuit = module.build_circuit(**kwargs)
        for name, definition in plan["models"].items():
            circuit.model(name, definition["type"], **definition["parameters"])
        shunt = plan["fixture"]["bias_shunt"]
        if shunt is not None:
            circuit.R("BULK_BIAS_SHUNT", shunt["node"], circuit.gnd,
                      shunt["resistance_ohm"])
            circuit.C("BULK_BIAS_DECOUPLE", shunt["node"], circuit.gnd,
                      shunt["capacitance_f"])
        simulator = circuit.simulator(temperature=27, nominal_temperature=27)
        analysis = simulator.transient(step_time=plan["stimulus"]["step_time_s"],
                                       end_time=plan["stimulus"]["end_time_s"],
                                       use_initial_condition=False)
        virtual = _trace(analysis, plan["fixture"]["virtual_ground"])
        reference = _trace(analysis, plan["fixture"]["virtual_ground_reference"])
        cases[case_id] = {"time_s": [float(t) for t in analysis.time],
                          "input_v": _trace(analysis, plan["fixture"]["input"]),
                          "output_v": _trace(analysis, plan["fixture"]["output"]),
                          "virtual_ground_v": [left - right for left, right in zip(virtual, reference)],
                          "clocks_v": {name: _trace(analysis, name)
                                       for name in plan["fixture"]["clocks"]}}
        _durable_json(candidate / f"{case_id}_waveforms.json", cases[case_id])
    evaluation = evaluate_traces(plan["fixture"], cases["baseline"], cases["perturbed"])
    evaluation["diagnostics"]["native_simulator_attempts"] = 2
    evaluation["diagnostics"]["native_simulator_returns"] = 2
    _durable_json(candidate / "native_result.json", {
        "mock": False, "identity_sha256": request["identity_sha256"],
        "vector_sha256": request["vector_sha256"], "evaluation": evaluation,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
