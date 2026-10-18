"""Bounded, evidence-producing CurrentMirror/circuit_0469 D0 entry point.

This is deliberately separate from the ordinary AL/D0 CLI.  It never calls AL,
Profiling, or an LLM.  Real execution requires the explicit --execute switch.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import hashlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
import traceback
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
CIRCUIT = ROOT / "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469"
FORMAL_CONFIG = CIRCUIT / "active_learning_config.yaml"
FIXED_COMMIT = "2c550afe40b95aa1f23e1b5a9f00567d5b18d5f1"
FROZEN_INPUT_SHA256 = {
    "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/active_learning_config.yaml":
        "612c73f32682a17cdce98fb488731ab88df1f782580c61d898b0ee4b7b956ac6",
    "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/parameter_template.yaml":
        "02b4b5c6e3553c49a1d89d1a7a2347c004edfa925fe30aa56a5e4caf8576ba3c",
    "dataset/circuit_0469/topology/circuit_0469.py":
        "e6862629fe5ff8cf9207af697bf6e9a12d69c7ecb436917710bcf5b72c388fb1",
    "ActiveLearning/circuit_types/CurrentMirror/performance_template.yaml":
        "e629cfdbefd0e1e116e5d7bbaa04710c9f5dfe1f3705336edeeacad9b52135f3",
}
PYSPICE_ORIGINAL_SHA256 = "206dcb289a12b115702dacfee57192b66e46c3cb9dc4b750dab79bcb43d7363e"
PYSPICE_PATCHED_SHA256 = "2eaa2ed62969b3919ed42739ff45009950255f41518539c9e8ce2e7f831c4d11"
ANCHOR = {
    "sample_id": "cm0469_template_nominal_v1",
    "source": "parameter_template_nominal",
    "params": {"IB1": -0.0001, "R0": 10000.0, "R1": 10000.0},
}
TARGETS = {
    "current_error": {"operator": "<=", "value": 0.04},
    "output_resistance": {"operator": ">=", "value": 26000000},
}
SAMPLING = {
    "sampling_mode": "v3", "n_samples": 20, "n_local": 10,
    "n_global": 9, "seed": 42, "local_factor": 1.25,
}
ASSETS = (
    "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/active_learning_config.yaml",
    "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/parameter_template.yaml",
    "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/parameters/ideal_design_space.yaml",
    "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/topology/circuit_0469.py",
    "dataset/circuit_0469/topology/circuit_0469.py",
    "ActiveLearning/circuit_types/CurrentMirror/performance_template.yaml",
    "ActiveLearning/InitializeD0/sampling/initial_sampler.py",
    "ActiveLearning/InitializeD0/d0_builder.py",
    "ActiveLearning/InitializeD0/evaluator/current_mirror_evaluator.py",
    "ActiveLearning/InitializeD0/validation/current_mirror_functional_validator.py",
    "ActiveLearning/InitializeD0/validation/excitation_perturbation_policy.py",
    "ActiveLearning/InitializeD0/validation/parameter_validator.py",
    "ActiveLearning/onboarding/auto_config.py",
    "ActiveLearning/onboarding/generic_adapter.py",
    "ActiveLearning/onboarding/runtime_provider.py",
    "ActiveLearning/d0_initialization.py",
    "ActiveLearning/cm0469_bounded_d0.py",
    "run/single_stage_amplifier/run_ideal_dataset_simulations.py",
)


class BoundedD0Error(RuntimeError):
    """An experiment must stop with incomplete provenance."""


_ACTIVE_CHILD_PID: int | None = None


def _terminate_for_global_deadline(_signum, _frame) -> None:
    if _ACTIVE_CHILD_PID is not None:
        try:
            os.killpg(_ACTIVE_CHILD_PID, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise BoundedD0Error("external overall deadline or termination signal")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _event(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time_ns": time.time_ns(), **value}, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise BoundedD0Error(f"analysis journal is missing: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _counts(events: list[dict[str, Any]]) -> dict[str, int]:
    attempts = [item for item in events if item.get("event") == "analysis_attempt"]
    finishes = [item for item in events if item.get("event") == "analysis_result"]
    kinds = [item["kind"] for item in attempts]
    if any(kind not in {"op", "dc"} for kind in kinds):
        raise BoundedD0Error("unsupported analysis type in journal")
    if len(finishes) > len(attempts):
        raise BoundedD0Error("analysis journal has excess results")
    seen = set()
    for item in finishes:
        number = item.get("number")
        if (not isinstance(number, int) or number < 1 or number > len(attempts)
                or number in seen or item.get("outcome") not in {"ok", "error"}):
            raise BoundedD0Error("analysis journal has malformed results")
        seen.add(number)
    if [item.get("number") for item in attempts] != list(range(1, len(attempts) + 1)):
        raise BoundedD0Error("analysis journal has malformed attempt sequence")
    return {
        "op_attempts": kinds.count("op"),
        "dc_attempts": kinds.count("dc"),
        "attempts": len(attempts),
        "failures": sum(item.get("outcome") == "error" for item in finishes),
        "unknown_outcomes": len(attempts) - len(finishes),
        "dc_sweep_points_requested": sum(
            item.get("points", 0) for item in attempts if item["kind"] == "dc"
        ),
    }


class AnalysisLedger:
    """Durable attempts at the PySpice execution boundary, before SPICE is called."""

    def __init__(self, path: Path, max_attempts: int, candidate_id: str):
        self.path = path
        self.max_attempts = max_attempts
        self.candidate_id = candidate_id
        self.attempts = 0

    def call(self, kind: str, points: int, callback, *args, **kwargs):
        if kind not in {"op", "dc"} or points < 1:
            raise BoundedD0Error("uncounted or malformed SPICE analysis")
        if self.attempts >= self.max_attempts:
            raise BoundedD0Error("SPICE analysis budget exhausted before call")
        number = self.attempts + 1
        _event(self.path, {
            "event": "analysis_attempt", "candidate_id": self.candidate_id,
            "number": number, "kind": kind, "points": points,
        })
        self.attempts = number
        try:
            result = callback(*args, **kwargs)
        except BaseException as error:
            _event(self.path, {
                "event": "analysis_result", "candidate_id": self.candidate_id,
                "number": number, "outcome": "error", "error": repr(error),
            })
            raise
        _event(self.path, {
            "event": "analysis_result", "candidate_id": self.candidate_id,
            "number": number, "outcome": "ok",
        })
        return result


def _dc_points(kwargs: dict[str, Any]) -> int:
    if len(kwargs) != 1 or not isinstance(next(iter(kwargs.values())), slice):
        raise BoundedD0Error("unexpected DC sweep arguments")
    sweep = next(iter(kwargs.values()))
    start, stop, step = float(sweep.start), float(sweep.stop), float(sweep.step)
    if step <= 0:
        raise BoundedD0Error("unexpected DC sweep step")
    points = round((stop - start) / step) + 1
    if points != 11:
        raise BoundedD0Error(f"unexpected DC sweep point count: {points}")
    return points


def install_spice_accounting(ledger: AnalysisLedger) -> None:
    """Reject any PySpice shared/subprocess analysis outside the counted path."""

    from PySpice.Spice.Simulation import CircuitSimulator
    from PySpice.Spice.NgSpice.Simulation import NgSpiceSharedCircuitSimulator
    from PySpice.Spice.NgSpice.Shared import NgSpiceShared

    original_base = CircuitSimulator._run
    original_shared = NgSpiceSharedCircuitSimulator._run
    original_low = NgSpiceShared.run
    active = {"value": False}

    def guarded_base(self, *args, **kwargs):
        if not active["value"]:
            raise BoundedD0Error("uncounted PySpice simulator backend")
        return original_base(self, *args, **kwargs)

    def guarded_low(self, *args, **kwargs):
        if not active["value"]:
            raise BoundedD0Error("uncounted ngspice shared run")
        return original_low(self, *args, **kwargs)

    def counted_shared(self, analysis_method, *args, **kwargs):
        if active["value"]:
            raise BoundedD0Error("nested SPICE analysis")
        if analysis_method not in {"operating_point", "dc"}:
            raise BoundedD0Error(f"unsupported SPICE analysis: {analysis_method}")
        kind = "op" if analysis_method == "operating_point" else "dc"
        points = 1 if kind == "op" else _dc_points(kwargs)

        def execute():
            active["value"] = True
            try:
                return original_shared(self, analysis_method, *args, **kwargs)
            finally:
                active["value"] = False

        return ledger.call(kind, points, execute)

    CircuitSimulator._run = guarded_base
    NgSpiceSharedCircuitSimulator._run = counted_shared
    NgSpiceShared.run = guarded_low


def _git_revision() -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return result.stdout.strip()


def _check_git_base() -> str:
    head = _git_revision()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT),
         "merge-base", "--is-ancestor", FIXED_COMMIT, head],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise BoundedD0Error("fixed B2A commit is not an ancestor of this checkout")
    return head


def _asset_hashes() -> dict[str, str]:
    tracked = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, check=True, timeout=20,
    ).stdout
    names = {os.fsdecode(item) for item in tracked.split(b"\0") if item}
    names.update(ASSETS)  # Include the new entry point before it is committed.
    return {name: sha256(ROOT / name) for name in sorted(names)}


def _check_design() -> None:
    import yaml

    config = yaml.safe_load(FORMAL_CONFIG.read_text(encoding="utf-8"))
    targets = config["target_specs"]
    if {name: {"operator": spec["operator"], "value": spec["value"]}
            for name, spec in targets.items()} != TARGETS:
        raise BoundedD0Error("formal CurrentMirror targets changed")
    template = yaml.safe_load((CIRCUIT / "parameter_template.yaml").read_text(encoding="utf-8"))
    from ActiveLearning.InitializeD0.validation.parameter_validator import validate_parameters
    if template["optimization_space"]["variables"] != ["IB1", "R0", "R1"]:
        raise BoundedD0Error("CurrentMirror optimization variables changed")
    if not validate_parameters(ANCHOR["params"], template)["parameter_valid"]:
        raise BoundedD0Error("frozen nominal anchor is invalid")
    nominals = {item["name"]: item["nominal_value"] for item in template["parameters"]
                if item["name"] in ANCHOR["params"]}
    if nominals != ANCHOR["params"]:
        raise BoundedD0Error("nominal anchor differs from parameter template")


def _environment() -> dict[str, Any]:
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise BoundedD0Error("requires reviewed Ubuntu Python 3.12 environment")
    head = _check_git_base()
    import PySpice
    import numpy

    if PySpice.__version__ != "1.5" or numpy.__version__ != "2.5.3":
        raise BoundedD0Error("unreviewed PySpice/NumPy environment")
    shared = Path(PySpice.__file__).parent / "Spice/NgSpice/Shared.py"
    if sha256(shared) != PYSPICE_PATCHED_SHA256:
        raise BoundedD0Error("PySpice stderr compatibility patch mismatch")
    dist = importlib.metadata.distribution("PySpice")
    entries = [item for item in dist.files or () if str(item).endswith("PySpice/Spice/NgSpice/Shared.py")]
    if len(entries) != 1 or entries[0].hash is None or entries[0].hash.mode != "sha256":
        raise BoundedD0Error("PySpice original-wheel hash is unavailable")
    encoded = entries[0].hash.value
    original = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
    if original != PYSPICE_ORIGINAL_SHA256:
        raise BoundedD0Error("PySpice original-wheel identity mismatch")
    version = subprocess.run(["ngspice", "--version"], capture_output=True,
                             text=True, check=True, timeout=10).stdout
    if "ngspice-42" not in version:
        raise BoundedD0Error("unreviewed ngspice executable version")
    library = ctypes.util.find_library("ngspice")
    if not library:
        raise BoundedD0Error("ngspice shared library is unavailable")
    ctypes.CDLL(library)
    linker = subprocess.run(["ldconfig", "-p"], capture_output=True,
                            text=True, check=True, timeout=10).stdout
    library_paths = [Path(line.split("=>", 1)[1].strip()) for line in linker.splitlines()
                     if "libngspice.so.0 " in line and "=>" in line]
    if len(library_paths) != 1 or not library_paths[0].is_file():
        raise BoundedD0Error("cannot identify the loaded ngspice shared library file")
    executable = shutil.which("ngspice")
    if executable is None:
        raise BoundedD0Error("ngspice executable is unavailable")
    return {
        "python": sys.version, "git_commit": head, "fixed_base_commit": FIXED_COMMIT,
        "pyspice": PySpice.__version__,
        "numpy": numpy.__version__, "pyyaml": importlib.metadata.version("PyYAML"),
        "ngspice": version.strip(), "ngspice_executable": executable,
        "ngspice_executable_sha256": sha256(Path(executable)),
        "ngspice_library": str(library_paths[0]),
        "ngspice_library_sha256": sha256(library_paths[0]),
        "shared_py_path": str(shared),
        "shared_py_original_sha256": original,
        "shared_py_patched_sha256": sha256(shared),
    }


def _verify_assets(expected: dict[str, str]) -> None:
    if _asset_hashes() != expected:
        raise BoundedD0Error("input asset hash changed during execution")


def _verify_frozen_inputs(actual: dict[str, str]) -> None:
    for name, expected in FROZEN_INPUT_SHA256.items():
        if actual.get(name) != expected:
            raise BoundedD0Error(f"frozen input differs from Ubuntu B2A baseline: {name}")


def _worker(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if (not request.get("worker_nonce") or
            request["worker_nonce"] != os.environ.get("CM0469_BOUNDED_WORKER_NONCE")):
        raise BoundedD0Error("candidate worker requires its parent process")
    if (request.get("mode") not in {"preflight", "d0"}
            or type(request.get("max_attempts")) is not int
            or not 1 <= request["max_attempts"] <= (2 if request["mode"] == "preflight" else 8)):
        raise BoundedD0Error("invalid candidate analysis limit")
    event_path = Path(request["event_path"])
    result_path = Path(request["result_path"])
    event_path.touch(exist_ok=False)
    try:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        _verify_assets(request["asset_sha256"])
        _check_design()
        _environment()
        ledger = AnalysisLedger(event_path, request["max_attempts"], request["sample"]["sample_id"])
        install_spice_accounting(ledger)
        from ActiveLearning.onboarding import build_active_learning_circuit_config
        runtime = build_active_learning_circuit_config("CurrentMirror", "circuit_0469", repository_root=ROOT)
        sample = request["sample"]
        started = time.monotonic()
        if request["mode"] == "preflight":
            from ActiveLearning.InitializeD0.validation.parameter_validator import validate_parameters
            if not validate_parameters(sample["params"], runtime.param_template)["parameter_valid"]:
                raise BoundedD0Error("nominal preflight parameter validation failed")
            current_error, resistance, valid = runtime.create_evaluator()(sample["params"])
            if (not valid or current_error is None or resistance is None
                    or not math.isfinite(float(current_error))
                    or not math.isfinite(float(resistance)) or float(resistance) <= 0):
                raise BoundedD0Error("nominal evaluator returned invalid simulation")
            record = {"params": sample["params"], "metrics": {
                "current_error": current_error, "output_resistance": resistance,
            }, "simulation_valid": True}
        else:
            from ActiveLearning.InitializeD0.d0_builder import build_runtime_d0
            from ActiveLearning.d0_initialization import _functional_config
            config = {"data": {"d0_mode": "independent"}}
            rows = build_runtime_d0(runtime, functional_config=_functional_config(config["data"], runtime.adapter),
                                    target_specs=TARGETS, prepared_samples=[sample])
            if len(rows) != 1:
                raise BoundedD0Error("candidate evaluation returned wrong row count")
            record = rows[0]
            status = record["status"]
            if status["parameter_valid"] and status["simulation_valid"] is not True:
                raise BoundedD0Error("simulation invalid: infrastructure/physics ambiguity")
            if status["parameter_valid"] and record.get("functional_evaluation_status") == "not_evaluable":
                raise BoundedD0Error("functional evaluation is not reliable")
            ambiguous = {"simulation_setup_failed", "nominal_current_data_invalid",
                         "compliance_data_invalid", "reference_response_data_invalid",
                         "reference_stimulus_unavailable"}
            if ambiguous.intersection(record.get("failure_reasons", [])):
                raise BoundedD0Error("functional evaluation has ambiguous infrastructure failure")
        events = _read_events(event_path)
        counts = _counts(events)
        if counts["unknown_outcomes"] or counts["failures"]:
            raise BoundedD0Error("SPICE analysis failure or unknown outcome")
        if request["mode"] == "preflight" and (counts["op_attempts"], counts["dc_attempts"]) != (1, 1):
            raise BoundedD0Error("nominal preflight did not execute exactly one OP and DC")
        if (request["mode"] == "d0" and record["status"]["parameter_valid"]
                and (counts["op_attempts"], counts["dc_attempts"]) != (6, 2)):
            raise BoundedD0Error("valid D0 candidate did not execute exactly six OP and two DC")
        if counts["attempts"] > request["max_attempts"]:
            raise BoundedD0Error("candidate analysis budget exceeded")
        _verify_assets(request["asset_sha256"])
        _json_atomic(result_path, {"record": record, "counts": counts,
                                   "elapsed_seconds": time.monotonic() - started})
        return 0
    except BaseException as error:
        _event(event_path, {"event": "worker_error", "error": repr(error)})
        traceback.print_exc(file=sys.stderr)
        return 1


def _run_child(request_path: Path, log_path: Path, timeout_seconds: float,
               *, command: list[str] | None = None) -> tuple[int, bool]:
    global _ACTIVE_CHILD_PID
    if command is None:
        command = [sys.executable, "-B", "-m", "ActiveLearning.cm0469_bounded_d0", "_worker", str(request_path)]
        nonce = json.loads(request_path.read_text(encoding="utf-8"))["worker_nonce"]
        options_env = {**os.environ, "CM0469_BOUNDED_WORKER_NONCE": nonce}
    else:
        options_env = dict(os.environ)
    options: dict[str, Any] = {"cwd": ROOT, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                               "text": True, "env": options_env}
    if os.name == "posix":
        options["start_new_session"] = True
    else:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, **options)
    _ACTIVE_CHILD_PID = process.pid
    timed_out = False
    try:
        try:
            output, _ = process.communicate(timeout=max(0.001, timeout_seconds))
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            output, _ = process.communicate(timeout=10)
        if os.name == "posix" and not timed_out:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                os.killpg(process.pid, signal.SIGKILL)
                timed_out = True  # A surviving descendant makes the outcome uncertain.
    finally:
        if process.poll() is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.communicate(timeout=10)
        _ACTIVE_CHILD_PID = None
    with log_path.open("x", encoding="utf-8") as stream:
        stream.write(output or "")
        stream.flush()
        os.fsync(stream.fileno())
    return process.returncode, timed_out


def _samples(mode: str) -> list[dict[str, Any]]:
    if mode == "preflight":
        return [{"sample_id": ANCHOR["sample_id"], "sampling_source": "nominal",
                 "params": dict(ANCHOR["params"])}]
    import yaml
    from ActiveLearning.InitializeD0.sampling import generate_v3_physical_local_global_samples

    template = yaml.safe_load((CIRCUIT / "parameter_template.yaml").read_text(encoding="utf-8"))
    samples = generate_v3_physical_local_global_samples(
        template, n_local=10, n_global=9, seed=42, local_factor=1.25,
        center_params=ANCHOR["params"],
    )
    if len(samples) != 20:
        raise BoundedD0Error("V3 sampler returned unexpected candidate count")
    samples[0].update(anchor_sample_id=ANCHOR["sample_id"], anchor_source=ANCHOR["source"])
    return samples


def _new_config(output_dir: Path) -> Path:
    import yaml
    config = yaml.safe_load(FORMAL_CONFIG.read_text(encoding="utf-8"))
    config["data"] = {
        "d0_mode": "independent", "d0_path_relative_to": "config",
        "d0_path": "d0/d0_v1_seed42.jsonl", "initialize_if_missing": True,
        "initial_anchor": ANCHOR, **SAMPLING,
    }
    path = output_dir / "active_learning_config.yaml"
    with path.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(config, stream, sort_keys=False)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def execute(mode: str, output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir == ROOT or output_dir.is_relative_to(ROOT):
        raise BoundedD0Error("versioned output must be outside the frozen Git checkout")
    if output_dir.exists():
        raise FileExistsError(f"refusing existing output directory: {output_dir}")
    _check_design()
    environment = _environment()
    before = _asset_hashes()
    _verify_frozen_inputs(before)
    samples = _samples(mode)
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = output_dir / "run_manifest.json"
    event_log = output_dir / "run_events.jsonl"
    event_log.touch(exist_ok=False)
    manifest: dict[str, Any] = {
        "status": "incomplete", "mode": mode, "git_commit": environment["git_commit"],
        "anchor": ANCHOR, "sampling": SAMPLING if mode == "d0" else None,
        "target_specs": TARGETS, "environment": environment,
        "asset_sha256_before": before, "config_path": None,
        "config_sha256": None,
        "limits": {"preflight_analyses": 2, "d0_analyses": 160,
                   "candidate_seconds": 180, "total_seconds": 3600},
        "candidates": [], "analysis_counts": {"op_attempts": 0, "dc_attempts": 0,
                                                  "attempts": 0, "failures": 0,
                                                  "unknown_outcomes": 0,
                                                  "dc_sweep_points_requested": 0},
        "stop_reason": "not_started",
    }
    _json_atomic(manifest_path, manifest)
    started = time.monotonic()
    records = []
    previous_sigterm = None
    if os.name == "posix":
        previous_sigterm = signal.signal(signal.SIGTERM, _terminate_for_global_deadline)
    try:
        config_path = _new_config(output_dir) if mode == "d0" else None
        manifest["config_path"] = str(config_path) if config_path else None
        manifest["config_sha256"] = sha256(config_path) if config_path else None
        _json_atomic(manifest_path, manifest)
        for sample in samples:
            _verify_assets(before)
            if config_path and sha256(config_path) != manifest["config_sha256"]:
                raise BoundedD0Error("new experiment config changed")
            elapsed = time.monotonic() - started
            remaining_seconds = (180 if mode == "preflight" else 3600) - elapsed
            remaining_attempts = (2 if mode == "preflight" else 160) - manifest["analysis_counts"]["attempts"]
            if remaining_seconds <= 0 or remaining_attempts <= 0:
                raise BoundedD0Error("overall time or analysis budget exhausted")
            candidate_id = sample["sample_id"]
            candidate_dir = output_dir / candidate_id
            candidate_dir.mkdir(exist_ok=False)
            request_path = candidate_dir / "request.json"
            result_path = candidate_dir / "result.json"
            analysis_path = candidate_dir / "analyses.jsonl"
            _json_atomic(request_path, {
                "mode": mode, "sample": sample, "asset_sha256": before,
                "max_attempts": min(8 if mode == "d0" else 2, remaining_attempts),
                "worker_nonce": secrets.token_hex(32),
                "event_path": str(analysis_path), "result_path": str(result_path),
            })
            _event(event_log, {"event": "candidate_start", "candidate_id": candidate_id})
            candidate = {"sample_id": candidate_id, "status": "running",
                         "journal": str(analysis_path),
                         "worker_log": str(candidate_dir / "worker.log")}
            manifest["candidates"].append(candidate)
            manifest["stop_reason"] = "candidate_incomplete"
            _json_atomic(manifest_path, manifest)
            began = time.monotonic()
            returncode, timed_out = _run_child(request_path, candidate_dir / "worker.log",
                                                min(180, remaining_seconds))
            events = _read_events(analysis_path)
            counts = _counts(events)
            for key, value in counts.items():
                manifest["analysis_counts"][key] += value
            if manifest["analysis_counts"]["attempts"] > (2 if mode == "preflight" else 160):
                raise BoundedD0Error("total SPICE analysis budget exceeded")
            candidate.update({"status": "returned", "elapsed_seconds": time.monotonic() - began,
                              "exit_code": returncode, "timed_out": timed_out,
                              "analysis_counts": counts})
            _json_atomic(manifest_path, manifest)
            _event(event_log, {"event": "candidate_end", **candidate})
            if timed_out or returncode != 0 or counts["unknown_outcomes"] or counts["failures"]:
                raise BoundedD0Error(f"candidate {candidate_id} failed or timed out")
            if not result_path.is_file():
                raise BoundedD0Error(f"candidate {candidate_id} result is missing")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result["counts"] != counts:
                raise BoundedD0Error(f"candidate {candidate_id} count mismatch")
            candidate["status"] = result["record"].get("status", {"simulation_valid": True})
            candidate["metrics"] = result["record"].get("metrics", {})
            candidate["failure_reasons"] = result["record"].get("failure_reasons", [])
            candidate["functional_evaluation_status"] = result["record"].get("functional_evaluation_status")
            _json_atomic(manifest_path, manifest)
            records.append(result["record"])
            _verify_assets(before)
        if mode == "d0":
            from ActiveLearning.InitializeD0.d0_builder import save_d0_dataset
            artifacts = save_d0_dataset(records, output_dir / "d0", "d0_v1_seed42")
            independent = {
                "d0_mode": "independent", "anchor_source": ANCHOR["source"],
                "anchor_sample_id": ANCHOR["sample_id"], "seed": 42,
                "sampling": SAMPLING, "records": len(records),
                "generated_config": "../active_learning_config.yaml",
                "config_sha256": manifest["config_sha256"],
                "artifacts": {name: path.name for name, path in artifacts.items()},
                "analysis_counts": manifest["analysis_counts"],
                "asset_sha256_before": before,
            }
            independent_path = output_dir / "d0/d0_v1_seed42_initialization.json"
            _json_atomic(independent_path, independent)
            manifest["artifacts"] = {
                **{name: {"path": str(path), "sha256": sha256(path)} for name, path in artifacts.items()},
                "independent_manifest": {"path": str(independent_path), "sha256": sha256(independent_path)},
            }
        else:
            manifest["preflight"] = records[0]
        manifest["asset_sha256_after"] = _asset_hashes()
        _verify_assets(before)
        manifest["elapsed_seconds"] = time.monotonic() - started
        if manifest["elapsed_seconds"] > (180 if mode == "preflight" else 3600):
            raise BoundedD0Error("overall wall-clock limit exceeded")
        _event(event_log, {"event": "run_complete"})
        manifest["stop_reason"] = "completed"
        manifest["status"] = "complete"
        _json_atomic(manifest_path, manifest)
        return manifest
    except BaseException as error:
        manifest["status"] = "incomplete"
        manifest["stop_reason"] = repr(error)
        manifest["error_traceback"] = traceback.format_exc()
        manifest["elapsed_seconds"] = time.monotonic() - started
        try:
            manifest["asset_sha256_after"] = _asset_hashes()
            _json_atomic(manifest_path, manifest)
            _event(event_log, {"event": "run_incomplete", "reason": repr(error)})
        except BaseException as persistence_error:
            raise BoundedD0Error(f"run stopped and evidence persistence failed: {persistence_error!r}") from error
        raise
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "d0", "_worker"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("request", nargs="?", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "_worker":
        if args.request is None:
            parser.error("worker request is required")
        return _worker(args.request)
    if not args.execute or args.output_dir is None or args.request is not None:
        parser.error("real execution requires --execute and --output-dir")
    execute(args.mode, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
