"""Automatic, read-only Ubuntu preflight for the reviewed SampleHold AL pilots.

The Windows canary closure remains provenance, not a Linux execution input.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from ActiveLearning.sc_sample_hold_generic import (
    ROOT, SCHEMA, CircuitMapping, OperatingConditions, _topology_contract,
    build_fixture, build_stimulus, canonical_sha256, sha256_file,
)
from ActiveLearning.sample_hold_linux_canary import _frozen_fixture, _linux_environment
from .errors import AutoConfigError


OUTPUT_ROOT_ENV = "SAMPLE_HOLD_UBUNTU_OUTPUT_ROOT"
LIBRARY_ENV = "NGSPICE_LIBRARY_PATH"
_FIXTURES = {
    "circuit_0259": "SC_0259_TRANSIENT_FIXTURE_V1_SC4B3C.json",
    "circuit_0263": "SC_0263_CANARY_FIXTURE_CANDIDATE_SC4C4.json",
}
_0263_CANONICAL_SHA256 = "1973f332e519fc767b353eba8bc09eccbef99e987dbe31966d0093614c6d537c"


def _asset(entry: Mapping[str, str]) -> Path:
    """Verify an actual Linux data asset against its reviewed raw-byte identity."""
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
        raise AutoConfigError("SampleHold source asset declaration changed")
    path = (ROOT / entry["path"]).resolve(strict=True)
    if not path.is_relative_to(ROOT) or not path.is_file() or sha256_file(path) != entry["sha256"]:
        raise AutoConfigError(f"SampleHold source asset missing or hash mismatch: {entry['path']}")
    return path


def load_al_plan(circuit_id: str) -> dict:
    """Compile the reviewed fixture without consulting Windows native closure bytes."""
    if circuit_id not in _FIXTURES:
        raise AutoConfigError("SampleHold Linux AL allows only circuit_0259/circuit_0263")
    path = ROOT / "ActiveLearning/docs/reference_audit" / _FIXTURES[circuit_id]
    if circuit_id == "circuit_0259":
        frozen = _frozen_fixture()
        data = frozen["data"]
        for name in ("topology", "converted_pyspice", "ideal_parameter_archive"):
            _asset(data["source_assets"][name])
        return {"config": data, "path": path, "file_sha256": sha256_file(path),
                "fixture": frozen["fixture"], "stimulus": frozen["stimulus"]}

    data = json.loads(path.read_text(encoding="utf-8"))
    if (canonical_sha256(data) != _0263_CANONICAL_SHA256
            or data.get("schema") != SCHEMA
            or data.get("status") != "candidate_unapproved"
            or data.get("circuit_id") != circuit_id):
        raise AutoConfigError("0263 reviewed fixture identity changed")
    assets = data["source_assets"]
    paths = {name: _asset(assets[name]) for name in
             ("topology", "converted", "ideal_parameters")}
    _topology_contract(data, paths)
    m, c = data["mapping"], data["conditions"]
    fixture = build_fixture(
        CircuitMapping(circuit_id, tuple(m["inputs"]), tuple(m["outputs"]),
                       m["clock"], m["track_clock_level"],
                       assets["topology"]["sha256"], tuple(m["diagnostic_nodes"])),
        OperatingConditions(tuple(c["vin_range_v"]), c["clock_period_s"],
                            c["clock_low_v"], c["clock_high_v"], c["clock_duty"],
                            c["clock_rise_s"], c["clock_fall_s"], c["provenance"],
                            fixed_common_mode_v=c["fixed_common_mode_v"],
                            vin_edge_s=c["vin_edge_s"]),
        startup_cycles=data["startup_cycles"], measurement_cycles=6,
        guard_time_s=data["guard_time_s"],
    )
    stimulus = build_stimulus(
        fixture, startup_input_v=data["startup_input_v"],
        physical_input_bounds_v=data["physical_input_bounds_v"],
    )
    if (fixture.fixture_hash != data["fixture_sha256"]
            or canonical_sha256(stimulus) != data["stimulus_sha256"]
            or data["budget"] != {"native_transient_attempts_max": 1,
                                  "automatic_retries": 0,
                                  "single_native_attempt_timeout_s": 180,
                                  "overall_execution_timeout_s": 240}):
        raise AutoConfigError("0263 fixture, stimulus or native budget changed")
    return {"config": data, "path": path, "file_sha256": sha256_file(path),
            "fixture": fixture, "stimulus": stimulus, "paths": paths}


def _check_attempt_ledger(experiment: Path, contract_sha: str,
                          circuit_id: str, execution_id: str, limit: int) -> None:
    """Read existing durable attempts without granting a replay or changing evidence."""
    if not experiment.exists():
        return
    identity_path = experiment / "identity.json"
    if not experiment.is_dir() or not identity_path.is_file():
        raise AutoConfigError("Ubuntu AL experiment identity is incomplete")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if (identity.get("execution_contract_sha256") != contract_sha
            or identity.get("circuit_id") != circuit_id
            or identity.get("experiment_id") != execution_id):
        raise AutoConfigError("Ubuntu AL experiment identity changed")
    ledger = experiment / "physical_attempt_ledger.jsonl"
    if not ledger.exists():
        if (experiment / "candidates").exists():
            raise AutoConfigError("Ubuntu AL candidate exists without attempt ledger")
        return
    attempts = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            if event.get("event") == "native_attempt_reserved":
                attempts.append(event)
    if (len(attempts) > limit
            or [row.get("attempt") for row in attempts] != list(range(1, len(attempts) + 1))
            or len({row.get("vector_sha256") for row in attempts}) != len(attempts)
            or len({row.get("token") for row in attempts}) != len(attempts)
            or any(not isinstance(row.get("vector_sha256"), str)
                   or not isinstance(row.get("token"), str) for row in attempts)):
        raise AutoConfigError("Ubuntu AL attempt budget or duplicate reservation changed")


def runtime_preflight(runtime: Any, config_path: Path, d0_path: Path,
                      pilot: Mapping[str, Any]) -> dict:
    """Bind exact Ubuntu resources; no simulator execution or experiment writes."""
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise AutoConfigError("SampleHold physical AL requires Ubuntu/Linux")
    if runtime.circuit_id not in _FIXTURES:
        raise AutoConfigError("SampleHold Linux AL circuit is not reviewed")
    config_path = Path(config_path).resolve(strict=True)
    d0_path = Path(d0_path).resolve(strict=True)
    root_text = os.getenv(OUTPUT_ROOT_ENV)
    library_text = os.getenv(LIBRARY_ENV)
    if not root_text or not library_text:
        raise AutoConfigError(f"set {OUTPUT_ROOT_ENV} and {LIBRARY_ENV} for Ubuntu AL")
    output_root = Path(root_text)
    if not output_root.is_absolute():
        raise AutoConfigError("Ubuntu AL output root must be absolute")
    output_root = output_root.resolve()
    if (output_root.is_relative_to(ROOT) or output_root == ROOT
            or output_root.exists() and not output_root.is_dir()):
        raise AutoConfigError("Ubuntu AL output root is not an external directory")
    ancestor = next((p for p in (output_root, *output_root.parents) if p.exists()), None)
    if ancestor is None or not os.access(ancestor, os.W_OK):
        raise AutoConfigError("Ubuntu AL output root is not writable")
    plan = load_al_plan(runtime.circuit_id)
    from ActiveLearning.onboarding.sample_hold_batch import _canonical_sha, al_pilot_contract
    contract = al_pilot_contract(runtime, config_path, output_root,
                                 pilot["execution_id"], pilot["budget"])
    _check_attempt_ledger(
        output_root / pilot["execution_id"], _canonical_sha(contract),
        runtime.circuit_id, pilot["execution_id"],
        pilot["budget"]["physical_attempts_max"],
    )
    environment = _linux_environment(Path(library_text))
    evidence = {
        "schema": "sample_hold_ubuntu_runtime_preflight_v1",
        "circuit_id": runtime.circuit_id,
        "config_sha256": sha256_file(config_path),
        "d0_sha256": sha256_file(d0_path),
        "fixture_file_sha256": plan["file_sha256"],
        "fixture_sha256": plan["fixture"].fixture_hash,
        "stimulus_sha256": plan["config"]["stimulus_sha256"],
        "execution_contract_sha256": _canonical_sha(contract),
        "environment": environment,
        "output_root": str(output_root),
        "budget": dict(pilot["budget"]),
        "attempt_ledger_policy": "durable_unique_reservation_no_automatic_retry",
        "python_executable": str(Path(sys.executable).resolve()),
    }
    evidence["evidence_sha256"] = canonical_sha256(evidence)
    return evidence
