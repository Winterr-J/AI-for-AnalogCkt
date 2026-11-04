"""Guarded circuit_0259 transient canary. Import and default CLI are offline only.

The native child is never imported here. Real execution needs an independent
operator approval, an explicit flag, a durable one-attempt ledger and a child
process. No D0, AL or production evaluator is involved.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ActiveLearning.sample_hold_offline import (
    CircuitMapping, OperatingConditions, SampleHoldFixture, build_fixture,
    evaluate_waveforms,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "ActiveLearning/docs/reference_audit/SC_0259_TRANSIENT_FIXTURE_V1_SC4B2.json"
SCHEMA = "sc_0259_guarded_transient_v1"
SCHEMA_SC4B3A = "sc_0259_guarded_transient_v1_sc4b3a"
SCHEMA_SC4B3C = "sc_0259_guarded_transient_v1_sc4b3c"
SOURCE_CLOSURE_PATH = (
    "ActiveLearning/docs/reference_audit/SC_0259_CANARY_SOURCE_CLOSURE_SC4B3A.json")
SOURCE_CLOSURE_PATH_SC4B3C = (
    "ActiveLearning/docs/reference_audit/SC_0259_CANARY_SOURCE_CLOSURE_SC4B3C.json")
REQUIRED_CLOSURE_SOURCES = frozenset({
    "topology", "converted_pyspice", "ideal_parameter_archive", "archived_runner",
    "offline_evaluator", "guarded_runner", "isolated_child", "windows_job_isolation",
    "active_learning_package", "run_package", "single_stage_package",
    "ngspice_configuration_module", "conversion_package", "cadence_conversion_module",
    "simulation_metrics_package", "simulation_metrics_ac", "simulation_metrics_dc",
    "simulation_metrics_loop_gain", "simulation_metrics_op", "simulation_metrics_transient",
})
FIXED_OUTPUT_ROOT = Path("D:/analogCkt/SC_0259_canary_runs")
EXPECTED_INPUT_ASSETS = {
    "topology": ("dataset/circuit_0259/topology/circuit_0259.cir",
                 "7bbd46660ee48f8c988585a02b198a2cb0a13368e5856a0cf5a8fe4ca166f130"),
    "converted_pyspice": ("dataset/circuit_0259/topology/circuit_0259.py",
                          "b859a176da95954676425bbeb37de6c698dfb3229f49697ac9bbf1d1ea6e530f"),
    "ideal_parameter_archive": ("dataset/circuit_0259/simulation/parameter/ideal_param.yaml",
                                "2438e232294721cf38f94340efb1fe11e03e2193a6eeee650524eff20b82bfc6"),
}


class CanaryError(RuntimeError):
    pass


class UncertainProcessError(CanaryError):
    pass


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_ledger(path: Path, event: dict) -> None:
    line = (json.dumps(event, sort_keys=True, allow_nan=False) + "\n").encode()
    with path.open("ab") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def _native_library_status() -> dict:
    override = os.environ.get("NGSPICE_LIBRARY_PATH")
    default = ROOT / "Batch_simulation/vendor/Spice64_dll/dll-vs/ngspice.dll"
    selected = Path(override) if override else default
    absolute_verified = selected.is_absolute() and selected.is_file()
    try:
        pyspice_version = importlib.metadata.version("PySpice")
    except importlib.metadata.PackageNotFoundError:
        pyspice_version = None
    return {"library_path": str(selected), "library_file_verified": absolute_verified,
            "pyspice_distribution_version": pyspice_version,
            "python": sys.version.split()[0], "python_executable": sys.executable,
            "pythonpath_set": bool(os.environ.get("PYTHONPATH")),
            "native_backend_verified": bool(absolute_verified and pyspice_version)}


def _piecewise_value(points: list[list[float]], at: float) -> float:
    if at <= points[0][0]:
        return points[0][1]
    for (left_t, left_v), (right_t, right_v) in zip(points, points[1:]):
        if left_t <= at <= right_t:
            return left_v + (right_v - left_v) * (at - left_t) / (right_t - left_t)
    return points[-1][1]


def clock_value(at: float, fixture: SampleHoldFixture) -> float:
    """ngspice PULSE(V1 V2 TD TR TF PW PER), with PW the high plateau."""
    c = fixture.conditions
    phase = at % c.clock_period_s
    rise = c.clock_rise_s
    fall = c.clock_fall_s
    plateau = c.clock_duty * c.clock_period_s - rise
    if phase < rise:
        return c.clock_low_v + (c.clock_high_v - c.clock_low_v) * phase / rise
    if phase < rise + plateau:
        return c.clock_high_v
    if phase < rise + plateau + fall:
        return c.clock_high_v - (c.clock_high_v - c.clock_low_v) * (
            phase - rise - plateau) / fall
    return c.clock_low_v


def build_stimulus(fixture: SampleHoldFixture, *, startup_vin_v: float) -> dict:
    """Produce exact finite-edge PWL and clock source; no simulator calls."""
    c = fixture.conditions
    if fixture.mapping.circuit_id != "circuit_0259" or c.vin_edge_s != 10e-9:
        raise CanaryError("unexpected canary identity or VIN edge")
    if not c.clock_rise_s or not c.clock_fall_s:
        raise CanaryError("finite clock edges required")
    plateau = c.clock_duty * c.clock_period_s - c.clock_rise_s
    if plateau <= 0 or plateau + c.clock_rise_s + c.clock_fall_s >= c.clock_period_s:
        raise CanaryError("invalid clock plateau")
    if not c.vin_range_v[0] <= startup_vin_v <= c.vin_range_v[1]:
        raise CanaryError("startup VIN outside working range")
    points: list[list[float]] = [[0.0, startup_vin_v]]
    previous = startup_vin_v
    for index, cycle in enumerate(fixture.cycles):
        start = (fixture.startup_cycles + index) * c.clock_period_s
        points.extend([[start, previous], [start + c.vin_edge_s, cycle.track_initial_v],
                       [cycle.track_step_s, cycle.track_initial_v],
                       [cycle.track_step_s + c.vin_edge_s, cycle.intended_v],
                       [cycle.disturbance_s, cycle.intended_v],
                       [cycle.disturbance_s + c.vin_edge_s, cycle.disturbed_v]])
        previous = cycle.disturbed_v
    end = (fixture.startup_cycles + len(fixture.cycles)) * c.clock_period_s
    points.append([end, previous])
    if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise CanaryError("non-increasing PWL time")
    if any(not c.vin_range_v[0] <= v <= c.vin_range_v[1] for _, v in points):
        raise CanaryError("PWL exceeds working range")
    for cycle in fixture.cycles:
        guard, edge = fixture.guard_time_s, c.vin_edge_s
        if abs(_piecewise_value(points, cycle.track_step_s - guard) -
               _piecewise_value(points, cycle.track_step_s + edge + guard)) < 0.1:
            raise CanaryError("missing finite Track step")
        if abs(_piecewise_value(points, cycle.disturbance_s - guard) -
               _piecewise_value(points, cycle.disturbance_s + edge + guard)) < 0.1:
            raise CanaryError("missing finite Hold perturbation")
        if abs(_piecewise_value(points, cycle.track_pre_s) - cycle.track_initial_v) > 1e-9:
            raise CanaryError("cross-cycle reset contaminates Track pre-window")
    clock = {"low_v": c.clock_low_v, "high_v": c.clock_high_v,
             "delay_s": 0.0, "rise_s": c.clock_rise_s, "fall_s": c.clock_fall_s,
             "high_plateau_s": plateau, "period_s": c.clock_period_s}
    return {"vin_pwl_points_s_v": points, "clock_pulse": clock,
            "end_time_s": end, "max_timestep_s": fixture.max_timestep_s}


def load_plan(fixture_path: Path) -> dict:
    """Read-only validation; no output directory or simulator initialization."""
    fixture_path = fixture_path.resolve(strict=True)
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if data.get("schema") not in (SCHEMA, SCHEMA_SC4B3A, SCHEMA_SC4B3C) or data.get("status") != "candidate_unapproved":
        raise CanaryError("not the circuit_0259 unapproved V1 fixture")
    if data["schema"] in (SCHEMA_SC4B3A, SCHEMA_SC4B3C):
        closure_path_by_schema = {
            SCHEMA_SC4B3A: (SOURCE_CLOSURE_PATH, "sc_0259_canary_source_closure_sc4b3a"),
            SCHEMA_SC4B3C: (SOURCE_CLOSURE_PATH_SC4B3C, "sc_0259_canary_source_closure_sc4b3c"),
        }
        expected_path, expected_schema = closure_path_by_schema[data["schema"]]
        closure_asset = data.get("source_assets", {}).get("source_closure", {})
        if closure_asset.get("path") != expected_path:
            raise CanaryError("source closure identity missing for fixture schema")
        closure_path = (ROOT / expected_path).resolve(strict=True)
        if not closure_path.is_relative_to(ROOT) or _hash_file(closure_path) != closure_asset.get("sha256"):
            raise CanaryError("source closure hash/path mismatch")
        closure = json.loads(closure_path.read_text(encoding="utf-8"))
        if closure.get("schema") != expected_schema:
            raise CanaryError("invalid source closure schema")
        sources = closure.get("source_files")
        if (not isinstance(sources, list) or not sources or
                sources != sorted(sources, key=lambda item: item["path"])):
            raise CanaryError("source closure ordering or entries invalid")
        source_by_name = {}
        source_paths = set()
        for item in sources:
            if (set(item) != {"name", "path", "sha256"} or
                    item["name"] in source_by_name or item["path"] in source_paths):
                raise CanaryError("duplicate or malformed source closure entry")
            source_by_name[item["name"]] = {"path": item["path"], "sha256": item["sha256"]}
            source_paths.add(item["path"])
            path = (ROOT / item["path"]).resolve(strict=True)
            if not path.is_relative_to(ROOT) or not path.is_file() or _hash_file(path) != item["sha256"]:
                raise CanaryError(f"source closure file mismatch: {item['name']}")
        if set(source_by_name) != REQUIRED_CLOSURE_SOURCES:
            raise CanaryError("source closure file set incomplete or unexpected")
        for name, asset in data["source_assets"].items():
            if name != "source_closure" and source_by_name.get(name) != asset:
                raise CanaryError(f"fixture/closure source mismatch: {name}")
        external = closure["external_environment"]
        if (Path(sys.executable).resolve() != Path(external["python_executable"]).resolve() or
                sys.version.split()[0] != external["python_version"]):
            raise CanaryError("Python execution environment differs from source closure")
        for package, version in external["packages"].items():
            try:
                actual = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError as error:
                raise CanaryError(f"required package unavailable: {package}") from error
            if actual != version:
                raise CanaryError(f"package identity differs: {package}")
        if set(external["environment_paths"]) != {"NGSPICE_LIBRARY_PATH", "SPICE_LIB_DIR"}:
            raise CanaryError("frozen native environment paths incomplete")
        for variable, target in external["environment_paths"].items():
            if Path(os.environ.get(variable, "")).resolve() != Path(target).resolve():
                raise CanaryError(f"native environment path differs: {variable}")
        if os.environ.get("PYTHONPATH"):
            raise CanaryError("PYTHONPATH contamination in frozen native environment")
        if set(external["native_files"]) != {"ngspice_dll", "libomp_dll", "spinit"}:
            raise CanaryError("frozen native dependency set incomplete")
        for name, asset in external["native_files"].items():
            path = Path(asset["path"]).resolve(strict=True)
            if not path.is_file() or _hash_file(path) != asset["sha256"]:
                raise CanaryError(f"native dependency mismatch: {name}")
    if (data.get("experiment_id") != "sc0259_ideal_v1_canary_1" or
            data.get("circuit_id") != "circuit_0259"):
        raise CanaryError("unexpected experiment identity")
    if data.get("output_root") != FIXED_OUTPUT_ROOT.as_posix():
        raise CanaryError("stable one-time output/ledger root changed")
    for name, (relative, expected_hash) in EXPECTED_INPUT_ASSETS.items():
        if data["source_assets"].get(name) != {"path": relative, "sha256": expected_hash}:
            raise CanaryError(f"frozen circuit/model asset changed: {name}")
    for name, asset in data["source_assets"].items():
        path = (ROOT / asset["path"]).resolve(strict=True)
        if not path.is_relative_to(ROOT) or _hash_file(path) != asset["sha256"]:
            raise CanaryError(f"source hash/path mismatch: {name}")
    fixed = data["fixed_parameters"]
    if fixed != {"W_M0_m": 10e-6, "L_M0_m": 1e-6, "C0_f": 1e-12,
                  "model": {"LEVEL": 1, "VTO": 0.7, "KP": 200e-6, "LAMBDA": 0.02},
                  "temperature_c": 25.0, "nominal_temperature_c": 25.0,
                  "additional_load": None, "forced_initial_condition": None}:
        raise CanaryError("fixed model/parameter configuration changed")
    mapping = CircuitMapping(**data["fixture_inputs"]["mapping"])
    conditions = OperatingConditions(**data["fixture_inputs"]["conditions"])
    if (mapping.circuit_id != "circuit_0259" or mapping.inputs != ["VIN1"] or
            mapping.outputs != ["VOUT1"] or mapping.clock != "VCLK1" or
            mapping.track_clock_level != "HIGH" or
            mapping.source_topology_sha256 != EXPECTED_INPUT_ASSETS["topology"][1]):
        raise CanaryError("semantic node mapping differs from frozen topology")
    if (conditions.vin_range_v != [0.8, 1.6] or conditions.clock_period_s != 1e-6 or
            conditions.clock_low_v != 0.0 or conditions.clock_high_v != 3.3 or
            conditions.clock_duty != 0.4 or conditions.clock_rise_s != 1e-8 or
            conditions.clock_fall_s != 1e-8 or conditions.vin_edge_s != 1e-8 or
            data["fixture_inputs"]["startup_cycles"] != 2 or
            data["fixture_inputs"]["measurement_cycles"] != 6 or
            data["fixture_inputs"]["guard_time_s"] != 5e-9 or
            data["startup_vin_v"] != 1.2):
        raise CanaryError("frozen stimulus conditions changed")
    fixture = build_fixture(mapping, conditions,
                            startup_cycles=data["fixture_inputs"]["startup_cycles"],
                            measurement_cycles=data["fixture_inputs"]["measurement_cycles"],
                            guard_time_s=data["fixture_inputs"]["guard_time_s"])
    if fixture.fixture_hash != data["abstract_fixture_sha256"]:
        raise CanaryError("fixture hash mismatch")
    stimulus = build_stimulus(fixture, startup_vin_v=data["startup_vin_v"])
    if _canonical_hash(stimulus) != data["stimulus_sha256"]:
        raise CanaryError("stimulus hash mismatch")
    if data["budget"] != {"logical_evaluations_max": 1,
                          "native_transient_attempts_max": 1,
                          "automatic_retries": 0,
                          "single_native_attempt_timeout_s": 180,
                          "overall_execution_timeout_s": 240}:
        raise CanaryError("physical budget changed")
    return {"config": data, "path": fixture_path, "file_sha256": _hash_file(fixture_path),
            "fixture": fixture, "stimulus": stimulus,
            "backend": _native_library_status()}


def _validate_approval(plan: dict, approval_path: Path, *, mock_backend: bool) -> dict:
    approval = json.loads(approval_path.resolve(strict=True).read_text(encoding="utf-8"))
    required = {"schema": "sc_0259_one_time_operator_approval_v1",
                "intent": "one_real_transient", "experiment_id": plan["config"]["experiment_id"],
                "fixture_file_sha256": plan["file_sha256"],
                "abstract_fixture_sha256": plan["fixture"].fixture_hash,
                "stimulus_sha256": plan["config"]["stimulus_sha256"],
                "source_assets": plan["config"]["source_assets"],
                "budget": plan["config"]["budget"]}
    if any(approval.get(key) != value for key, value in required.items()):
        raise CanaryError("approval does not bind the frozen fixture/assets/budget")
    if not str(approval.get("approved_by", "")).strip() or not str(approval.get("approved_at", "")).strip():
        raise CanaryError("operator identity/time missing")
    try:
        approved_at = datetime.fromisoformat(approval["approved_at"])
    except (TypeError, ValueError) as error:
        raise CanaryError("operator approval timestamp invalid") from error
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise CanaryError("operator approval timestamp must include timezone")
    output_root = Path(approval.get("output_root", ""))
    if not output_root.is_absolute() or output_root.resolve() == ROOT or output_root.resolve().is_relative_to(ROOT):
        raise CanaryError("approval output_root must be absolute and outside repository")
    if not mock_backend and output_root.resolve() != FIXED_OUTPUT_ROOT.resolve():
        raise CanaryError("real approval cannot relocate the stable budget ledger")
    return approval


def _terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        raise UncertainProcessError("Windows process trees require Job Object containment")
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except BaseException as error:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise UncertainProcessError("native process-tree termination is uncertain") from error
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as error:
        raise UncertainProcessError("native child/process tree could not be reaped") from error


def _native_launcher(command: list[str], *, cwd: Path, timeout: float,
                     environment: dict[str, str]) -> subprocess.CompletedProcess:
    if os.name == "nt":
        from ActiveLearning.windows_job_isolation import JobIsolationError, run_in_job

        try:
            return run_in_job(command, cwd=cwd, timeout=timeout, environment=environment)
        except JobIsolationError as error:
            raise UncertainProcessError("Windows Job containment or cleanup unconfirmed") from error
    process = subprocess.Popen(command, cwd=cwd, env=environment, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        _terminate_tree(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_guarded(plan: dict, approval_path: Path, *, launcher: Callable = _native_launcher,
                acknowledgement: bool = False) -> dict:
    """Only real-mode entry; injectable launcher is for explicitly selected mock tests."""
    if not acknowledgement:
        raise CanaryError("separate operator acknowledgement required")
    overall_start = time.monotonic()
    approval = _validate_approval(plan, approval_path, mock_backend=launcher is not _native_launcher)
    if not plan["backend"]["native_backend_verified"] and launcher is _native_launcher:
        raise CanaryError("native simulator library/PySpice not statically verified")
    root = Path(approval["output_root"]).resolve()
    experiment = root / plan["config"]["experiment_id"]
    root.mkdir(parents=True, exist_ok=True)
    lock = root / (plan["config"]["experiment_id"] + ".lock")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise CanaryError("experiment lock exists; fail closed pending audit") from error
    released = False
    try:
        if experiment.exists():
            raise CanaryError("experiment already exists; no replay or overwrite")
        experiment.mkdir()
        ledger = experiment / "physical_attempt_ledger.jsonl"
        _write_json(experiment / "approved_fixture.json", plan["config"])
        _write_json(experiment / "source_and_fixture_hashes.json",
                    {"fixture_file_sha256": plan["file_sha256"],
                     "fixture_sha256": plan["fixture"].fixture_hash,
                     "stimulus_sha256": plan["config"]["stimulus_sha256"],
                     "source_assets": plan["config"]["source_assets"],
                     "approval_file_sha256": _hash_file(approval_path)})
        _write_json(experiment / "stimulus.json", plan["stimulus"])
        token = uuid.uuid4().hex
        intent = {"event": "native_attempt_reserved", "attempt": 1,
                  "experiment_id": plan["config"]["experiment_id"],
                  "fixture_file_sha256": plan["file_sha256"],
                  "utc_unix_s": time.time(), "token": token}
        _append_ledger(ledger, intent)  # Durable consumption BEFORE child launch.
        _write_json(experiment / "launch_intent.json", intent)
        command = [sys.executable, "-m", "ActiveLearning.sc_0259_canary_child",
                   "--fixture", str(plan["path"]), "--experiment", str(experiment),
                   "--token", token]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)  # Do not inherit unrelated NumPy injections.
        timeout = min(plan["config"]["budget"]["single_native_attempt_timeout_s"],
                      plan["config"]["budget"]["overall_execution_timeout_s"] -
                      (time.monotonic() - overall_start))
        if timeout <= 0:
            raise TimeoutError("overall deadline elapsed before child launch")
        try:
            completed = launcher(command, cwd=ROOT, timeout=timeout, environment=environment)
            _write_json(experiment / "simulator_status.json",
                        {"returncode": completed.returncode, "mock": launcher is not _native_launcher,
                         "process_isolation": getattr(completed, "job_evidence", None)})
            (experiment / "child_stdout.txt").write_text(completed.stdout or "", encoding="utf-8")
            (experiment / "child_stderr.txt").write_text(completed.stderr or "", encoding="utf-8")
            if completed.returncode != 0:
                raise CanaryError(f"native child exited {completed.returncode}")
            if time.monotonic() - overall_start > plan["config"]["budget"]["overall_execution_timeout_s"]:
                raise TimeoutError("overall execution deadline exceeded")
            raw_path = experiment / "raw_waveforms.json"
            if not raw_path.is_file():
                raise CanaryError("native child produced no waveform file")
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            if raw.get("mock") is not (launcher is not _native_launcher):
                raise CanaryError("mock/real waveform provenance mismatch")
            result = evaluate_waveforms(plan["fixture"], raw["waveforms"], synthetic=raw["mock"])
            _write_json(experiment / "evaluation_result.json", result.to_dict())
            _append_ledger(ledger, {"event": "native_attempt_finished", "attempt": 1,
                                    "outcome": "complete" if result.measurement_valid else "measurement_invalid",
                                    "utc_unix_s": time.time()})
            released = True
            return {"status": "complete" if result.measurement_valid else "measurement_invalid",
                    "experiment": str(experiment), "result": result.to_dict(),
                    "mock": launcher is not _native_launcher}
        except BaseException as error:
            outcome = "timeout" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else (
                "interrupted_or_uncertain" if isinstance(error, (KeyboardInterrupt, UncertainProcessError))
                else "failed")
            _append_ledger(ledger, {"event": "native_attempt_finished", "attempt": 1,
                                    "outcome": outcome, "error_type": type(error).__name__,
                                    "utc_unix_s": time.time()})
            _write_json(experiment / "failure_diagnostics.json",
                        {"outcome": outcome, "error_type": type(error).__name__,
                         "message": str(error)})
            released = not isinstance(error, UncertainProcessError)
            raise
    finally:
        if released:
            lock.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SC-0259 offline plan; real mode requires separate approval")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--execute-real-spice", action="store_true")
    parser.add_argument("--approval-file", type=Path)
    parser.add_argument("--acknowledge-approval", action="store_true")
    args = parser.parse_args(argv)
    plan = load_plan(args.fixture)
    if not args.execute_real_spice:
        print(json.dumps({"mode": "OFFLINE_PLAN", "experiment_id": plan["config"]["experiment_id"],
                          "fixture_sha256": plan["fixture"].fixture_hash,
                          "stimulus_sha256": plan["config"]["stimulus_sha256"],
                          "source_hashes_verified": True, "backend": plan["backend"],
                          "native_attempts": 0, "real_execution_authorized": False}, indent=2))
        return 0
    if args.approval_file is None:
        raise CanaryError("separate approval file required for real execution")
    run_guarded(plan, args.approval_file, acknowledgement=args.acknowledge_approval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
