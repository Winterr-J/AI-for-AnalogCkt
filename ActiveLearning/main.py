"""Generic command-line entry point for Active Learning experiments."""

import argparse
from copy import deepcopy
import json
from pathlib import Path, PureWindowsPath

import yaml

from ActiveLearning.active_learning import run_active_learning
from ActiveLearning.validity.validity_memory import VALIDITY_V1_DEFAULTS
from ActiveLearning.d0_initialization import initialize_configured_d0
from ActiveLearning.experiment_logging import (
    create_run_directory,
    write_completed_run,
    write_config_snapshot,
)
from ActiveLearning.onboarding import (
    AutoConfigError,
    build_active_learning_circuit_config,
)


_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _ROOT.parent
_EXAMPLES_ROOT = _ROOT / "InitializeD0" / "examples"
_RUNS_ROOT = _ROOT / "runs"


def _load_yaml(path):
    with Path(path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _load_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _resolve_paths(circuit_type, circuit_id, config_path=None):
    topology = (
        _REPOSITORY_ROOT
        / "dataset"
        / circuit_id
        / "topology"
        / f"{circuit_id}.py"
    )
    performance_template = (
        _ROOT / "circuit_types" / circuit_type / "performance_template.yaml"
    )
    if not topology.is_file() or not performance_template.is_file():
        raise ValueError(
            f"Unsupported Active Learning circuit: {circuit_type}/{circuit_id}"
        )

    circuit_dir = _EXAMPLES_ROOT / circuit_type / circuit_id
    return {
        "circuit_dir": circuit_dir,
        "parameter_template": circuit_dir / "parameter_template.yaml",
        "topology": topology,
        "performance_template": performance_template,
        "config": (
            Path(config_path).resolve()
            if config_path is not None
            else circuit_dir / "active_learning_config.yaml"
        ),
    }


def _load_adapter(circuit_type, circuit_id, runtime_config=None):
    """Return the generic adapter; legacy circuit adapters are reference-only."""

    runtime = runtime_config or build_active_learning_circuit_config(
        circuit_type,
        circuit_id,
        repository_root=_REPOSITORY_ROOT,
    )
    return runtime.adapter


def _relative_to_circuit(path, circuit_dir):
    resolved = Path(path)
    return resolved if resolved.is_absolute() else circuit_dir / resolved


def _configured_targets(config):
    target_specs = config.get("target_specs")
    if not target_specs:
        raise ValueError("target_specs must be configured before running")
    missing = [
        name
        for name, spec in target_specs.items()
        if spec.get("value") is None
    ]
    if missing:
        raise ValueError(
            "target_specs values must be configured before running: "
            + ", ".join(missing)
        )
    return target_specs


def _resolve_experiment(circuit_type, circuit_id, config_path, runtime):
    paths = _resolve_paths(circuit_type, circuit_id, config_path)
    if not paths["config"].is_file():
        raise AutoConfigError(
            f"AutoConfig succeeded for {circuit_type}/{circuit_id}, but performance "
            f"targets are not configured: {paths['config']}"
        )
    config = _load_yaml(paths["config"])
    target_specs = _configured_targets(config)
    data_config = config.get("data") or {}
    if not data_config.get("d0_path"):
        raise ValueError("data.d0_path must be configured before running")
    d0_mode = data_config.get("d0_mode")
    if d0_mode is not None and d0_mode not in {"reuse", "independent"}:
        raise ValueError("data.d0_mode must be reuse or independent")
    path_base = data_config.get("d0_path_relative_to", "circuit")
    if path_base not in {"circuit", "config"}:
        raise ValueError("data.d0_path_relative_to must be circuit or config")
    raw_d0_path = data_config["d0_path"]
    if d0_mode == "independent":
        if (path_base != "config" or Path(raw_d0_path).is_absolute()
                or PureWindowsPath(raw_d0_path).drive or "\\" in raw_d0_path):
            raise ValueError("independent D0 requires a config-relative path")
        if data_config.get("initialize_if_missing") is not True:
            raise ValueError("independent D0 requires initialize_if_missing: true")
        if not isinstance(data_config.get("initial_anchor"), dict):
            raise ValueError("independent D0 requires a profiling initial_anchor")
    base_dir = paths["config"].parent if path_base == "config" else paths["circuit_dir"]
    d0_path = _relative_to_circuit(raw_d0_path, base_dir)
    if d0_mode == "independent" and not d0_path.resolve().is_relative_to(
        paths["config"].parent.resolve()
    ):
        raise ValueError("independent D0 must stay inside the generated config directory")
    return paths, config, target_specs, d0_path


def _check_independent_d0(config, d0_path):
    data = config.get("data") or {}
    if data.get("d0_mode") != "independent" or not d0_path.is_file():
        return
    csv_path = d0_path.with_suffix(".csv")
    manifest = d0_path.with_name(d0_path.stem + "_initialization.json")
    if not csv_path.is_file() or not manifest.is_file():
        raise AutoConfigError(f"independent D0 is incomplete: {d0_path}")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    anchor = data["initial_anchor"]
    if (metadata.get("d0_mode") != "independent"
            or metadata.get("anchor_sample_id") != str(anchor.get("sample_id"))
            or metadata.get("seed") != data.get("seed")):
        raise AutoConfigError(f"independent D0 provenance does not match config: {manifest}")


def _inspect_experiment_config(circuit_type, circuit_id, config_path=None):
    """Load optional experiment targets for config-only inspection.

    Structural AutoConfig remains usable for circuits that have not yet received
    an experiment configuration.  When a config exists, its targets are parsed
    through the same generic schema used by D0 and Active Learning.
    """

    paths = _resolve_paths(circuit_type, circuit_id, config_path)
    if not paths["config"].is_file():
        return None
    config = _load_yaml(paths["config"])
    experiment = {
        "config_path": paths["config"].resolve(),
        "config": config,
        "target_specs": _configured_targets(config),
    }
    if (config.get("data") or {}).get("d0_mode") is not None:
        experiment["resolved_d0_path"] = _resolve_experiment(
            circuit_type, circuit_id, config_path, None
        )[3].resolve()
    return experiment


def initialize_experiment_d0(
    circuit_type,
    circuit_id,
    *,
    config_path=None,
    runtime_config=None,
):
    """Initialize the experiment D0 without entering the Active Learning loop."""

    runtime = runtime_config or build_active_learning_circuit_config(
        circuit_type,
        circuit_id,
        repository_root=_REPOSITORY_ROOT,
    )
    _paths, config, target_specs, d0_path = _resolve_experiment(
        circuit_type,
        circuit_id,
        config_path,
        runtime,
    )
    if d0_path.is_file():
        _check_independent_d0(config, d0_path)
        manifest = d0_path.with_name(d0_path.stem + "_initialization.json")
        return {
            "records": _load_jsonl(d0_path),
            "artifacts": {
                "jsonl": d0_path.resolve(),
                **({"manifest": manifest.resolve()} if manifest.is_file() else {}),
            },
            "sampling": None,
            "created": False,
            "target_specs": target_specs,
            "d0_mode": (config.get("data") or {}).get("d0_mode", "configured"),
            "anchor_sample_id": ((config.get("data") or {}).get("initial_anchor") or {}).get("sample_id"),
        }
    if (config.get("data") or {}).get("d0_mode") == "reuse":
        raise AutoConfigError(f"reused D0 is not available: {d0_path}")
    result = initialize_configured_d0(
        runtime,
        config,
        target_specs,
        d0_path,
    )
    return {**result, "created": True, "target_specs": target_specs}


def run_configured_active_learning(
    circuit_type,
    circuit_id,
    *,
    config_path=None,
    llm_client=None,
    runtime_config=None,
):
    """Auto-configure one circuit and delegate to the unchanged AL loop."""
    runtime = runtime_config or build_active_learning_circuit_config(
        circuit_type,
        circuit_id,
        repository_root=_REPOSITORY_ROOT,
    )
    paths, config, target_specs, d0_path = _resolve_experiment(
        circuit_type,
        circuit_id,
        config_path,
        runtime,
    )
    parameter_template = runtime.param_template
    performance_template = _load_yaml(paths["performance_template"])
    if not d0_path.is_file():
        if ((config.get("data") or {}).get("initialize_if_missing") is True
                and (config.get("data") or {}).get("d0_mode") != "reuse"):
            initialize_configured_d0(runtime, config, target_specs, d0_path)
        else:
            raise AutoConfigError(
                f"AutoConfig succeeded for {circuit_type}/{circuit_id}, but D0 is not "
                f"available: {d0_path}"
            )
    _check_independent_d0(config, d0_path)
    records = _load_jsonl(d0_path)
    circuit_info = dict(parameter_template["circuit"])
    circuit_info.update(config.get("circuit_info", {}))
    settings = config.get("active_learning", {})
    effective_settings = {
        "max_iterations": settings.get("max_iterations", 10),
        "beta": settings.get("beta", 0.7),
        "acquisition_mode": settings.get(
            "acquisition_mode", "conservative_feasibility"
        ),
        "kappa": settings.get("kappa", 1.0),
        "context_k": settings.get("context_k", 5),
        "retrieval_k": settings.get("retrieval_k", 3),
        "committee_size": settings.get("committee_size", 5),
        "data_expansion_seed": settings.get("data_expansion_seed", 42),
    }
    data_expansion_config = deepcopy(config.get("data_expansion") or {"mode": "v1"})
    if data_expansion_config.get("mode", "v1") not in {"v1", "v2"}:
        raise ValueError("data_expansion.mode must be v1 or v2")
    data_expansion_config.setdefault("mode", "v1")
    validity_settings = {
        **VALIDITY_V1_DEFAULTS,
        **(config.get("validity") or {}),
    }
    run_context = create_run_directory(
        _RUNS_ROOT,
        circuit_type,
        circuit_id,
        effective_settings["acquisition_mode"],
        data_expansion_mode=data_expansion_config["mode"],
    )
    effective_config = deepcopy(config)
    effective_config.setdefault(
        "target_source",
        "auto_generated" if (config.get("target_provenance") or {}).get("source") == "automatic_profiling_llm_v1"
        else "user_specified",
    )
    effective_config["circuit_info"] = circuit_info
    effective_config["target_specs"] = target_specs
    effective_config["active_learning"] = effective_settings
    effective_config["data_expansion"] = data_expansion_config
    effective_config["validity"] = validity_settings
    effective_config["autoconfig"] = runtime.summary()
    effective_config["data"] = {
        **deepcopy(config.get("data", {})),
        "resolved_d0_path": str(d0_path.resolve()),
    }
    effective_config["output"] = {
        "run_id": run_context["run_id"],
        "run_directory": str(run_context["run_directory"].resolve()),
    }
    write_config_snapshot(run_context["run_directory"], effective_config)

    result = run_active_learning(
        records=records,
        circuit_info=circuit_info,
        parameter_template=parameter_template,
        performance_template=performance_template,
        target_specs=target_specs,
        adapter=_load_adapter(circuit_type, circuit_id, runtime),
        runtime=runtime,
        max_iterations=effective_settings["max_iterations"],
        beta=effective_settings["beta"],
        acquisition_mode=effective_settings["acquisition_mode"],
        context_k=effective_settings["context_k"],
        retrieval_k=effective_settings["retrieval_k"],
        committee_size=effective_settings["committee_size"],
        data_expansion_seed=effective_settings["data_expansion_seed"],
        data_expansion_config=data_expansion_config,
        output_dir=run_context["run_directory"],
        run_id=run_context["run_id"],
        llm_client=llm_client,
        validity_config=validity_settings,
    )
    write_completed_run(
        _RUNS_ROOT,
        run_context,
        result,
        circuit_type=circuit_type,
        circuit_id=circuit_id,
        acquisition_mode=effective_settings["acquisition_mode"],
        data_expansion_mode=data_expansion_config["mode"],
        benchmark_version=config.get("benchmark_version"),
    )
    return result


def _print_summary(circuit_type, circuit_id, result):
    print(f"circuit_type: {circuit_type}")
    print(f"circuit_id: {circuit_id}")
    print(f"iterations_completed: {result['iterations_completed']}")
    print(f"stop_reason: {result['stop_reason']}")
    if "total_timing" in result:
        print("total_timing: " + json.dumps(result["total_timing"], default=float))
        print("total_tokens: " + json.dumps(result["total_tokens"]))
        print(f"llm_calls: {result['llm_calls']}")
    final_record = result["best_or_final_record"]
    if final_record is not None:
        status = final_record["status"]
        print(f"simulation_valid: {status['simulation_valid']}")
        print(f"meets_spec: {status['meets_spec']}")
        print(
            "metrics: "
            + json.dumps(final_record["metrics"], ensure_ascii=False, default=float)
        )


def _print_autoconfig_summary(runtime):
    summary = runtime.summary()
    semantic = summary["semantic_mapping"]
    print("AutoConfig:")
    print(f"  circuit_id: {summary['circuit_id']}")
    print(f"  circuit_type: {summary['circuit_type']}")
    print(f"  parameters: {summary['parameters']}")
    print(f"  auto_bound: {summary['auto_bound']}")
    print(f"  unresolved: {summary['unresolved']}")
    print(f"  simulator: {summary['simulator']}")
    print(f"  semantic_resolver: {summary['semantic_resolver']}")
    print(f"  evaluator: {summary['evaluator']}")
    print(f"  functional_validator: {summary['functional_validator']}")
    print(f"  performance_template: {summary['performance_template']}")
    print(f"  parameter_source: {summary['parameter_source']}")
    print("  parameter_nominals:")
    for parameter in runtime.runtime_parameters:
        print(f"    {parameter.name}: {parameter.nominal}")
    print("  operating_conditions:")
    for name, value in summary["operating_conditions"].items():
        print(f"    {name}: {value}")
    print("  testbench:")
    rendered_testbench = yaml.safe_dump(
        summary["testbench"],
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    for line in rendered_testbench.splitlines():
        print(f"    {line}")
    print("  semantic_mapping:")
    if "reference_current_measurement" in semantic:
        reference = semantic["reference_current_measurement"]
        output = semantic["output_current_measurement"]
        print("    reference_current:")
        print(f"      type: {reference['type']}")
        print(f"      element: {reference['element']}")
        print(f"      measurement: {reference['section']}/{reference['name']}")
        print("      evidence:")
        for item in reference["evidence"]:
            print(f"        - {item}")
        excitation = semantic.get("reference_excitation")
        print("    reference_excitation:")
        if excitation is None:
            print("      status: unavailable")
        else:
            print(f"      status: {excitation['status']}")
            print(f"      type: {excitation['type']}")
            print(f"      element: {excitation['element']}")
            print(f"      parameter: {excitation['parameter']}")
            print(f"      nominal: {excitation['nominal']}")
            if excitation["reason"]:
                print(f"      reason: {excitation['reason']}")
            print("      evidence:")
            for item in excitation["evidence"]:
                print(f"        - {item}")
            if excitation["candidates"]:
                print("      candidates:")
                for candidate in excitation["candidates"]:
                    print(
                        "        - "
                        f"{candidate['element']} ({candidate['type']}, "
                        f"{candidate['topology_role']})"
                    )
        print("    output_current:")
        print(f"      type: {output['type']}")
        print(f"      element: {output['element']}")
        print(f"      op_measurement: {output['op_section']}/{output['op_name']}")
        print(f"      dc_measurement: {output['dc_section']}/{output['dc_name']}")
        print(f"    output_node: {semantic['output_node']}")
        print(f"    sweep_element: {semantic['sweep_element']}")
    else:
        inputs = semantic["inputs"]
        outputs = semantic["outputs"]
        feedback = semantic["feedback"]
        common_mode = semantic["common_mode_condition"]
        print(f"    input_exposure: {inputs['exposure']}")
        print(f"    polarity_status: {inputs['polarity_status']}")
        print(f"    positive_input: {inputs['positive']['node']}")
        print(f"    negative_input: {inputs['negative']['node']}")
        print(f"    output_mode: {outputs['mode']}")
        print(f"    output_node: {outputs['positive_node']}")
        print(f"    feedback_exposure: {feedback['exposure']}")
        print(f"    feedback_status: {feedback['status']}")
        print(f"    input_common_mode_v: {common_mode['input_common_mode_v']}")
        print("    measurement_modes:")
        for mode in semantic["measurement_modes"]:
            print(f"      - {mode}")


def _print_experiment_config_summary(experiment):
    config = experiment["config"]
    print("Experiment Config:")
    if config.get("benchmark_version") is not None:
        print(f"  benchmark_version: {config['benchmark_version']}")
    print("  performance_target:")
    for name, spec in experiment["target_specs"].items():
        print(f"    {name} {spec['operator']} {spec['value']}")
    if config.get("performance_target_selected") is not None:
        print(
            "  performance_target_selected: "
            f"{str(bool(config['performance_target_selected'])).lower()}"
        )
    if (config.get("data") or {}).get("d0_mode") is not None:
        print(f"  d0_mode: {config['data']['d0_mode']}")
        print(f"  d0_path: {experiment['resolved_d0_path']}")
    print(f"  config: {experiment['config_path']}")


def _print_d0_summary(result):
    records = result["records"]
    sampling = result.get("sampling") or {}
    functional_valid_count = sum(
        item["status"].get("functional_valid") is True
        or item["status"].get("functional_feasible") is True
        for item in records
    )
    meets_spec_count = sum(
        item["status"]["meets_spec"] is True for item in records
    )
    print("D0 Initialization:")
    print(f"  created: {result['created']}")
    if result.get("d0_mode") is not None:
        print(f"  d0_mode: {result['d0_mode']}")
    if result.get("anchor_sample_id") is not None:
        print(f"  anchor_sample_id: {result['anchor_sample_id']}")
    print(f"  requested_samples: {sampling.get('n_samples', len(records))}")
    print(f"  records: {len(records)}")
    print(
        "  nominal_samples: "
        f"{sum(item['sampling_source'] == 'nominal' for item in records)}"
    )
    print(
        "  anchor_samples: "
        f"{sum(item['sampling_source'] == 'anchor' for item in records)}"
    )
    print(
        "  local_samples: "
        f"{sum(item['sampling_source'] == 'local' for item in records)}"
    )
    print(
        "  global_samples: "
        f"{sum(item['sampling_source'] == 'global' for item in records)}"
    )
    print(
        "  parameter_valid: "
        f"{sum(item['status']['parameter_valid'] is True for item in records)}"
    )
    print(
        "  simulation_valid: "
        f"{sum(item['status']['simulation_valid'] is True for item in records)}"
    )
    print(
        "  functional_valid: "
        f"{functional_valid_count}"
    )
    print(f"  meets_spec: {meets_spec_count}")
    print(f"  meets_spec_count: {meets_spec_count}")
    if sampling:
        print(f"  sampling_mode: {sampling['sampling_mode']}")
        print(f"  seed: {sampling['seed']}")
        if sampling["sampling_mode"] == "v2":
            print(f"  local_radius: {sampling['local_radius']}")
    for name, spec in result["target_specs"].items():
        print(f"  target {name}: {spec['operator']} {spec['value']}")
    print(f"  jsonl: {result['artifacts']['jsonl']}")
    if result["artifacts"].get("csv") is not None:
        print(f"  csv: {result['artifacts']['csv']}")
    if result["artifacts"].get("manifest") is not None:
        print(f"  manifest: {result['artifacts']['manifest']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auto-configure and run an AL experiment")
    parser.add_argument("--circuit-type", required=True)
    parser.add_argument("--circuit-id", required=True)
    parser.add_argument("--config")
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="inspect Stage A/B AutoConfig without D0, SPICE, or LLM execution",
    )
    parser.add_argument(
        "--d0-only",
        action="store_true",
        help="initialize or inspect configured D0 without entering Active Learning",
    )
    args = parser.parse_args(argv)

    runtime = build_active_learning_circuit_config(
        args.circuit_type,
        args.circuit_id,
        repository_root=_REPOSITORY_ROOT,
    )
    _print_autoconfig_summary(runtime)
    if args.config_only:
        summary = runtime.summary()
        experiment = _inspect_experiment_config(
            args.circuit_type,
            args.circuit_id,
            args.config,
        )
        if experiment is not None:
            _print_experiment_config_summary(experiment)
            config = experiment["config"]
            summary = {
                **summary,
                "benchmark_version": config.get("benchmark_version"),
                "target_specs": experiment["target_specs"],
                "target_roles": deepcopy(config.get("target_roles")),
                "target_provenance": deepcopy(config.get("target_provenance")),
                "performance_target_selected": bool(
                    config.get("performance_target_selected", False)
                ),
                "experiment_config_path": str(experiment["config_path"]),
                "d0_mode": (config.get("data") or {}).get("d0_mode"),
                "resolved_d0_path": (
                    str(experiment["resolved_d0_path"])
                    if "resolved_d0_path" in experiment else None
                ),
            }
        return summary
    if args.d0_only:
        result = initialize_experiment_d0(
            args.circuit_type,
            args.circuit_id,
            config_path=args.config,
            runtime_config=runtime,
        )
        _print_d0_summary(result)
        return result

    result = run_configured_active_learning(
        args.circuit_type,
        args.circuit_id,
        config_path=args.config,
        runtime_config=runtime,
    )
    _print_summary(args.circuit_type, args.circuit_id, result)
    return result


if __name__ == "__main__":
    main()
