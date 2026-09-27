"""Generic command-line entry point for Active Learning experiments."""

import argparse
import json
from pathlib import Path

import yaml

from ActiveLearning.active_learning import run_active_learning
from ActiveLearning.InitializeD0.examples.CurrentMirror.circuit_0469.evaluator_adapter import (
    Circuit0469CurrentMirrorAdapter,
)


_ROOT = Path(__file__).resolve().parent
_EXAMPLES_ROOT = _ROOT / "InitializeD0" / "examples"

ADAPTER_BUILDERS = {
    ("CurrentMirror", "circuit_0469"): Circuit0469CurrentMirrorAdapter,
}


def _load_yaml(path):
    with Path(path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _load_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _resolve_paths(circuit_type, circuit_id, config_path=None):
    key = (circuit_type, circuit_id)
    if key not in ADAPTER_BUILDERS:
        raise ValueError(
            f"Unsupported Active Learning circuit: {circuit_type}/{circuit_id}"
        )

    circuit_dir = _EXAMPLES_ROOT / circuit_type / circuit_id
    return {
        "circuit_dir": circuit_dir,
        "parameter_template": circuit_dir / "parameter_template.yaml",
        "performance_template": (
            _ROOT / "circuit_types" / circuit_type / "performance_template.yaml"
        ),
        "config": (
            Path(config_path).resolve()
            if config_path is not None
            else circuit_dir / "active_learning_config.yaml"
        ),
    }


def _load_adapter(circuit_type, circuit_id):
    try:
        builder = ADAPTER_BUILDERS[(circuit_type, circuit_id)]
    except KeyError as error:
        raise ValueError(
            f"Unsupported Active Learning circuit: {circuit_type}/{circuit_id}"
        ) from error
    return builder()


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


def run_configured_active_learning(
    circuit_type,
    circuit_id,
    *,
    config_path=None,
    llm_client=None,
):
    """Load one registered circuit and delegate to the existing AL loop."""
    paths = _resolve_paths(circuit_type, circuit_id, config_path)
    parameter_template = _load_yaml(paths["parameter_template"])
    performance_template = _load_yaml(paths["performance_template"])
    config = _load_yaml(paths["config"])
    target_specs = _configured_targets(config)

    circuit_dir = paths["circuit_dir"]
    records = _load_jsonl(
        _relative_to_circuit(config["data"]["d0_path"], circuit_dir)
    )
    output_value = config.get("output", {}).get("output_dir")
    output_dir = (
        _relative_to_circuit(output_value, circuit_dir)
        if output_value is not None
        else None
    )
    circuit_info = dict(parameter_template["circuit"])
    circuit_info.update(config.get("circuit_info", {}))
    settings = config.get("active_learning", {})

    return run_active_learning(
        records=records,
        circuit_info=circuit_info,
        parameter_template=parameter_template,
        performance_template=performance_template,
        target_specs=target_specs,
        adapter=_load_adapter(circuit_type, circuit_id),
        max_iterations=settings.get("max_iterations", 10),
        beta=settings.get("beta", 0.7),
        context_k=settings.get("context_k", 5),
        retrieval_k=settings.get("retrieval_k", 3),
        committee_size=settings.get("committee_size", 5),
        data_expansion_seed=settings.get("data_expansion_seed", 42),
        output_dir=output_dir,
        llm_client=llm_client,
    )


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a registered AL experiment")
    parser.add_argument("--circuit-type", required=True)
    parser.add_argument("--circuit-id", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)

    result = run_configured_active_learning(
        args.circuit_type,
        args.circuit_id,
        config_path=args.config,
    )
    _print_summary(args.circuit_type, args.circuit_id, result)
    return result


if __name__ == "__main__":
    main()
