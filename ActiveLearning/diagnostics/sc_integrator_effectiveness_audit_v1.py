"""Read-only, fail-closed SCIntegrator Batch effectiveness analysis.

Inputs are never written. Only four audit artifacts in a new --output-dir are
created. Missing or conflicting evidence is reported as UNKNOWN.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import yaml

METRICS = ("integration_step_magnitude_v", "output_headroom_v",
           "step_consistency_v", "virtual_ground_error_v",
           "zero_input_drift_v_per_cycle")
SAMPLE_FIELDS = ("batch_id", "circuit_id", "stage", "sample_id", "sampling_source", "iteration",
                 "parameter_valid", "simulation_valid", "measurement_valid", "functional_valid",
                 "validity_unknown_fields_json",
                 "recorded_meets_spec", "computed_meets_spec", "eligible",
                 "joint_violation", "pareto_dominated", "params_json",
                 "params_sha256", "metric_missing_or_nonfinite_fields_json",
                 "source_file", "source_sha256", *METRICS)
EFFECT_FIELDS = ("batch_id", "circuit_id", "status", "target_sha256",
                 "witness_ids", "profile_best_id", "d0_best_id", "al_best_id",
                 "overall_best_id", "profile_best_violation", "d0_best_violation",
                 "al_best_violation", "overall_best_violation", "al_improved_d0",
                 "d0_joint_success_count", "al_joint_success_count",
                 "al_exact_witness_vector", "valid_profile", "valid_d0",
                 "valid_al", "al_run_id", "issues")
CONSTRAINT_FIELDS = ("batch_id", "circuit_id", "stage", "sample_id", "metric",
                     "operator", "constraint_type", "threshold", "measured", "scale", "passes",
                     "normalized_violation", "source_file", "source_sha256")


class EvidenceError(ValueError):
    pass


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"),
                           object_pairs_hook=unique_pairs,
                           parse_constant=float)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"unreadable JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"non-object JSON root: {path}")
    return value


def read_jsonl(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise EvidenceError(f"unreadable JSONL {path}: {exc}") from exc
    result = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            raise EvidenceError(f"blank JSONL line {path}:{index}")
        try:
            row = json.loads(line, object_pairs_hook=unique_pairs)
        except (json.JSONDecodeError, EvidenceError) as exc:
            raise EvidenceError(f"truncated/conflicting JSONL {path}:{index}: {exc}") from exc
        if not isinstance(row, dict):
            raise EvidenceError(f"non-object JSONL {path}:{index}")
        result.append(row)
    return result


class UniqueLoader(yaml.SafeLoader):
    pass


def yaml_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise EvidenceError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


def read_yaml(path):
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EvidenceError(f"unreadable YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"non-mapping YAML: {path}")
    return value


def sha(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError(f"unreadable source {path}: {exc}") from exc


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def inside(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or "\\" in relative:
        raise EvidenceError(f"unsafe relative path: {relative}")
    path = root / relative
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)) or path.is_symlink():
        raise EvidenceError(f"path escaped data root: {path}")
    return resolved


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def target_contract(config, template):
    specs = config.get("target_specs")
    body = template.get("performance_template", {})
    if not isinstance(specs, dict) or set(specs) != set(METRICS) or set(body.get("active_metrics", [])) != set(METRICS):
        raise EvidenceError("frozen target/template must contain exactly five metrics")
    result = {}
    for name in METRICS:
        direction = body["metrics"][name]["direction"]
        operator = {"maximize": ">=", "minimize": "<="}.get(direction)
        spec = specs[name]
        if operator is None or not isinstance(spec, dict) or spec.get("operator") != operator:
            raise EvidenceError(f"target direction conflict: {name}")
        value = finite(spec.get("value"))
        if value is None:
            raise EvidenceError(f"nonfinite target: {name}")
        result[name] = {"operator": operator, "value": value,
                        "constraint_type": spec.get("constraint_type")}
    return result


def as_sample(raw, stage, batch_id, circuit_id, path):
    status = raw if stage == "PROFILE" else raw.get("status", {})
    metrics = raw if stage == "PROFILE" else raw.get("metrics", {})
    params, sample_id = raw.get("params"), raw.get("sample_id")
    if not isinstance(status, dict) or not isinstance(metrics, dict) or not isinstance(params, dict) or not params or not isinstance(sample_id, str) or not sample_id:
        raise EvidenceError(f"incomplete sample {path}/{sample_id}")
    flags = {key: status.get(key) for key in ("parameter_valid", "simulation_valid", "functional_valid")}
    if any(value is not None and type(value) is not bool for value in flags.values()):
        raise EvidenceError(f"nonboolean validity {path}/{sample_id}")
    measurement_valid = status.get("measurement_valid")
    if measurement_valid is not None and type(measurement_valid) is not bool:
        raise EvidenceError(f"nonboolean measurement validity {path}/{sample_id}")
    values = {name: finite(metrics.get(name)) for name in METRICS}
    return {"batch_id": batch_id, "circuit_id": circuit_id, "stage": stage,
            "sample_id": sample_id, "sampling_source": raw.get("sampling_source", raw.get("source")),
            "iteration": raw.get("iteration"), **flags,
            "measurement_valid": measurement_valid,
            "validity_unknown_fields_json": json.dumps([key for key, value in flags.items()
                                                         if value is None] +
                                                        (["measurement_valid"] if stage != "PROFILE"
                                                         and measurement_valid is None else [])),
            "recorded_meets_spec": status.get("meets_spec"),
            "params_json": json.dumps(params, sort_keys=True, ensure_ascii=False),
            "params_sha256": canonical_sha(params), "source_file": str(path),
            "source_sha256": sha(path),
            "metric_missing_or_nonfinite_fields_json": json.dumps(
                [name for name, value in values.items() if value is None]), **values,
            "eligible": all(flags.values()) and (stage == "PROFILE" or measurement_valid is True)
            and all(v is not None for v in values.values())}


def distinct(rows, label):
    ids = [row["sample_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise EvidenceError(f"duplicate sample ID in {label}")


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def scales_from_profile(profile, target):
    valid = [row for row in profile if row["eligible"]]
    if not valid:
        raise EvidenceError("no fully valid Profile samples for scale")
    result = {}
    for name in METRICS:
        values = [row[name] for row in valid]
        result[name] = max(abs(target[name]["value"]), percentile(values, .9) - percentile(values, .1))
        if result[name] <= 0:
            raise EvidenceError(f"zero Profile scale: {name}")
    return result


def annotate(rows, target, scales):
    details = []
    for row in rows:
        violations, passes = [], []
        for name in METRICS:
            spec, value = target[name], row[name]
            difference = None if value is None else (spec["value"] - value if spec["operator"] == ">=" else value - spec["value"])
            passed = None if difference is None else difference <= 0
            violation = None if difference is None else max(0.0, difference / scales[name])
            passes.append(passed)
            violations.append(violation)
            details.append({"batch_id": row["batch_id"], "circuit_id": row["circuit_id"],
                            "stage": row["stage"], "sample_id": row["sample_id"],
                            "metric": name, "operator": spec["operator"],
                            "constraint_type": spec["constraint_type"],
                            "threshold": spec["value"], "measured": value,
                            "scale": scales[name], "passes": passed,
                            "normalized_violation": violation,
                            "source_file": row["source_file"], "source_sha256": row["source_sha256"]})
        row["computed_meets_spec"] = None if any(p is None for p in passes) else row["eligible"] and all(passes)
        row["joint_violation"] = max(violations) if row["eligible"] else None
        row["_tie"] = tuple(sorted(violations, reverse=True)) if row["eligible"] else None
    return details


def best(rows):
    valid = [row for row in rows if row["eligible"]]
    return min(valid, key=lambda r: (r["joint_violation"], r["_tie"], r["stage"], r["sample_id"])) if valid else None


def mark_pareto(rows, target):
    valid = [row for row in rows if row["eligible"]]
    for row in rows:
        row["pareto_dominated"] = None if not row["eligible"] else any(
            other is not row and all((other[m] >= row[m] if target[m]["operator"] == ">=" else other[m] <= row[m]) for m in METRICS)
            and any((other[m] > row[m] if target[m]["operator"] == ">=" else other[m] < row[m]) for m in METRICS)
            for other in valid)


def manifest_ids(repo, number):
    batch_id = f"MTV2_SCI_PREP_{number:03d}"
    base = repo / "ActiveLearning/candidate_manifests/sc_integrator_v2"
    master_path = base / "master_manifest.json"
    path = base / "batches" / f"{batch_id}.json"
    master, manifest = read_json(master_path), read_json(path)
    ids = manifest.get("circuit_ids")
    candidates = master.get("candidates", [])
    if master.get("candidate_count") != 811 or len(candidates) != 811:
        raise EvidenceError(f"candidate master count conflict: {master_path}")
    expected = [row["circuit_id"] for row in candidates[(number - 1) * 10:number * 10]]
    if (manifest.get("schema_version") != "sc_integrator_preparation_candidate_batch_v1"
            or manifest.get("batch_id") != batch_id or manifest.get("circuit_type") != "SCIntegrator"
            or manifest.get("status") != "CANDIDATE_IDS_ONLY_NOT_PREPARATION_READY"
            or manifest.get("master_sha256") != sha(master_path) or ids != expected
            or len(ids) != 10 or len(set(ids)) != 10
            or [row.get("circuit_id") for row in manifest.get("candidates", [])] != ids):
        raise EvidenceError(f"candidate manifest identity conflict: {path}")
    return batch_id, ids, sha(path)


def assert_hash(path, expected, label):
    if not isinstance(expected, str) or sha(path) != expected:
        raise EvidenceError(f"{label} hash conflict: {path}")


def verify_physical_ledger(directory, stage, circuit_id, manifest_hash, config_sha=None):
    physical = directory / f"physical_{stage}"
    identity = read_json(inside(physical, "identity.json"))
    events = read_jsonl(inside(physical, "physical_attempt_ledger.jsonl"))
    limit = 40 if stage == "profile" else 20
    if (identity.get("schema") != "sc_integrator_bulk_native_v1"
            or identity.get("stage") != stage or identity.get("circuit_id") != circuit_id
            or identity.get("limit") != limit
            or identity.get("extra", {}).get("candidate_manifest_sha256") != manifest_hash
            or (config_sha is not None and identity.get("extra", {}).get("config_sha256") != config_sha)):
        raise EvidenceError(f"{stage} physical identity conflict: {circuit_id}")
    identity_hash = canonical_sha(identity)
    reserved = [r for r in events if r.get("event") == "reserved"]
    finished = [r for r in events if r.get("event") == "finished"]
    if (len(events) != len(reserved) + len(finished)
            or len(reserved) != len(finished) or len(reserved) > limit
            or (stage == "d0" and len(reserved) != 20)
            or [r.get("attempt") for r in reserved] != list(range(1, len(reserved) + 1))
            or len({r.get("vector_sha256") for r in reserved}) != len(reserved)
            or any(a.get("identity_sha256") != identity_hash
                   or b.get("attempt") != a.get("attempt")
                   or b.get("vector_sha256") != a.get("vector_sha256")
                   or b.get("outcome") != "complete"
                   for a, b in zip(reserved, finished))):
        raise EvidenceError(f"{stage} physical ledger conflict: {circuit_id}")
    return len(reserved)


def load_preparation(root, batch_id, circuit_id, manifest_hash, template):
    batch_dir = root / batch_id
    summary = read_json(batch_dir / "summary.json")
    directory = batch_dir / circuit_id
    receipt = read_json(directory / "result.json")
    if summary.get("batch_id") != batch_id or summary.get("status") != "PREPARATION_COMPLETE" or receipt.get("batch_id") != batch_id or receipt.get("circuit_id") != circuit_id:
        raise EvidenceError(f"Preparation identity conflict: {circuit_id}")
    summary_row = summary.get("circuits", {}).get(circuit_id)
    if summary_row is not None and summary_row != receipt:
        raise EvidenceError(f"Preparation summary/receipt conflict: {circuit_id}")
    if receipt.get("status") != "PREPARATION_READY":
        return receipt, None
    if receipt.get("candidate_manifest_sha256") != manifest_hash:
        raise EvidenceError(f"Preparation manifest conflict: {circuit_id}")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise EvidenceError(f"missing Preparation artifact closure: {circuit_id}")
    closed = {name: inside(directory, name) for name in artifacts}
    for name, path in closed.items():
        assert_hash(path, artifacts[name], "Preparation artifact")
    paths = {key: inside(directory, receipt.get(key)) for key in ("config_path", "d0_path", "profile_path")}
    target_path = paths["config_path"].with_name("target_generation.json")
    d0_meta_path = paths["d0_path"].with_name(paths["d0_path"].stem + "_initialization.json")
    if any(path not in closed.values() for path in (*paths.values(), target_path, d0_meta_path)):
        raise EvidenceError(f"key artifact absent from closure: {circuit_id}")
    config = read_yaml(paths["config_path"])
    profile = read_json(paths["profile_path"])
    target_result = read_json(target_path)
    d0_meta = read_json(d0_meta_path)
    if (config.get("circuit_info") != {"circuit_type": "SCIntegrator", "circuit_id": circuit_id}
            or config.get("target_source") != "auto_generated"
            or config.get("target_provenance", {}).get("profiling_backend") != "spice"
            or profile.get("evaluation_backend") != "spice"
            or profile.get("circuit_id") != circuit_id
            or target_result.get("status") != "TARGET_VALIDATED"):
        raise EvidenceError(f"Preparation target/profile identity conflict: {circuit_id}")
    target = target_contract(config, template)
    if canonical_sha(config["target_specs"]) != receipt.get("target_spec_sha256") or target_result.get("validation", {}).get("target_specs") != config["target_specs"]:
        raise EvidenceError(f"frozen Target hash/recommendation conflict: {circuit_id}")
    data = config.get("data", {})
    if (data.get("d0_mode") != "independent" or data.get("seed") != 42
            or data.get("n_samples") != 20 or config.get("active_learning", {}).get("max_iterations") != 5):
        raise EvidenceError(f"independent D0/AL config mismatch: {circuit_id}")
    sampling = d0_meta.get("sampling", {})
    if (d0_meta.get("d0_mode") != "independent" or d0_meta.get("seed") != 42
            or d0_meta.get("records") != 20 or sampling.get("seed") != 42
            or sampling.get("n_samples") != 20 or sampling.get("n_local") != 10
            or sampling.get("n_global") != 9
            or sampling.get("sampling_mode") != data.get("sampling_mode")):
        raise EvidenceError(f"D0 initialization metadata conflict: {circuit_id}")
    raw_profile = profile.get("records")
    if not isinstance(raw_profile, list):
        raise EvidenceError(f"missing physical Profile records: {circuit_id}")
    profile_rows = [as_sample(r, "PROFILE", batch_id, circuit_id, paths["profile_path"]) for r in raw_profile]
    d0_rows = [as_sample(r, "D0", batch_id, circuit_id, paths["d0_path"]) for r in read_jsonl(paths["d0_path"])]
    distinct(profile_rows, "Profile")
    distinct(d0_rows, "D0")
    if len(profile_rows) != profile.get("total_samples") or len(d0_rows) != 20:
        raise EvidenceError(f"Profile/D0 count conflict: {circuit_id}")
    raw_d0 = read_jsonl(paths["d0_path"])
    sources = [row.get("sampling_source") for row in raw_d0]
    if (sources.count("anchor") != 1 or sources.count("local") != 10
            or sources.count("global") != 9):
        raise EvidenceError(f"D0 sampling roles conflict: {circuit_id}")
    profile_attempts = verify_physical_ledger(directory, "profile", circuit_id, manifest_hash)
    d0_attempts = verify_physical_ledger(directory, "d0", circuit_id, manifest_hash,
                                         sha(paths["config_path"]))
    return receipt, {"paths": paths, "config": config, "profile": profile,
                     "target_result": target_result, "target": target,
                     "profile_rows": profile_rows, "d0_rows": d0_rows,
                     "profile_attempts": profile_attempts, "d0_attempts": d0_attempts,
                     "receipt": receipt, "d0_meta_path": d0_meta_path}


def witness_ids(profile_rows, prep):
    computed = sorted(r["sample_id"] for r in profile_rows if r["eligible"] and r["computed_meets_spec"] is True)
    target_result, config, profile = prep["target_result"], prep["config"], prep["profile"]
    declared = target_result.get("validation", {}).get("joint_supporting_sample_ids")
    provenance = config.get("target_provenance", {}).get("joint_supporting_sample_ids")
    candidate_metrics = profile.get("valid_candidate_metrics")
    if not isinstance(candidate_metrics, list):
        raise EvidenceError("missing valid_candidate_metrics")
    by_id = {r["sample_id"]: r for r in profile_rows}
    if len({r.get("sample_id") for r in candidate_metrics}) != len(candidate_metrics):
        raise EvidenceError("duplicate AutoTarget valid candidate")
    for row in candidate_metrics:
        actual = by_id.get(row.get("sample_id"))
        if actual is None or not actual["eligible"] or any(actual[m] != row.get("metrics", {}).get(m) for m in METRICS):
            raise EvidenceError("AutoTarget candidate metrics conflict with Profile")
    if not computed or sorted(declared or []) != computed or sorted(provenance or []) != computed:
        raise EvidenceError("Profiling Witness conflicts with frozen Target provenance")
    return computed


def load_al(root, batch_id, circuit_id, manifest_hash, prep, template):
    directory = root / batch_id
    if not directory.exists():
        return [], {"state": "NO_BATCH"}
    manifest, state = read_json(directory / "manifest.json"), read_json(directory / "status.json")
    frozen_manifest = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if (manifest.get("batch_id") != batch_id
            or manifest.get("candidate_manifest_sha256") != manifest_hash
            or manifest.get("manifest_sha256") != canonical_sha({"runtime": frozen_manifest})
            or state.get("manifest_sha256") != manifest.get("manifest_sha256")):
        raise EvidenceError(f"Batch manifest/status conflict: {batch_id}")
    jobs = [j for j in manifest.get("jobs", []) if j.get("circuit_id") == circuit_id]
    if not jobs:
        return [], {"state": "NO_JOB"}
    if len(jobs) != 1 or jobs[0].get("job_id") != circuit_id:
        raise EvidenceError(f"ambiguous Batch job: {circuit_id}")
    job = jobs[0]
    paths = prep["paths"]
    source_path = job.get("source_path")
    source_hashes = prep["receipt"].get("source_sha256")
    if (Path(job.get("config_path", "")).resolve() != paths["config_path"]
            or Path(job.get("d0_path", "")).resolve() != paths["d0_path"]
            or job.get("config_sha256") != sha(paths["config_path"])
            or job.get("d0_sha256") != sha(paths["d0_path"])
            or not isinstance(source_path, str) or not source_path
            or Path(source_path).is_absolute()
            or not isinstance(source_hashes, dict) or source_path not in source_hashes
            or not isinstance(source_hashes[source_path], str)
            or not isinstance(job.get("source_sha256"), str)
            or job["source_sha256"] != source_hashes[source_path]
            or job.get("target_spec_sha256") != canonical_sha(prep["config"]["target_specs"])):
        raise EvidenceError(f"Batch job not bound to Preparation: {circuit_id}")
    job_state = state.get("jobs", {}).get(circuit_id)
    if not isinstance(job_state, dict):
        raise EvidenceError(f"missing Batch job state: {circuit_id}")
    attempts = job_state.get("attempts", [])
    if not isinstance(attempts, list) or len({r.get("attempt_id") for r in attempts}) != len(attempts):
        raise EvidenceError(f"ambiguous Batch attempts: {circuit_id}")
    if job_state.get("state") != "COMPLETED":
        return [], {"state": job_state.get("state"), "batch_attempts": len(attempts)}
    if len(attempts) != 1:
        raise EvidenceError(f"completed job has ambiguous attempts: {circuit_id}")
    attempt_id = attempts[0].get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.isalnum():
        raise EvidenceError(f"invalid attempt ID: {circuit_id}")
    attempt = inside(directory, f"jobs/{circuit_id}/attempts/{attempt_id}")
    result_path = inside(attempt, "result.json")
    assert_hash(result_path, attempts[0].get("result_sha256"), "Batch result")
    result = read_json(result_path)
    context = read_json(inside(attempt, "al_run_context.json"))
    run_dir = Path(context.get("run_directory", "")).resolve(strict=True)
    if not run_dir.is_relative_to(attempt) or run_dir.name != context.get("run_id") or run_dir.is_symlink():
        raise EvidenceError(f"AL run escaped Batch attempt: {circuit_id}")
    summary_path = inside(run_dir, "summary.json")
    summary = read_json(summary_path)
    if (summary.get("run_id") != context.get("run_id") or summary.get("circuit_id") != circuit_id
            or summary.get("circuit_type") != "SCIntegrator" or result.get("run_id") != context.get("run_id")
            or result.get("al_result_sha256") != sha(summary_path)
            or Path(result.get("result_path", "")).resolve() != summary_path
            or summary.get("iterations_completed") != result.get("iterations_completed")):
        raise EvidenceError(f"AL summary/result identity conflict: {circuit_id}")
    snapshot = read_json(inside(run_dir, "config_snapshot.json"))
    if target_contract(snapshot, template) != prep["target"] or snapshot.get("target_specs") != prep["config"]["target_specs"]:
        raise EvidenceError(f"AL snapshot differs from frozen Target: {circuit_id}")
    records_path = inside(run_dir, "active_learning_records.jsonl")
    records = read_jsonl(records_path)
    d0 = prep["d0_rows"]
    if len(records) < len(d0):
        raise EvidenceError(f"AL history shorter than D0: {circuit_id}")
    for original, historical in zip(d0, records[:len(d0)]):
        if (original["sample_id"] != historical.get("sample_id")
                or original["params_sha256"] != canonical_sha(historical.get("params"))
                or any(original[flag] != historical.get("status", {}).get(flag)
                       for flag in ("parameter_valid", "simulation_valid", "measurement_valid",
                                    "functional_valid"))
                or any(original[m] != finite(historical.get("metrics", {}).get(m)) for m in METRICS)):
            raise EvidenceError(f"AL history rewrote D0: {circuit_id}")
    iterations = read_jsonl(inside(run_dir, "iteration_log.jsonl"))
    count = summary.get("iterations_completed")
    if not isinstance(count, int) or len(iterations) != count or len(records) != len(d0) + count:
        raise EvidenceError(f"AL iteration/history count conflict: {circuit_id}")
    al = [as_sample(r, "AL", batch_id, circuit_id, records_path) for r in records[len(d0):]]
    distinct(al, "AL")
    for index, (row, log) in enumerate(zip(al, iterations), 1):
        if (row["iteration"] != index or log.get("iteration") != index
                or log.get("run_id") != context["run_id"]
                or row["sample_id"] != f"iter_{index:03d}_{log.get('selected_candidate_id')}"):
            raise EvidenceError(f"AL iteration/sample identity conflict: {circuit_id}/{index}")
    if count:
        identity = read_json(inside(directory, f"native/{circuit_id}/identity.json"))
        ledger = read_jsonl(inside(directory, f"native/{circuit_id}/physical_attempt_ledger.jsonl"))
        reserved = [r for r in ledger if r.get("event") == "reserved"]
        finished = [r for r in ledger if r.get("event") == "finished"]
        if (identity.get("schema") != "sc_integrator_bulk_native_v1"
                or identity.get("stage") != "al" or identity.get("circuit_id") != circuit_id
                or identity.get("limit") != 5
                or identity.get("extra", {}).get("batch_id") != batch_id
                or identity.get("extra", {}).get("config_sha256") != sha(paths["config_path"])
                or identity.get("extra", {}).get("d0_sha256") != sha(paths["d0_path"])
                or len(ledger) != len(reserved) + len(finished)
                or len(reserved) != count or len(finished) != count or len(reserved) > 5
                or [r.get("attempt") for r in reserved] != list(range(1, count + 1))
                or len({r.get("vector_sha256") for r in reserved}) != len(reserved)
                or any(a.get("identity_sha256") != canonical_sha(identity)
                       or a.get("attempt") != b.get("attempt")
                       or a.get("vector_sha256") != b.get("vector_sha256")
                       or b.get("outcome") != "complete" for a, b in zip(reserved, finished))):
            raise EvidenceError(f"AL physical ledger conflict: {circuit_id}")
    return al, {"state": "COMPLETED", "run_id": context["run_id"],
                "attempt_id": attempt_id, "iterations": count,
                "physical_evaluator_reservations": count,
                "native_TRAN_count": "UNKNOWN_FROM_LEDGER_ALONE",
                "summary_path": str(summary_path)}


def audit_circuit(prep_root, batch_root, batch_id, circuit_id, manifest_hash, template):
    receipt, prep = load_preparation(prep_root, batch_id, circuit_id, manifest_hash, template)
    if prep is None:
        return {"batch_id": batch_id, "circuit_id": circuit_id,
                "status": receipt.get("status", "UNKNOWN"), "issues": receipt.get("reason", "")}, [], [], {"preparation_status": receipt.get("status")}
    target, profile, d0 = prep["target"], prep["profile_rows"], prep["d0_rows"]
    scales = scales_from_profile(profile, target)
    al, al_meta = load_al(batch_root, batch_id, circuit_id, manifest_hash, prep, template)
    samples = profile + d0 + al
    constraints = annotate(samples, target, scales)
    mark_pareto(samples, target)
    witnesses = witness_ids(profile, prep)
    conflicts = [r["sample_id"] for r in d0 + al if
                 (r["recorded_meets_spec"] is True and not r["eligible"])
                 or (r["eligible"] and r["recorded_meets_spec"] is not None
                     and r["recorded_meets_spec"] != r["computed_meets_spec"])]
    if conflicts:
        raise EvidenceError(f"saved meets_spec conflicts with frozen Target: {conflicts}")
    pb, db, ab, overall = best(profile), best(d0), best(al), best(d0 + al)
    witness_vectors = {r["params_sha256"] for r in profile if r["sample_id"] in witnesses}
    d0_success = sum(r["eligible"] and r["computed_meets_spec"] is True for r in d0)
    result = {"batch_id": batch_id, "circuit_id": circuit_id,
              "status": "OK" if al_meta["state"] == "COMPLETED" else al_meta["state"],
              "target_sha256": canonical_sha(prep["config"]["target_specs"]),
              "witness_ids": json.dumps(witnesses),
              "profile_best_id": pb["sample_id"] if pb else None,
              "d0_best_id": db["sample_id"] if db else None,
              "al_best_id": ab["sample_id"] if ab else None,
              "overall_best_id": overall["sample_id"] if overall else None,
              "profile_best_violation": pb["joint_violation"] if pb else None,
              "d0_best_violation": db["joint_violation"] if db else None,
              "al_best_violation": ab["joint_violation"] if ab else None,
              "overall_best_violation": overall["joint_violation"] if overall else None,
              "al_improved_d0": None if db is None or ab is None else ab["joint_violation"] < db["joint_violation"],
              "d0_joint_success_count": d0_success,
              "al_joint_success_count": sum(r["eligible"] and r["computed_meets_spec"] is True for r in al),
              "al_exact_witness_vector": any(r["params_sha256"] in witness_vectors for r in al),
              "valid_profile": sum(r["eligible"] for r in profile),
              "valid_d0": sum(r["eligible"] for r in d0),
              "valid_al": sum(r["eligible"] for r in al),
              "al_run_id": al_meta.get("run_id"),
              "issues": ("D0_SUCCESS_BUT_AL_RAN_CHECK_STOP_POLICY"
                         if d0_success and al_meta.get("iterations", 0)
                         and prep["config"]["active_learning"].get("stop_on_meets_spec") is True
                         else "")}
    metadata = {"preparation_status": receipt["status"],
                "target_source": str(prep["paths"]["config_path"]),
                "target_sha256": result["target_sha256"],
                "frozen_target_specs": prep["config"]["target_specs"],
                "profile_source": str(prep["paths"]["profile_path"]),
                "profile_sha256": sha(prep["paths"]["profile_path"]),
                "target_generation_source": str(prep["paths"]["config_path"].with_name("target_generation.json")),
                "target_generation_sha256": sha(prep["paths"]["config_path"].with_name("target_generation.json")),
                "d0_source": str(prep["paths"]["d0_path"]),
                "d0_sha256": sha(prep["paths"]["d0_path"]),
                "d0_initialization_source": str(prep["d0_meta_path"]),
                "d0_initialization_sha256": sha(prep["d0_meta_path"]),
                "witness_ids": witnesses, "profile_scales": scales,
                "profile_evaluator_attempts": prep["profile_attempts"],
                "d0_evaluator_attempts": prep["d0_attempts"],
                "stop_on_meets_spec": prep["config"]["active_learning"].get("stop_on_meets_spec"),
                "overall_best_binding_constraints": [
                    name for name in METRICS
                    if overall is not None and
                    (target[name]["value"] - overall[name] if target[name]["operator"] == ">="
                     else overall[name] - target[name]["value"]) > 0],
                "valid_pareto_nondominated_count": sum(
                    row["eligible"] and row["pareto_dominated"] is False for row in samples),
                "al": al_meta}
    return result, samples, constraints, metadata


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(repo_root, preparation_root, batch_root, batch_start, batch_end, output_dir):
    roots = [p.resolve(strict=True) for p in (repo_root, preparation_root, batch_root)]
    output = output_dir.resolve()
    if any(output == root or output.is_relative_to(root) or root.is_relative_to(output) for root in roots):
        raise EvidenceError("output must be independent of repository and production roots")
    if output_dir.is_symlink() or (output.exists() and any(output.iterdir())):
        raise EvidenceError("output must be new or empty; refusing overwrite")
    if not 1 <= batch_start <= batch_end <= 82:
        raise EvidenceError("batch range outside 001..082")
    template = read_yaml(roots[0] / "ActiveLearning/circuit_types/SCIntegrator/performance_template.yaml")
    inventory, seen = [], set()
    for number in range(batch_start, batch_end + 1):
        batch_id, ids, manifest_hash = manifest_ids(roots[0], number)
        for circuit_id in ids:
            if circuit_id in seen:
                raise EvidenceError(f"circuit in multiple selected manifests: {circuit_id}")
            seen.add(circuit_id)
            inventory.append((batch_id, circuit_id, manifest_hash))
    samples, effectiveness, constraints, evidence = [], [], [], {}
    for batch_id, circuit_id, manifest_hash in inventory:
        try:
            row, circuit_samples, circuit_constraints, metadata = audit_circuit(
                roots[1], roots[2], batch_id, circuit_id, manifest_hash, template)
            effectiveness.append(row)
            samples.extend(circuit_samples)
            constraints.extend(circuit_constraints)
            evidence[circuit_id] = {"status": row["status"], **metadata}
        except (EvidenceError, OSError, KeyError, TypeError, ValueError) as exc:
            effectiveness.append({"batch_id": batch_id, "circuit_id": circuit_id,
                                  "status": "INTEGRITY_UNKNOWN", "issues": str(exc)})
            evidence[circuit_id] = {"status": "INTEGRITY_UNKNOWN", "error": str(exc),
                                    "statistics_suppressed": True}
    report = {"schema_version": "sc_integrator_effectiveness_audit_v1",
              "synthetic_results": False, "batch_start": batch_start,
              "batch_end": batch_end, "selected_circuit_count": len(inventory),
              "complete_evidence_count": sum(r["status"] == "OK" for r in effectiveness),
              "distance_definition": {
                  "scale": "max(abs(frozen target), valid Profile P90-P10)",
                  "zero_scale": "UNKNOWN; suppress comparison",
                  "eligibility": "Profile: parameter/simulation/functional valid; D0/AL: additionally measurement_valid; all five metrics finite",
                  "per_metric": "max(0,(target-value)/scale) for >=; reverse for <=",
                  "joint": "maximum of five violations in one physical sample",
                  "tie_break": "lexicographic descending violations; then stage and sample ID",
                  "improvement": "strictly smaller joint violation",
                  "pareto": "five measured directions among fully valid samples"},
              "limitations": [
                  "A missing Witness is evidence inconsistency, not proof of physical infeasibility.",
                  "Exact Witness parameter equality is tested; nearby-region exploration needs a preregistered distance.",
                  "Native ledger reservations count evaluator calls, not individual TRAN completions.",
                  "Failed/interrupted Batch jobs are not promoted to successful AL samples."],
              "circuits": evidence}
    output.mkdir(parents=True, exist_ok=True)
    (output / "data_integrity_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_csv(output / "sc_integrator_samples.csv", SAMPLE_FIELDS, samples)
    write_csv(output / "sc_integrator_effectiveness.csv", EFFECT_FIELDS, effectiveness)
    write_csv(output / "sc_integrator_target_constraints.csv", CONSTRAINT_FIELDS, constraints)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo-root", "preparation-root", "batch-root", "output-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--batch-start", type=int, required=True)
    parser.add_argument("--batch-end", type=int, required=True)
    args = parser.parse_args()
    try:
        report = run(args.repo_root, args.preparation_root, args.batch_root,
                     args.batch_start, args.batch_end, args.output_dir)
    except (EvidenceError, OSError) as exc:
        parser.exit(2, f"audit refused: {exc}\n")
    print(json.dumps({"selected_circuit_count": report["selected_circuit_count"],
                      "complete_evidence_count": report["complete_evidence_count"],
                      "output_dir": str(args.output_dir.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
