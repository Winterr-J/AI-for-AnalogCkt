"""Durable, no-retry POSIX physical executor for SampleHold bulk stages."""

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
from .sample_hold_bulk_core import ROOT, derive_plan, sha


def _durable_json(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _append(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        json.dump(event, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


class BulkPhysicalExecutor:
    """One stage/circuit identity; every native launch consumes one durable slot."""

    def __init__(self, runtime: Any, stage_root: Path, *, stage: str, limit: int,
                 timeout_seconds: int = 180, extra_identity: Mapping[str, Any] | None = None):
        if os.name != "posix" or not sys.platform.startswith("linux"):
            raise AutoConfigError("SampleHold bulk physical execution requires Ubuntu/Linux")
        if stage not in {"profile", "d0", "al"} or type(limit) is not int or not 1 <= limit <= 50:
            raise AutoConfigError("invalid SampleHold bulk stage or physical budget")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
            raise AutoConfigError("invalid SampleHold native timeout")
        self.runtime = runtime
        self.plan = derive_plan(runtime.circuit_id)
        self.root = Path(stage_root).resolve()
        if not self.root.is_absolute() or self.root.is_relative_to(ROOT):
            raise AutoConfigError("SampleHold physical ledger must be outside checkout")
        self.stage = stage
        self.limit = limit
        self.timeout = timeout_seconds
        from ActiveLearning.onboarding.sample_hold_ubuntu import LIBRARY_ENV
        from ActiveLearning.sample_hold_linux_canary import _linux_environment
        library = os.getenv(LIBRARY_ENV)
        if not library:
            raise AutoConfigError(f"{LIBRARY_ENV} is required")
        self.environment = _linux_environment(Path(library))
        self.identity = {
            "schema": "sample_hold_bulk_native_v1",
            "circuit_id": runtime.circuit_id, "stage": stage,
            "limit": limit, "timeout_seconds": timeout_seconds,
            "source_sha256": self.plan["source_sha256"],
            "parameter_space_sha256": canonical_sha256(runtime.param_template),
            "fixture_sha256": self.plan["fixture_sha256"],
            "stimulus_sha256": self.plan["stimulus_sha256"],
            "executor_sha256": sha(Path(__file__)),
            "child_sha256": sha(Path(__file__).with_name("sample_hold_bulk_child.py")),
            "binding_sha256": sha(Path(__file__).with_name("sample_hold_bulk_core.py")),
            "measurement_sha256": sha(ROOT / "ActiveLearning/sc_sample_hold_generic.py"),
            "fixture_code_sha256": sha(ROOT / "ActiveLearning/sample_hold_offline.py"),
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
            raise AutoConfigError("SampleHold bulk physical identity changed")
        events = _events(self.ledger)
        reserved = [event for event in events if event.get("event") == "reserved"]
        if (len(reserved) > self.limit
                or [event.get("attempt") for event in reserved] != list(range(1, len(reserved) + 1))
                or len({event.get("vector_sha256") for event in reserved}) != len(reserved)
                or any(event.get("identity_sha256") != self.identity_sha256 for event in reserved)):
            raise AutoConfigError("SampleHold bulk physical budget/duplicate ledger invalid")

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
            raise AutoConfigError("SampleHold physical executor is busy or orphaned") from error
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
                        or not any(event.get("event") == "finished"
                                   and event.get("vector_sha256") == vector_sha
                                   and event.get("result_sha256") == sha256_file(cached)
                                   for event in _events(self.ledger))):
                    raise AutoConfigError("cached physical result provenance mismatch")
                release = True
                return data["evaluation"]
            if candidate.exists():
                raise AutoConfigError("SampleHold candidate already attempted; no automatic retry")
            events = _events(self.ledger)
            reserved = [event for event in events if event.get("event") == "reserved"]
            if len(reserved) >= self.limit:
                raise AutoConfigError("SampleHold physical attempt budget exhausted")
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
            command = [sys.executable, "-m", "ActiveLearning.onboarding.sample_hold_bulk_child",
                       "--candidate", str(candidate), "--token", token]
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            try:
                result = run_isolated(command, cwd=ROOT, timeout=self.timeout,
                                      environment=environment)
            except ProcessGroupUncertain as error:
                raise AutoConfigError("SampleHold child/descendant reaping unconfirmed") from error
            except subprocess.TimeoutExpired:
                _append(self.ledger, {"event": "finished", "attempt": len(reserved) + 1,
                                      "vector_sha256": vector_sha, "outcome": "timeout_reaped",
                                      "unix_s": time.time()})
                release = True
                raise
            evidence = getattr(result, "job_evidence", None)
            if (not isinstance(evidence, dict)
                    or evidence.get("contained_before_resume") is not True
                    or evidence.get("active_processes_at_return") != 0):
                raise AutoConfigError("SampleHold process containment unconfirmed")
            _durable_json(candidate / "process_status.json", {
                "returncode": result.returncode, "job_evidence": evidence,
                "stdout": result.stdout, "stderr": result.stderr,
            })
            if result.returncode != 0:
                _append(self.ledger, {"event": "finished", "attempt": len(reserved) + 1,
                                      "vector_sha256": vector_sha, "outcome": "failed_reaped",
                                      "exit_code": result.returncode, "unix_s": time.time()})
                release = True
                raise AutoConfigError(f"SampleHold native child exited {result.returncode}")
            data = json.loads(cached.read_text(encoding="utf-8"))
            if (data.get("vector_sha256") != vector_sha
                    or data.get("identity_sha256") != self.identity_sha256
                    or data.get("mock") is not False):
                raise AutoConfigError("SampleHold native result identity mismatch")
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
        return {"schema": "sample_hold_bulk_native_v1", "stage": self.stage,
                "physical_attempts": sum(row.get("event") == "reserved" for row in events),
                "completed_attempts": sum(row.get("event") == "finished"
                                          and row.get("outcome") == "complete" for row in events),
                "physical_ledger_path": str(self.ledger),
                "physical_attempts_max": self.limit}
