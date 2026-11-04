"""Linux-only, one-attempt circuit_0259 native canary admission.

Capture is a draft, never an authorization. A reviewed Ubuntu closure and a
separate operator approval are required before the physical entry point opens.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from ActiveLearning.sample_hold_offline import CircuitMapping, OperatingConditions, build_fixture, evaluate_waveforms
from ActiveLearning.sc_0259_canary import EXPECTED_INPUT_ASSETS, FIXED_OUTPUT_ROOT, build_stimulus
from ActiveLearning.sample_hold_posix import ProcessGroupUncertain, run_isolated


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "ActiveLearning/docs/reference_audit/SC_0259_TRANSIENT_FIXTURE_V1_SC4B3C.json"
EXPERIMENT_ID = "sc0259_linux_native_canary_v1"
FIXTURE_SEMANTIC_SHA256 = "29329d5284d0e0dc6c20d1bf748e92d9776291149706e5f28b14be0c78e664e2"
SOURCE_PATHS = (
    "ActiveLearning/__init__.py",
    "ActiveLearning/sample_hold_linux_canary.py",
    "ActiveLearning/sample_hold_linux_canary_child.py",
    "ActiveLearning/sample_hold_posix.py",
    "ActiveLearning/sample_hold_offline.py",
    "ActiveLearning/sc_0259_canary.py",
    "ActiveLearning/docs/reference_audit/SC_0259_TRANSIENT_FIXTURE_V1_SC4B3C.json",
    "run/__init__.py",
    "run/single_stage_amplifier/__init__.py",
    "run/single_stage_amplifier/run_ideal_dataset_simulations.py",
    "conversion/__init__.py",
    "conversion/cadence_to_pyspice.py",
    "simulation_metrics/__init__.py",
    "simulation_metrics/ac.py",
    "simulation_metrics/dc.py",
    "simulation_metrics/loop_gain.py",
    "simulation_metrics/op.py",
    "simulation_metrics/transient.py",
    *(item[0] for item in EXPECTED_INPUT_ASSETS.values()),
)
PACKAGES = ("PySpice", "numpy", "scipy", "PyYAML", "cffi")
PACKAGE_MODULES = {"PySpice": "PySpice", "numpy": "numpy", "scipy": "scipy",
                   "PyYAML": "yaml", "cffi": "cffi"}
BUDGET = {"logical_evaluations_max": 1, "native_transient_attempts_max": 1,
          "automatic_retries": 0, "single_native_attempt_timeout_s": 180,
          "overall_execution_timeout_s": 240}


class AdmissionError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _write_new(path: Path, value: object) -> None:
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _append(path: Path, value: object) -> None:
    with path.open("ab") as stream:
        stream.write((json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())


def _frozen_fixture() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    semantic = json.loads(json.dumps(data))
    semantic.pop("output_root", None)
    semantic.pop("experiment_id", None)
    semantic["source_assets"].pop("source_closure", None)
    if (_canonical(semantic) != FIXTURE_SEMANTIC_SHA256 or
            data.get("experiment_id") != "sc0259_ideal_v1_canary_1" or
            data.get("output_root") != FIXED_OUTPUT_ROOT.as_posix() or
            data["source_assets"].get("source_closure") != {
                "path": "ActiveLearning/docs/reference_audit/SC_0259_CANARY_SOURCE_CLOSURE_SC4B3C.json",
                "sha256": "6ba56786bd88d21bfa7d1cb1d5a2f51dc228048c5b293ea91d145d5a385ac35e"}):
        raise AdmissionError("0259 historical fixture semantics changed")
    for name, (relative, historical_sha) in EXPECTED_INPUT_ASSETS.items():
        asset = data["source_assets"].get(name)
        if asset != {"path": relative, "sha256": historical_sha}:
            raise AdmissionError(f"historical asset identity changed: {name}")
    mapping = CircuitMapping(**data["fixture_inputs"]["mapping"])
    conditions = OperatingConditions(**data["fixture_inputs"]["conditions"])
    inputs = data["fixture_inputs"]
    fixture = build_fixture(mapping, conditions, startup_cycles=inputs["startup_cycles"],
                            measurement_cycles=inputs["measurement_cycles"],
                            guard_time_s=inputs["guard_time_s"])
    stimulus = build_stimulus(fixture, startup_vin_v=data["startup_vin_v"])
    if (fixture.fixture_hash != data["abstract_fixture_sha256"] or
            _canonical(stimulus) != data["stimulus_sha256"] or data["budget"] != BUDGET):
        raise AdmissionError("0259 fixture, stimulus or budget differs from frozen canary")
    return {"data": data, "fixture": fixture, "stimulus": stimulus}


def _is_linux() -> bool:
    return sys.platform == "linux" and os.name == "posix"


def _linux_environment(library: Path) -> dict:
    if not _is_linux():
        raise AdmissionError("Linux native admission requires Linux POSIX runtime")
    if not library.is_absolute() or library.name not in ("libngspice.so.0", "libngspice.so"):
        raise AdmissionError("explicit absolute libngspice.so path required")
    requested_library = str(library)
    library = library.resolve(strict=True)
    if not library.is_file():
        raise AdmissionError("ngspice shared library target is not a file")
    try:
        ctypes.CDLL(str(library))
    except OSError as error:
        raise AdmissionError("libngspice could not be loaded") from error
    versions = {}
    for package in PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as error:
            raise AdmissionError(f"required package missing: {package}") from error
        try:
            importlib.import_module(PACKAGE_MODULES[package])
        except ImportError as error:
            raise AdmissionError(f"required package cannot import: {package}") from error
    if os.environ.get("PYTHONPATH"):
        raise AdmissionError("PYTHONPATH contamination is not admitted")
    override = os.environ.get("NGSPICE_LIBRARY_PATH")
    if override and Path(override).resolve() != library:
        raise AdmissionError("ngspice library override differs from closure")
    spice_lib_dir = os.environ.get("SPICE_LIB_DIR")
    if spice_lib_dir:
        raise AdmissionError("external SPICE_LIB_DIR is not admitted for ideal-model canary")
    return {"python_executable": str(Path(sys.executable).resolve()),
            "python_version": sys.version.split()[0], "packages": versions,
            "ngspice_library": {"requested_path": requested_library,
                                "path": str(library), "sha256": _sha(library)},
            "spice_lib_dir": None}


def _external_output_root(path: Path) -> str:
    if not path.is_absolute():
        raise AdmissionError("output root must be absolute")
    resolved = path.resolve()
    if resolved == ROOT or resolved.is_relative_to(ROOT):
        raise AdmissionError("output root must be outside checkout")
    return str(resolved)


def capture_closure(*, library: Path, output_root: Path, output: Path) -> dict:
    """Capture Ubuntu bytes as an unapproved draft; never overwrite an existing file."""
    if not output.is_absolute() or output.resolve().is_relative_to(ROOT):
        raise AdmissionError("closure file must be absolute and outside checkout")
    _frozen_fixture()
    environment = _linux_environment(library)
    files = []
    for relative in sorted(SOURCE_PATHS):
        path = (ROOT / relative).resolve(strict=True)
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise AdmissionError(f"missing or escaping source: {relative}")
        files.append({"path": relative, "sha256": _sha(path)})
    closure = {"schema": "sample_hold_0259_linux_source_closure_v1",
               "status": "draft_unapproved", "circuit_id": "circuit_0259",
               "experiment_id": EXPERIMENT_ID, "output_root": _external_output_root(output_root),
               "fixture_semantic_sha256": FIXTURE_SEMANTIC_SHA256,
               "fixture_file_sha256": _sha(FIXTURE), "source_files": files,
               "external_environment": environment, "budget": BUDGET,
               "windows_provenance": {"historical_fixture": str(FIXTURE.relative_to(ROOT)),
                                      "windows_source_closure": _frozen_fixture()["data"]["source_assets"]["source_closure"]}}
    _write_new(output, closure)
    return closure


def verify_closure(path: Path, *, require_frozen: bool = False) -> dict:
    closure_file = path.resolve(strict=True)
    if closure_file.is_relative_to(ROOT):
        raise AdmissionError("Linux closure must be stored outside checkout")
    closure = json.loads(closure_file.read_text(encoding="utf-8"))
    status = "frozen_verified" if require_frozen else closure.get("status")
    if (closure.get("schema") != "sample_hold_0259_linux_source_closure_v1" or
            closure.get("status") != status or status not in ("draft_unapproved", "frozen_verified") or
            closure.get("circuit_id") != "circuit_0259" or
            closure.get("experiment_id") != EXPERIMENT_ID or closure.get("budget") != BUDGET or
            closure.get("fixture_semantic_sha256") != FIXTURE_SEMANTIC_SHA256 or
            closure.get("fixture_file_sha256") != _sha(FIXTURE)):
        raise AdmissionError("Linux closure identity, status or frozen budget mismatch")
    fixture = _frozen_fixture()
    if closure.get("windows_provenance") != {
            "historical_fixture": str(FIXTURE.relative_to(ROOT)),
            "windows_source_closure": fixture["data"]["source_assets"]["source_closure"]}:
        raise AdmissionError("historical Windows provenance differs")
    _external_output_root(Path(closure.get("output_root", "")))
    files = closure.get("source_files")
    if (not isinstance(files, list) or
            [entry.get("path") for entry in files] != sorted(SOURCE_PATHS)):
        raise AdmissionError("Linux source closure file set differs")
    for entry in files:
        relative = entry["path"]
        target = (ROOT / relative).resolve(strict=True)
        if not target.is_relative_to(ROOT) or not target.is_file() or _sha(target) != entry.get("sha256"):
            raise AdmissionError(f"Linux source bytes changed: {relative}")
    env = closure.get("external_environment", {})
    library = Path(env.get("ngspice_library", {}).get("requested_path", ""))
    if not library.is_absolute() or _linux_environment(library) != env:
        raise AdmissionError("Linux Python/package/ngspice environment differs")
    return closure


def _approval(path: Path, closure_path: Path, closure: dict) -> dict:
    approval_file = path.resolve(strict=True)
    if approval_file.is_relative_to(ROOT):
        raise AdmissionError("operator approval must be stored outside checkout")
    approval = json.loads(approval_file.read_text(encoding="utf-8"))
    required = {"schema": "sample_hold_0259_linux_operator_approval_v1",
                "intent": "one_real_transient", "circuit_id": "circuit_0259",
                "experiment_id": EXPERIMENT_ID, "source_closure_sha256": _sha(closure_path),
                "fixture_file_sha256": closure["fixture_file_sha256"],
                "fixture_semantic_sha256": FIXTURE_SEMANTIC_SHA256,
                "budget": BUDGET, "allow_replay": False}
    if any(approval.get(key) != value for key, value in required.items()):
        raise AdmissionError("operator approval does not bind closure/fixture/budget")
    if not str(approval.get("approved_by", "")).strip():
        raise AdmissionError("operator identity missing")
    try:
        approved_at = datetime.fromisoformat(approval["approved_at"])
        if approved_at.utcoffset() is None:
            raise ValueError("timezone missing")
    except (KeyError, TypeError, ValueError) as error:
        raise AdmissionError("operator timestamp must be timezone-aware ISO-8601") from error
    output_root = Path(approval.get("output_root", ""))
    if _external_output_root(output_root) != closure["output_root"]:
        raise AdmissionError("approved output root differs from frozen closure")
    return approval


def execute(*, closure_path: Path, approval_path: Path, acknowledgement: bool,
            launcher=run_isolated) -> dict:
    if not acknowledgement:
        raise AdmissionError("separate operator approval acknowledgement required")
    start = time.monotonic()
    closure = verify_closure(closure_path, require_frozen=True)
    approval = _approval(approval_path, closure_path, closure)
    plan = _frozen_fixture()
    root = Path(approval["output_root"]).resolve()
    experiment = root / EXPERIMENT_ID
    root.mkdir(parents=True, exist_ok=True)
    lock = root / (EXPERIMENT_ID + ".lock")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise AdmissionError("experiment lock exists; fail closed") from error
    released = False
    try:
        if experiment.exists():
            raise AdmissionError("experiment exists; no replay or overwrite")
        experiment.mkdir()
        ledger = experiment / "physical_attempt_ledger.jsonl"
        _write_new(experiment / "approved_closure.json", closure)
        _write_new(experiment / "approved_fixture.json", plan["data"])
        _write_new(experiment / "approval_identity.json",
                   {"approval_sha256": _sha(approval_path), "closure_sha256": _sha(closure_path)})
        _write_new(experiment / "stimulus.json", plan["stimulus"])
        token = uuid.uuid4().hex
        intent = {"event": "native_attempt_reserved", "attempt": 1,
                  "experiment_id": EXPERIMENT_ID, "token": token,
                  "closure_sha256": _sha(closure_path), "utc_unix_s": time.time()}
        _append(ledger, intent)
        _write_new(experiment / "launch_intent.json", intent)
        command = [sys.executable, "-m", "ActiveLearning.sample_hold_linux_canary_child",
                   "--closure", str(closure_path.resolve()), "--experiment", str(experiment),
                   "--token", token]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["NGSPICE_LIBRARY_PATH"] = closure["external_environment"]["ngspice_library"]["requested_path"]
        timeout = min(BUDGET["single_native_attempt_timeout_s"],
                      BUDGET["overall_execution_timeout_s"] - (time.monotonic() - start))
        try:
            if timeout <= 0:
                raise TimeoutError("overall deadline elapsed")
            completed = launcher(command, cwd=ROOT, timeout=timeout, environment=environment)
            _write_new(experiment / "simulator_status.json", {"returncode": completed.returncode})
            _write_new(experiment / "child_output.json",
                       {"stdout": completed.stdout, "stderr": completed.stderr})
            if completed.returncode != 0:
                raise AdmissionError(f"native child exited {completed.returncode}")
            if time.monotonic() - start > BUDGET["overall_execution_timeout_s"]:
                raise TimeoutError("overall deadline elapsed after child")
            raw = json.loads((experiment / "raw_waveforms.json").read_text(encoding="utf-8"))
            if raw.get("backend") != "PySpice-ngspice-linux-isolated-child" or raw.get("mock") is not False:
                raise AdmissionError("physical waveform provenance invalid")
            if raw.get("closure_sha256") != _sha(closure_path):
                raise AdmissionError("waveform closure identity mismatch")
            result = evaluate_waveforms(plan["fixture"], raw["waveforms"], synthetic=False)
            _write_new(experiment / "evaluation_result.json", result.to_dict())
            _append(ledger, {"event": "native_attempt_finished", "attempt": 1,
                             "outcome": "complete" if result.measurement_valid else "measurement_invalid",
                             "utc_unix_s": time.time()})
            released = True
            return {"status": "complete" if result.measurement_valid else "measurement_invalid",
                    "experiment": str(experiment), "result": result.to_dict()}
        except BaseException as error:
            uncertain = isinstance(error, (ProcessGroupUncertain, KeyboardInterrupt))
            _append(ledger, {"event": "native_attempt_finished", "attempt": 1,
                             "outcome": "interrupted_or_uncertain" if uncertain else "failed",
                             "error_type": type(error).__name__, "utc_unix_s": time.time()})
            _write_new(experiment / "failure_diagnostics.json",
                       {"error_type": type(error).__name__, "message": str(error)})
            released = not uncertain
            raise
    finally:
        if released:
            lock.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Linux 0259 closure and guarded one-time canary")
    sub = parser.add_subparsers(dest="action", required=True)
    capture = sub.add_parser("capture-closure")
    capture.add_argument("--ngspice-library", required=True, type=Path)
    capture.add_argument("--output-root", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    verify = sub.add_parser("verify-closure")
    verify.add_argument("--closure", required=True, type=Path)
    run = sub.add_parser("execute-real-spice")
    run.add_argument("--closure", required=True, type=Path)
    run.add_argument("--approval-file", required=True, type=Path)
    run.add_argument("--acknowledge-approval", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "capture-closure":
        result = capture_closure(library=args.ngspice_library, output_root=args.output_root,
                                 output=args.output)
        print(json.dumps({"status": result["status"], "output": str(args.output)}))
    elif args.action == "verify-closure":
        result = verify_closure(args.closure)
        print(json.dumps({"status": result["status"], "closure_sha256": _sha(args.closure)}))
    else:
        result = execute(closure_path=args.closure, approval_path=args.approval_file,
                         acknowledgement=args.acknowledge_approval)
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
