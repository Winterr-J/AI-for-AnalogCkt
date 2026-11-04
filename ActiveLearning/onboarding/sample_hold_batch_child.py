"""Internal parameterized transient child; no native import until claim validation."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import time


def constructor_contract(runtime, plan: dict, arguments: dict) -> tuple[dict, dict, float, float]:
    """Pure parameter substitution over the previously verified nominal fixture."""
    from ActiveLearning.sc_sample_hold_canary_child import _constructor_arguments, _pulse, _pwl

    stimulus = plan["stimulus"]
    if runtime.circuit_id == "circuit_0259":
        kwargs = {**arguments,
                  "VIN1": _pwl(stimulus["vin_pwl_points_s_v"]),
                  "VCLK1": _pulse(stimulus["clock_pulse"])}
        fixed = plan["config"]["fixed_parameters"]
        return kwargs, fixed["model"], fixed["temperature_c"], fixed["nominal_temperature_c"]
    in_memory = dict(plan["config"])
    assembly = dict(in_memory["assembly"])
    assembly["constructor_arguments"] = {
        key: ({"kind": "fixed", "value": arguments[key]} if key in arguments else value)
        for key, value in assembly["constructor_arguments"].items()
    }
    in_memory["assembly"] = assembly
    kwargs = _constructor_arguments(in_memory, stimulus)
    return (kwargs, plan["config"]["assembly"]["model"],
            plan["config"]["temperature_c"], plan["config"]["nominal_temperature_c"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--token", required=True)
    parser.add_argument("--extension", type=Path)
    parser.add_argument("--al-pilot", action="store_true")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)

    import yaml
    from ActiveLearning.onboarding import build_active_learning_circuit_config
    from ActiveLearning.onboarding.sample_hold_batch import (
        ROOT, _canonical_sha, _frozen_plan, _al_plan, al_pilot_contract,
        operator_contract, validate_approval,
    )
    from ActiveLearning.onboarding.d0_execution_windows import read_window
    from ActiveLearning.sc_sample_hold_generic import evaluate_physical, physical_waveforms, sha256_file
    from ActiveLearning.sc_sample_hold_canary_child import _write_json

    config_path = args.config.resolve(strict=True)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    circuit_id = config["circuit_info"]["circuit_id"]
    if config["circuit_info"].get("circuit_type") != "SampleHold":
        raise RuntimeError("not a SampleHold D0 configuration")
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    data = config["data"]
    if args.al_pilot:
        if args.approval is not None or args.extension is not None:
            raise RuntimeError("AL pilot does not accept D0 approval or extension files")
        pilot = config["sample_hold_pilot"]
        root = (args.output_root or Path(pilot["output_root"])).resolve()
        experiment_id = pilot["execution_id"]
        contract_key = "execution_contract_sha256"
        contract_sha = _canonical_sha(al_pilot_contract(
            runtime, config_path, root, experiment_id, pilot["budget"],
        ))
        if os.name == "posix":
            from ActiveLearning.onboarding.sample_hold_ubuntu import runtime_preflight
            from ActiveLearning.main import _resolve_experiment
            d0_path = _resolve_experiment(
                "SampleHold", circuit_id, config_path, runtime,
            )[3].resolve(strict=True)
            actual_preflight = runtime_preflight(runtime, config_path, d0_path, pilot)
            record_path = root / experiment_id / "runtime_preflight.json"
            if (not record_path.is_file() or
                    json.loads(record_path.read_text(encoding="utf-8")) != actual_preflight):
                raise RuntimeError("Ubuntu AL runtime preflight record changed")
    else:
        if args.output_root is not None:
            raise RuntimeError("D0 execution does not accept an AL output root")
        root = Path(data["batch_output_root"]).resolve()
        experiment_id = data["batch_experiment_id"]
    operator_full = data.get("batch_execution_mode") == "operator_full"
    if not args.al_pilot and operator_full:
        if (args.approval is not None or args.extension is not None
                or data.get("batch_approval_file") is not None
                or data.get("batch_review_gate") is not None):
            raise RuntimeError("operator-full D0 cannot carry approval/review files")
        contract_key = "execution_contract_sha256"
        contract_sha = _canonical_sha(operator_contract(
            runtime, config_path, root, experiment_id, data["batch_budget"]))
    elif not args.al_pilot:
        if args.approval is None:
            raise RuntimeError("separate D0 batch approval file required")
        extension_kwargs = (
            {"extension_path": args.extension,
             "experiment_path": root / experiment_id,
             "checkpoint_path": config_path.parent / data["d0_path"],
             "review_path": Path(data["batch_review_gate"]["continuation_review_file"])}
            if args.extension is not None else {}
        )
        validate_approval(runtime, config_path, root, experiment_id, data["batch_budget"],
                          args.approval, **extension_kwargs)
        contract_key = "approval_sha256"
        contract_sha = sha256_file(args.approval)
    if not args.al_pilot and args.extension is not None:
        window = read_window(
            root / experiment_id / "execution_window_v1.json",
            root / experiment_id / "physical_attempt_ledger.jsonl",
            extension_sha256=sha256_file(args.extension),
            continuation_window_s=data["batch_budget"]["overall_timeout_s"],
        )
        if window is None or time.time() >= window["deadline_unix_s"]:
            raise RuntimeError("D0 continuation execution window not active")
    plan = _al_plan(runtime) if args.al_pilot else _frozen_plan(runtime)
    if circuit_id != "circuit_0259" and os.name != "posix":
        from ActiveLearning.sc_sample_hold_canary import _native_environment_verified
        if not _native_environment_verified(plan["closure"]):
            raise RuntimeError("strict native environment mismatch")
    candidate = args.candidate.resolve(strict=True)
    if candidate.parent != (root / experiment_id / "candidates").resolve():
        raise RuntimeError("candidate escaped independent D0 experiment")
    request = json.loads((candidate / "input.json").read_text(encoding="utf-8"))
    identity = json.loads((root / experiment_id / "identity.json").read_text(encoding="utf-8"))
    if (request.get(contract_key) != contract_sha
            or identity.get(contract_key) != contract_sha):
        raise RuntimeError("candidate execution contract hash mismatch")
    if not (root / (experiment_id + ".lock")).is_dir():
        raise RuntimeError("batch execution lock missing")
    arguments = request["arguments"]
    vector_sha = request["vector_sha256"]
    if candidate.name != vector_sha[:16] or _canonical_sha(arguments) != vector_sha:
        raise RuntimeError("candidate vector hash mismatch")
    expected_params = {name: arguments[name] / scale
                       for name, scale in runtime.candidate_builder.scales.items()}
    rebound = runtime.candidate_builder.expand_candidate(expected_params)
    if (set(rebound) != set(arguments) or any(
            (not math.isclose(value, arguments[name], rel_tol=1e-12, abs_tol=0)
             if type(value) in (int, float) and type(arguments[name]) in (int, float)
             else value != arguments[name]) for name, value in rebound.items())):
        raise RuntimeError("candidate changed fixed topology/model/operating values")
    intent = json.loads((candidate / "launch_intent.json").read_text(encoding="utf-8"))
    ledger = (root / experiment_id / "physical_attempt_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    last_is_intent = bool(ledger and json.loads(ledger[-1]) == intent)
    first_extension_launch = bool(
        args.extension is not None and len(ledger) >= 2
        and json.loads(ledger[-2]) == intent
        and json.loads(ledger[-1]).get("event") == "execution_window_activated")
    if (not (last_is_intent or first_extension_launch)
            or intent.get("event") != "native_attempt_reserved"
            or intent.get("token") != args.token
            or intent.get("vector_sha256") != vector_sha
            or (candidate / "native_result.json").exists()):
        raise RuntimeError("missing/consumed durable native attempt reservation")
    with (candidate / "worker_claim.json").open("xb") as stream:
        stream.write((json.dumps({"vector_sha256": vector_sha, "token": args.token}) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())

    # The only native boundary: all approval, source and candidate checks above.
    _write_json(candidate / "worker_stage.json", {"stage": "native_initialization"})
    from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice
    configure_ngspice()
    spec = importlib.util.spec_from_file_location("sc_d0_native_topology", runtime.topology_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("converted topology unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stimulus = plan["stimulus"]
    kwargs, model, temperature, nominal_temperature = constructor_contract(
        runtime, plan, arguments)
    circuit = module.build_circuit(**kwargs)
    circuit.model("NMOS_IDEAL", "NMOS", **model)
    simulator = circuit.simulator(temperature=temperature,
                                  nominal_temperature=nominal_temperature)
    _write_json(candidate / "worker_stage.json", {"stage": "transient_simulation"})
    analysis = simulator.transient(step_time=stimulus["max_timestep_s"],
                                   end_time=stimulus["end_time_s"],
                                   use_initial_condition=False)
    _write_json(candidate / "worker_stage.json", {"stage": "measurement"})
    traces = physical_waveforms(analysis, plan["fixture"])
    _write_json(candidate / "raw_waveforms.json", {
        "mock": False, "vector_sha256": vector_sha,
        "fixture_sha256": plan["fixture"].fixture_hash,
        "stimulus_sha256": plan["config"]["stimulus_sha256"],
        "waveforms": traces,
    })
    evaluation = evaluate_physical(plan["fixture"], traces, mock=False)["evaluation"]
    _write_json(candidate / "native_result.json", {
        "vector_sha256": vector_sha, "mock": False,
        "fixture_sha256": plan["fixture"].fixture_hash,
        "stimulus_sha256": plan["config"]["stimulus_sha256"],
        "evaluation": evaluation,
    })
    _write_json(candidate / "worker_stage.json", {"stage": "complete"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
