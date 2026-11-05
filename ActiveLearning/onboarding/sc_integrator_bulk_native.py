"""SCIntegrator durable POSIX executor; shares only proven ledger primitives."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Any, Mapping

from ActiveLearning.sc_sample_hold_generic import canonical_sha256, sha256_file
from ActiveLearning.sample_hold_posix import ProcessGroupUncertain, run_isolated
from .errors import AutoConfigError
from .sample_hold_bulk_native import _append, _durable_json, _events
from .sc_integrator_bulk_core import ROOT, derive_plan
from .sample_hold_bulk_core import sha


class SCIntegratorPhysicalExecutor:
    """One independently budgeted candidate bundle per source-bound vector."""

    def __init__(self, runtime: Any, stage_root: Path, *, stage: str, limit: int,
                 timeout_seconds: int = 300, extra_identity: Mapping[str, Any] | None = None):
        if os.name != "posix" or not sys.platform.startswith("linux"):
            raise AutoConfigError("SCIntegrator physical execution requires Ubuntu/Linux")
        if stage not in {"profile", "d0", "al"} or type(limit) is not int or not 1 <= limit <= 50:
            raise AutoConfigError("invalid SCIntegrator physical stage or budget")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
            raise AutoConfigError("invalid SCIntegrator native timeout")
        self.runtime = runtime
        self.plan = derive_plan(runtime.circuit_id)
        self.root = Path(stage_root).resolve()
        if not self.root.is_absolute() or self.root.is_relative_to(ROOT):
            raise AutoConfigError("SCIntegrator physical ledger must be outside checkout")
        self.stage, self.limit, self.timeout = stage, limit, timeout_seconds
        from .sample_hold_ubuntu import LIBRARY_ENV
        from ActiveLearning.sample_hold_linux_canary import _linux_environment
        library = os.getenv(LIBRARY_ENV)
        if not library:
            raise AutoConfigError(f"{LIBRARY_ENV} is required")
        self.environment = _linux_environment(Path(library))
        self.identity = {
            "schema": "sc_integrator_bulk_native_v1",
            "circuit_id": runtime.circuit_id, "stage": stage,
            "limit": limit, "timeout_seconds": timeout_seconds,
            "source_sha256": self.plan["source_sha256"],
            "parameter_space_sha256": canonical_sha256(runtime.param_template),
            "fixture_sha256": self.plan["fixture_sha256"],
            "stimulus_sha256": self.plan["stimulus_sha256"],
            "executor_sha256": sha(Path(__file__)),
            "ledger_helpers_sha256": sha(Path(__file__).with_name("sample_hold_bulk_native.py")),
            "model_binding_sha256": sha(Path(__file__).with_name("sample_hold_bulk_core.py")),
            "parameter_builder_sha256": sha(Path(__file__).with_name("sample_hold.py")),
            "posix_sha256": sha(ROOT / "ActiveLearning/sample_hold_posix.py"),
            "ngspice_config_sha256": sha(ROOT / "run/single_stage_amplifier/run_ideal_dataset_simulations.py"),
            "child_sha256": sha(Path(__file__).with_name("sc_integrator_bulk_child.py")),
            "binding_sha256": sha(Path(__file__).with_name("sc_integrator_bulk_core.py")),
            "measurement_sha256": sha(Path(__file__).with_name("sc_integrator_bulk_core.py")),
            "python_executable": str(Path(sys.executable).resolve()),
            "environment": self.environment,
            "extra": dict(extra_identity or {}),
        }
        self.identity_sha256 = canonical_sha256(self.identity)
        self.ledger = self.root / "physical_attempt_ledger.jsonl"

    def verify_existing(self) -> None:
        if not self.root.exists():
            return
        identity_path = self.root / "identity.json"
        if (not self.root.is_dir() or self.root.is_symlink()
                or not identity_path.is_file()
                or json.loads(identity_path.read_text(encoding="utf-8")) != self.identity):
            raise AutoConfigError("SCIntegrator physical source/environment identity changed")
        events = _events(self.ledger)
        reserved = [row for row in events if row.get("event") == "reserved"]
        if (len(reserved) > self.limit
                or [row.get("attempt") for row in reserved] != list(range(1, len(reserved) + 1))
                or len({row.get("vector_sha256") for row in reserved}) != len(reserved)
                or any(row.get("identity_sha256") != self.identity_sha256 for row in reserved)):
            raise AutoConfigError("SCIntegrator physical budget/duplicate ledger invalid")

    def __call__(self, arguments: Mapping[str, Any]) -> dict:
        self.verify_existing()
        self.root.mkdir(parents=True, exist_ok=True)
        identity_path = self.root / "identity.json"
        if not identity_path.exists():
            _durable_json(identity_path, self.identity)
        lock = self.root / ".lock"
        try:
            lock.mkdir()
        except FileExistsError as error:
            raise AutoConfigError("SCIntegrator physical executor busy or orphaned") from error
        release = False
        try:
            self.verify_existing()
            vector_sha = canonical_sha256(dict(arguments))
            candidate = self.root / "candidates" / vector_sha[:16]
            cached = candidate / "native_result.json"
            if cached.is_file():
                data = json.loads(cached.read_text(encoding="utf-8"))
                if (data.get("vector_sha256") != vector_sha
                        or data.get("identity_sha256") != self.identity_sha256
                        or not any(row.get("event") == "finished"
                                   and row.get("vector_sha256") == vector_sha
                                   and row.get("result_sha256") == sha256_file(cached)
                                   for row in _events(self.ledger))):
                    raise AutoConfigError("SCIntegrator cached native result changed")
                release = True
                return data["evaluation"]
            if candidate.exists():
                raise AutoConfigError("SCIntegrator vector already attempted; no automatic retry")
            reserved = [row for row in _events(self.ledger) if row.get("event") == "reserved"]
            if len(reserved) >= self.limit:
                raise AutoConfigError("SCIntegrator physical attempt budget exhausted")
            candidate.mkdir(parents=True)
            token = uuid.uuid4().hex
            request = {"identity_sha256": self.identity_sha256,
                       "vector_sha256": vector_sha, "arguments": dict(arguments),
                       "token": token}
            _durable_json(candidate / "request.json", request)
            intent = {"event": "reserved", "attempt": len(reserved) + 1,
                      "identity_sha256": self.identity_sha256,
                      "vector_sha256": vector_sha, "token": token, "unix_s": time.time()}
            _append(self.ledger, intent)
            _durable_json(candidate / "launch_intent.json", intent)
            command = [sys.executable, "-m", "ActiveLearning.onboarding.sc_integrator_bulk_child",
                       "--candidate", str(candidate), "--token", token]
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            try:
                result = run_isolated(command, cwd=ROOT, timeout=self.timeout,
                                      environment=environment)
            except ProcessGroupUncertain as error:
                raise AutoConfigError("SCIntegrator process reaping unconfirmed") from error
            except subprocess.TimeoutExpired:
                _append(self.ledger, {"event": "finished", "attempt": len(reserved) + 1,
                                      "vector_sha256": vector_sha,
                                      "outcome": "timeout_reaped", "unix_s": time.time()})
                release = True
                raise
            evidence = getattr(result, "job_evidence", None)
            if (not isinstance(evidence, dict)
                    or evidence.get("contained_before_resume") is not True
                    or evidence.get("active_processes_at_return") != 0):
                raise AutoConfigError("SCIntegrator process containment unconfirmed")
            _durable_json(candidate / "process_status.json", {
                "returncode": result.returncode, "job_evidence": evidence,
                "stdout": result.stdout, "stderr": result.stderr})
            if result.returncode != 0:
                _append(self.ledger, {"event": "finished", "attempt": len(reserved) + 1,
                                      "vector_sha256": vector_sha,
                                      "outcome": "failed_reaped", "exit_code": result.returncode,
                                      "unix_s": time.time()})
                release = True
                raise AutoConfigError(f"SCIntegrator native child exited {result.returncode}")
            data = json.loads(cached.read_text(encoding="utf-8"))
            if (data.get("vector_sha256") != vector_sha
                    or data.get("identity_sha256") != self.identity_sha256
                    or data.get("mock") is not False):
                raise AutoConfigError("SCIntegrator native result identity mismatch")
            _append(self.ledger, {"event": "finished", "attempt": len(reserved) + 1,
                                  "vector_sha256": vector_sha, "outcome": "complete",
                                  "result_sha256": sha256_file(cached), "unix_s": time.time()})
            release = True
            return data["evaluation"]
        finally:
            if release:
                lock.rmdir()

    def execution_statistics(self) -> dict:
        events = _events(self.ledger)
        return {"schema": "sc_integrator_bulk_native_v1", "stage": self.stage,
                "physical_attempts": sum(row.get("event") == "reserved" for row in events),
                "completed_attempts": sum(row.get("event") == "finished"
                                          and row.get("outcome") == "complete" for row in events),
                "physical_ledger_path": str(self.ledger),
                "physical_attempts_max": self.limit,
                "transients_per_completed_attempt": 2}
