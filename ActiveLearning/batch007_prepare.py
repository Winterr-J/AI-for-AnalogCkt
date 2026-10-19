"""Fixed Batch 007 OpAmp preparation using existing AutoTarget and D0 CLIs.

Dry-run is read-only. Real preparation requires explicit flags and runs each
circuit in isolated bounded subprocesses; it never launches AL or V2 production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import signal
import subprocess
import sys

import yaml

from ActiveLearning.InitializeD0.validation.parameter_validator import validate_parameters
from ActiveLearning.onboarding import build_active_learning_circuit_config

ROOT = Path(__file__).resolve().parents[1]
IDS = tuple(f"circuit_{number}" for number in range(1621, 1631))
BUNDLE = (
    "topology/{id}.py", "topology/{id}.cir",
    "simulation/parameter/ideal_param.yaml",
    "parameters/ideal_design_space.yaml",
    "parameters/operating_space.yaml",
    "parameters/tunable_parameters.yaml",
    "simulation/results/ideal/op_results.yaml", "metadata.txt",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_legacy(raw: str, circuit_id: str) -> Path:
    parts = PureWindowsPath(raw).parts
    positions = [index for index, value in enumerate(parts) if value.casefold() == "dataset"]
    if len(positions) != 1:
        raise ValueError("legacy asset is not inside dataset")
    relative = Path(*parts[positions[0] + 1:])
    if not relative.parts or relative.parts[0] != circuit_id or ".." in relative.parts:
        raise ValueError("legacy asset escaped the selected circuit")
    return relative


def load_fixed_candidates(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or [row.get("circuit_id") for row in rows] != list(IDS):
        raise ValueError("Batch 007 circuit IDs or order differ from the fixed manifest")
    for row in rows:
        if (row.get("requested_circuit_type"), row.get("inferred_circuit_type"),
                row.get("static_status"), row.get("deployment_eligible")) != (
                "OpAmp", "OpAmp", "STATIC_READY", True):
            raise ValueError("Batch 007 candidate identity/status differs from approval")
    return rows


def inspect_candidate(row: dict, source_dataset: Path,
                      output_root: Path) -> dict:
    circuit_id = row["circuit_id"]
    result = {"circuit_id": circuit_id, "circuit_type": "OpAmp",
              "status": "BLOCKED", "missing": [], "asset_hashes": {}}
    try:
        expected = {item.format(id=circuit_id) for item in BUNDLE}
        reported = {_relative_legacy(value, circuit_id).relative_to(circuit_id).as_posix()
                    for value in row["auditable_bundle_paths"]}
        required = {_relative_legacy(value, circuit_id).relative_to(circuit_id).as_posix()
                    for value in row["runtime_required_paths"]}
        if reported != expected or required != {
                f"topology/{circuit_id}.py", "simulation/parameter/ideal_param.yaml"}:
            raise ValueError("legacy asset bundle differs from the fixed eight-file contract")
        for relative in sorted(expected):
            source = source_dataset / circuit_id / relative
            destination = ROOT / "dataset" / circuit_id / relative
            if not source.is_file() or not destination.is_file():
                result["missing"].append(relative)
                continue
            if source.is_symlink() or destination.is_symlink() or _sha(source) != _sha(destination):
                raise ValueError(f"source byte identity mismatch: {relative}")
            result["asset_hashes"][relative] = _sha(destination)
        if result["missing"]:
            result["reason"] = "required circuit assets missing"
            return result
        runtime = build_active_learning_circuit_config("OpAmp", circuit_id)
        nominal = {item.name: float(item.nominal) for item in runtime.runtime_parameters}
        validation = validate_parameters(nominal, runtime.param_template)
        if not validation["parameter_valid"] or runtime.unresolved:
            raise ValueError("AutoConfig nominal or parameter binding invalid")
        result["autoconfig"] = {"parameter_count": len(runtime.runtime_parameters),
                                "nominal_valid": True}
        result["evaluator"] = type(runtime.create_evaluator()).__name__
        result["functional_validator"] = type(runtime.create_functional_validator()).__name__
        circuit_output = output_root / circuit_id
        receipt = circuit_output / "result.json"
        if circuit_output.exists():
            if circuit_output.is_symlink() or not receipt.is_file():
                raise ValueError("existing preparation output needs manual review")
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            if (saved.get("circuit_id") != circuit_id or
                    saved.get("source_sha256") != result["asset_hashes"] or
                    saved.get("status") != "PREPARATION_READY"):
                raise ValueError("existing preparation identity differs")
            for name, digest in saved.get("artifacts", {}).items():
                artifact = circuit_output / name
                if (not artifact.is_file() or artifact.is_symlink() or
                        not artifact.resolve().is_relative_to(circuit_output.resolve()) or
                        _sha(artifact) != digest):
                    raise ValueError("existing preparation artifact differs")
            if len(saved.get("artifacts", {})) != 4:
                raise ValueError("existing preparation receipt is incomplete")
            if (saved.get("records") != 20 or saved.get("seed") != 42
                    or saved.get("sampling_mode") != "v4"
                    or saved.get("functional_valid_count", 0) < 1):
                raise ValueError("existing preparation has no verified valid D0")
            config_names = [name for name in saved["artifacts"]
                            if name.endswith("/active_learning_config.yaml")]
            if len(config_names) != 1:
                raise ValueError("existing preparation configuration is ambiguous")
            config = yaml.safe_load((circuit_output / config_names[0]).read_text(encoding="utf-8"))
            data = config.get("data") or {}
            d0 = (circuit_output / config_names[0]).parent / str(data.get("d0_path", ""))
            if (data.get("d0_mode") != "independent"
                    or data.get("seed") != 42
                    or data.get("n_samples") != 20
                    or data.get("sampling_mode") != "v4"
                    or data.get("n_local") != 10
                    or data.get("n_global") != 9
                    or data.get("local_radius") != saved.get("local_radius")
                    or not config.get("target_specs")
                    or not d0.is_file() or not d0.resolve().is_relative_to(circuit_output.resolve())
                    or d0.relative_to(circuit_output).as_posix() not in saved["artifacts"]):
                raise ValueError("existing D0 binding differs from the preparation receipt")
            records = [json.loads(line) for line in d0.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
            valid_count = sum(item.get("status", {}).get("functional_valid") is True
                              for item in records)
            initialization = json.loads(
                d0.with_name(d0.stem + "_initialization.json").read_text(encoding="utf-8"))
            if (len(records) != 20 or valid_count < 1
                    or valid_count != saved["functional_valid_count"]
                    or initialization.get("d0_mode") != "independent"
                    or initialization.get("seed") != 42
                    or initialization.get("records") != 20
                    or (initialization.get("sampling") or {}).get("sampling_mode") != "v4"):
                raise ValueError("existing independent D0 provenance or counts differ")
            result["status"] = "PREPARATION_READY"
            result["reused"] = True
            return result
        result["status"] = "TARGET_PREPARATION_REQUIRED"
        result["missing"].extend(["circuit-specific performance target",
                                  "20-row independent D0"])
        return result
    except Exception as error:
        result["reason"] = f"{type(error).__name__}: {error}"
        return result


def _bounded(command: list[str], timeout: int, log_path: Path) -> dict:
    process = subprocess.Popen(command, cwd=ROOT, start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        log_path.write_text(
            f"exit_code={process.returncode} timed_out=true\nSTDOUT\n{stdout}\nSTDERR\n{stderr}\n",
            encoding="utf-8")
        raise TimeoutError("preparation subprocess exceeded its approved stage timeout")
    log_path.write_text(
        f"exit_code={process.returncode} timed_out=false\nSTDOUT\n{stdout}\nSTDERR\n{stderr}\n",
        encoding="utf-8")
    if process.returncode != 0:
        raise RuntimeError(f"preparation subprocess failed with exit code {process.returncode}; see {log_path}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"stdout": stdout.strip(), "stderr_length": len(stderr)}


def prepare_candidate(row: dict, source_dataset: Path, output_root: Path,
                      args: argparse.Namespace) -> dict:
    inspected = inspect_candidate(row, source_dataset, output_root)
    if inspected["status"] in {"BLOCKED", "PREPARATION_READY"}:
        return inspected
    circuit_id = row["circuit_id"]
    circuit_output = output_root / circuit_id
    try:
        circuit_output.mkdir(parents=True, exist_ok=False)
        target = _bounded([
            sys.executable, "-m", "ActiveLearning.auto_target",
            "--circuit-type", "OpAmp", "--circuit-id", circuit_id,
            "--output-root", str(circuit_output), "--seed", str(args.seed),
            "--max-anchor-evaluations", str(args.max_anchor_evaluations),
            "--profile-samples", str(args.profile_samples),
            "--max-profile-evaluations", str(args.profile_samples),
            "--additional-samples", "0", "--max-llm-calls", str(args.max_llm_calls),
            "--provider", args.provider, "--model", args.model,
            "--d0-mode", "independent", "--allow-spice", "--allow-llm",
        ], args.target_timeout_seconds, circuit_output / "auto_target.log")
        if target.get("status") != "READY_FOR_D0":
            raise RuntimeError("AutoTarget did not produce validated per-circuit targets")
        config = Path(target["artifact_paths"]["config"]).resolve(strict=True)
        if not config.is_relative_to(circuit_output.resolve()) or config.is_symlink():
            raise ValueError("generated configuration escaped circuit output")
        payload = yaml.safe_load(config.read_text(encoding="utf-8"))
        data = payload["data"]
        data.update({"sampling_mode": "v4", "local_radius": args.d0_local_radius,
                     "d0_path": f"d0/d0_v4_seed{args.seed}.jsonl",
                     "n_samples": 20, "n_local": 10, "n_global": 9, "seed": args.seed})
        data.pop("local_factor", None)
        config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        _bounded([
            sys.executable, "-m", "ActiveLearning.main",
            "--circuit-type", "OpAmp", "--circuit-id", circuit_id,
            "--config", str(config), "--d0-only",
        ], args.d0_timeout_seconds, circuit_output / "d0.log")
        d0 = config.parent / data["d0_path"]
        artifacts = [config, d0, d0.with_suffix(".csv"),
                     d0.with_name(d0.stem + "_initialization.json")]
        if any(not path.is_file() for path in artifacts):
            raise ValueError("independent D0 artifacts incomplete")
        records = [json.loads(line) for line in d0.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        if len(records) != 20 or any(not isinstance(item.get("status"), dict)
                                      for item in records):
            raise ValueError("independent D0 does not contain 20 structured records")
        simulation_valid = sum(item["status"].get("simulation_valid") is True
                               for item in records)
        functional_valid = sum(item["status"].get("functional_valid") is True
                               for item in records)
        if functional_valid < 1:
            raise ValueError("independent D0 has no functional-valid real sample")
        receipt = {"circuit_id": circuit_id, "status": "PREPARATION_READY",
                   "source_sha256": inspected["asset_hashes"],
                   "artifacts": {path.relative_to(circuit_output).as_posix(): _sha(path)
                                 for path in artifacts},
                   "records": len(records), "seed": args.seed,
                   "sampling_mode": "v4", "local_radius": args.d0_local_radius,
                   "simulation_valid_count": simulation_valid,
                   "functional_valid_count": functional_valid}
        (circuit_output / "result.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {**inspected, "status": "PREPARATION_READY", "missing": [],
                "records": len(records)}
    except Exception as error:
        return {**inspected, "status": "BLOCKED",
                "reason": f"{type(error).__name__}: {error}",
                "output_directory": str(circuit_output)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fixed Batch 007 onboarding")
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--source-dataset-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--execute-preparation", action="store_true")
    parser.add_argument("--allow-spice", action="store_true")
    parser.add_argument("--allow-llm", action="store_true")
    parser.add_argument("--provider", choices=("gemini", "openai"))
    parser.add_argument("--model")
    parser.add_argument("--max-anchor-evaluations", type=int, default=12)
    parser.add_argument("--profile-samples", type=int, default=25)
    parser.add_argument("--max-llm-calls", type=int, default=3)
    parser.add_argument("--d0-local-radius", type=float)
    parser.add_argument("--target-timeout-seconds", type=int)
    parser.add_argument("--d0-timeout-seconds", type=int)
    args = parser.parse_args(argv)
    if args.seed != 42:
        parser.error("Batch 007 seed is fixed at 42")
    if not args.execute_preparation and (args.allow_spice or args.allow_llm):
        parser.error("dry-run cannot enable external work")
    if args.execute_preparation and (
            not args.allow_spice or not args.allow_llm or not args.provider or
            not args.model or args.max_anchor_evaluations < 1 or
            args.profile_samples < 2 or args.max_llm_calls < 1 or
            args.d0_local_radius is None or not 0 < args.d0_local_radius <= 1 or
            not args.target_timeout_seconds or args.target_timeout_seconds < 1 or
            not args.d0_timeout_seconds or args.d0_timeout_seconds < 1):
        parser.error("real preparation requires explicit provider, radius, budgets and timeouts")
    if args.output_root.resolve().is_relative_to(ROOT):
        parser.error("preparation output must be outside the Git checkout")
    rows = load_fixed_candidates(args.candidate_manifest)
    output_root = args.output_root.resolve()
    source_dataset = args.source_dataset_root.resolve(strict=True)
    results = [prepare_candidate(row, source_dataset, output_root, args)
               if args.execute_preparation else inspect_candidate(row, source_dataset, output_root)
               for row in rows]
    print(json.dumps({"batch": "007", "mode": "execute" if args.execute_preparation else "dry_run",
                      "results": results}, indent=2, ensure_ascii=False))
    return 0 if all(item["status"] != "BLOCKED" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
